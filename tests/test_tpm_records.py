import concurrent.futures
import json
import math
import threading
import unittest
from unittest.mock import patch

from tpm_metrics import LiveMetrics
from tpm_records import CompletedRecords
from tpm_report import metrics as reference_metrics
from tpm_snapshot import clone_snapshot
from tpm_usage import normalize_usage


def row(index):
    return {"id": str(index), "phase": "formal", "started_at": float(index),
            "ended_at": index + .25, "latency_seconds": .25,
            "ttft_seconds": .125, "schedule_delay_seconds": .00002,
            "pool_wait_seconds": .000001, "success": True, "error": None,
            "usage": normalize_usage({"prompt_tokens": 99, "completion_tokens": 1,
                                       "prompt_tokens_details": {"cached_tokens": 90},
                                       "extra": [True, None, "合成材料", {"fraction": .12345678901234567}]}),
            "timing": {"started_at": "2026-10-16T16:00:00.123+08:00"}}


class CompletedRecordsTests(unittest.TestCase):
    def test_empty_and_invalid_indexes(self):
        records = CompletedRecords()
        self.assertEqual(len(records), 0)
        self.assertEqual(records[:], [])
        self.assertEqual(list(records), [])
        for key in (0, -1):
            with self.assertRaises(IndexError):
                records[key]
        with self.assertRaises(TypeError):
            records[1.5]
        with self.assertRaises(ValueError):
            records[::0]

    def test_append_encoding_and_hot_ownership(self):
        records = CompletedRecords(hot_limit=2)
        source = row(0)
        result = records.append(source)
        self.assertEqual(result, json.dumps(source, ensure_ascii=False, separators=(",", ":")))
        self.assertIs(records[0], source, "completed record ownership is retained while hot")
        records.append(row(1))
        records.append(row(2))
        self.assertEqual(records.hot_count, 2)
        self.assertEqual(records.hot_limit, 2)
        self.assertIsNot(records[0], source)
        self.assertEqual(records[0], source)
        self.assertEqual(json.loads(result), records[0])

    def test_all_fields_types_and_numeric_precision_survive_cold_roundtrip(self):
        records = CompletedRecords(hot_limit=0)
        source = row(0)
        source["large_integer"] = 2 ** 80 + 1
        source["negative_zero"] = -0.0
        source["raw_usage"] = {"missing": None, "explicit_zero": 0, "array": [], "object": {}}
        records.append(source)
        restored = records[0]
        self.assertEqual(source, restored)
        self.assertEqual(json.dumps(source, ensure_ascii=False), json.dumps(restored, ensure_ascii=False))
        self.assertIs(type(restored["usage"]["issues"]), list)
        self.assertIs(type(restored["usage"]["raw"]["extra"]), list)
        self.assertEqual(records.hot_count, 0)

    def test_integer_negative_and_slice_semantics_match_list(self):
        source = [row(index) for index in range(9)]
        records = CompletedRecords(hot_limit=3)
        for item in source:
            records.append(item)
        for index in range(-9, 9):
            self.assertEqual(records[index], source[index])
        for key in (slice(None), slice(None, None, -1), slice(-6, -1), slice(2, 8, 2),
                    slice(100), slice(-100, 100), slice(8, 1, -3), slice(7, 1)):
            self.assertEqual(records[key], source[key])
        for index in (-10, 9):
            with self.assertRaises(IndexError):
                records[index]

    def test_iteration_captures_length_when_called(self):
        records = CompletedRecords(hot_limit=1)
        records.append(row(0))
        iterator = iter(records)
        records.append(row(1))
        self.assertEqual(list(iterator), [row(0)])
        self.assertEqual(list(records), [row(0), row(1)])

    def test_slice_captures_range_before_concurrent_append_and_eviction(self):
        records = CompletedRecords(hot_limit=1)
        for index in range(3):
            records.append(row(index))
        decode_started, continue_decode = threading.Event(), threading.Event()
        original = records._decode
        first = True

        def delayed(value):
            nonlocal first
            if first:
                first = False
                decode_started.set()
                self.assertTrue(continue_decode.wait(2))
            return original(value)

        with patch.object(records, "_decode", side_effect=delayed):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(lambda: records[:])
                self.assertTrue(decode_started.wait(2))
                records.append(row(3))
                continue_decode.set()
                self.assertEqual(result.result(timeout=2), [row(index) for index in range(3)])
        self.assertEqual(len(records), 4)

    def test_concurrent_append_and_read_remain_complete(self):
        records = CompletedRecords(hot_limit=7)
        barrier = threading.Barrier(5)

        def append_batch(worker):
            barrier.wait()
            for offset in range(75):
                records.append(row(worker * 75 + offset))
                current = records[-7:]
                self.assertTrue(all(item["usage"]["complete"] for item in current))
                self.assertLessEqual(records.hot_count, 7)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(append_batch, worker) for worker in range(4)]
            barrier.wait()
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(len(records), 300)
        self.assertEqual({item["id"] for item in records}, {str(index) for index in range(300)})
        self.assertEqual(records.hot_count, 7)

    def test_encoding_failure_is_atomic(self):
        records = CompletedRecords(hot_limit=1)
        first = row(0)
        records.append(first)
        invalid = [{"bad": object()}, {"bad": {1, 2}}, [row(1)]]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    records.append(value)
                self.assertEqual(len(records), 1)
                self.assertIs(records[0], first)
                self.assertEqual(records.hot_count, 1)
        with patch("tpm_records.json.dumps", side_effect=RuntimeError("encode failed")):
            with self.assertRaises(RuntimeError):
                records.append(row(1))
        self.assertEqual(list(records), [first])

    def test_nonfinite_raw_diagnostics_keep_existing_encoder_behavior(self):
        records = CompletedRecords(hot_limit=0)
        source = row(0)
        source["usage"]["raw"]["diagnostic"] = [float("nan"), float("inf"), float("-inf")]
        expected = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(records.append(source), expected)
        values = records[0]["usage"]["raw"]["diagnostic"]
        self.assertTrue(math.isnan(values[0]))
        self.assertEqual(values[1:], [float("inf"), float("-inf")])
        self.assertFalse(records[0]["usage"].get("missing_field", False))

    def test_failed_encoding_fallback_keeps_complete_record_after_hot_eviction(self):
        records = CompletedRecords(hot_limit=1)
        records.append(row(0))
        unusual = row(1)
        marker = object()
        unusual["unexpected_diagnostic"] = marker
        with self.assertRaises(TypeError):
            records.append(unusual)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records.retain_unencoded(unusual))
        self.assertEqual(len(records), 2)
        self.assertIs(records[-1], unusual)
        records.append(row(2))
        self.assertEqual(records.hot_count, 1)
        self.assertIs(records[1], unusual)
        self.assertIs(records[:][1]["unexpected_diagnostic"], marker)
        self.assertEqual([item["id"] for item in records], ["0", "1", "2"])
        self.assertEqual(records[1]["usage"], row(1)["usage"])

    def test_failed_encoding_fallback_without_hot_cache_preserves_cycles(self):
        records = CompletedRecords(hot_limit=0)
        unusual = row(0)
        unusual["unexpected_cycle"] = unusual
        with self.assertRaises(ValueError):
            records.append(unusual)
        self.assertEqual(len(records), 0)
        records.retain_unencoded(unusual)
        self.assertIs(records[0], unusual)
        self.assertIs(records[-1]["unexpected_cycle"], unusual)
        self.assertEqual(records.hot_count, 0)

    def test_live_metrics_crosses_cold_window_without_recounting(self):
        records, source = CompletedRecords(hot_limit=5), []
        compact_metrics, list_metrics = LiveMetrics(), LiveMetrics()
        for index in range(130):
            item = row(index)
            records.append(item)
            source.append(item)
            if index in (1, 17, 64, 129):
                compact_metrics.update(records)
                list_metrics.update(source)
                kwargs = {"now": index + .5, "formal_started_at": .001, "planned": 150,
                          "missed": 2, "in_flight": 3}
                self.assertEqual(compact_metrics.snapshot(**kwargs), list_metrics.snapshot(**kwargs))
                compact_metrics.update(records)
                self.assertEqual(compact_metrics.snapshot(**kwargs), list_metrics.snapshot(**kwargs))

    def test_first_metrics_update_after_thousand_mixed_records_matches_full_scan(self):
        records, source = CompletedRecords(), []
        for index in range(1000):
            item = row(index)
            if index % 17 == 0:
                item["phase"] = "calibration"
            if index % 5 == 0:
                item.update(success=False, error="http_429")
            if index % 7 == 0:
                item["usage"] = normalize_usage(None)
            elif index % 3 == 0:
                item["usage"] = normalize_usage({"input_tokens": 60, "cache_read_input_tokens": 30,
                                                 "cache_creation_input_tokens": 9, "output_tokens": 1},
                                                semantics="independent")
            records.append(item)
            source.append(item)
        self.assertEqual(records.hot_count, 100)
        compact_metrics, list_metrics = LiveMetrics(), LiveMetrics()
        compact_metrics.update(records)
        list_metrics.update(source)
        kwargs = {"now": 999.5, "formal_started_at": .001, "planned": 1200,
                  "missed": 3, "in_flight": 4}
        expected = reference_metrics(source, **kwargs)
        self.assertEqual(compact_metrics.snapshot(**kwargs), expected)
        self.assertEqual(list_metrics.snapshot(**kwargs), expected)
        with patch.object(records, "_decode", side_effect=AssertionError("already consumed rows decoded again")):
            compact_metrics.update(records)
            compact_metrics.update(records)
            self.assertEqual(compact_metrics.snapshot(**kwargs), expected)

    def test_snapshot_clone_is_independent_for_hot_and_cold_records(self):
        records = CompletedRecords(hot_limit=1)
        for index in range(3):
            records.append(row(index))
        snapshot = clone_snapshot({"requests": records[:]})
        for item in snapshot["requests"]:
            item["usage"]["raw"]["extra"].append("snapshot only")
            item["usage"]["full_input_tokens"] = -1
            item["timing"]["started_at"] = None
        self.assertEqual(list(records), [row(index) for index in range(3)])

    def test_hot_limit_validation_and_default_bound(self):
        for invalid in (-1, True, 1.5, "100"):
            with self.assertRaises(ValueError):
                CompletedRecords(hot_limit=invalid)
        records = CompletedRecords()
        for index in range(105):
            records.append(row(index))
        self.assertEqual(records.hot_count, 100)
        self.assertEqual(records.hot_limit, 100)


if __name__ == "__main__":
    unittest.main()
