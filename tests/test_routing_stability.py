#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按请求清单序号统计连续失败与首字耗时平滑峰值。"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing_ab as ab  # noqa: E402


def record(index, ok=True, ttft=100):
    return {"index": index, "item_id": f"routing-ab-v1-{index:06d}",
            "status_code": 200 if ok else 429, "completed": ok,
            "ttft_ms": ttft}


class RequestOrderTests(unittest.TestCase):
    def test_explicit_index_precedes_legacy_id_and_compatibility_field(self):
        result = record(3)
        result.update({"index": 0, "request_index": 7})
        self.assertEqual(ab.request_index(result), 0)
        del result["index"]
        self.assertEqual(ab.request_index(result), 7)

    def test_legacy_corpus_id_recovers_order_without_using_completion_order(self):
        records = [record(5), record(1), record(3)]
        for result in records:
            del result["index"]
        self.assertEqual([ab.request_index(result) for result in ab.ordered_records(records)], [1, 3, 5])

    def test_invalid_index_is_not_a_number_and_unknown_ids_stay_unknown(self):
        for index in (True, -1, 1.5, "1", None):
            with self.subTest(index=index):
                self.assertIsNone(ab.request_index({"index": index, "item_id": "unknown-000001"}))
        self.assertIsNone(ab.request_index({"item_id": "routing-ab-v1-000001\n"}))
        self.assertIsNone(ab.request_index({"item_id": None}))
        self.assertIsNone(ab.request_index({"item_id": 123}))
        self.assertIsNone(ab.request_index({"item_id": "routing-ab-v1-" + "1" * 5000}))
        unknown = {"item_id": "unknown"}
        self.assertEqual(ab.ordered_records([unknown, record(2), record(0)]), [record(0), record(2), unknown])


class FailureSequenceTests(unittest.TestCase):
    def test_concurrent_completion_order_does_not_change_failure_run(self):
        results = [record(0, False), record(1, False), record(2, False),
                   record(3), record(4, False), record(5, False)]
        for completion_order in (results, results[::-1], [results[i] for i in (3, 1, 4, 2, 5, 0)]):
            with self.subTest(order=[item["index"] for item in completion_order]):
                self.assertEqual(ab.sequence_stability(completion_order)["max_consecutive_failures"], 3)

    def test_pending_index_breaks_failures_until_result_arrives(self):
        results = [record(0, False), record(2, False), record(3, False)]
        self.assertEqual(ab.sequence_stability(results)["max_consecutive_failures"], 2)
        results.append(record(1, False))
        self.assertEqual(ab.sequence_stability(results)["max_consecutive_failures"], 4)

    def test_unknown_order_is_undefined_and_all_success_is_zero(self):
        stats = ab.sequence_stability([{"status_code": 429}, {"ok": False}])
        self.assertIsNone(stats["max_consecutive_failures"])
        self.assertEqual(stats["unordered_requests"], 2)
        self.assertEqual(ab.sequence_stability([record(i) for i in range(3)])["max_consecutive_failures"], 0)
        self.assertIsNone(ab.sequence_stability([])["max_consecutive_failures"])

    def test_duplicate_indices_are_excluded_and_break_sequence(self):
        results = [record(0, False), record(1, False), record(2, False),
                   record(2, False), record(3, False), record(4, False)]
        stats = ab.sequence_stability(results)
        self.assertEqual(stats["max_consecutive_failures"], 2)
        self.assertEqual(stats["ordered_requests"], 4)
        self.assertEqual(stats["excluded_duplicate_requests"], 2)
        duplicates_only = ab.sequence_stability([record(2, False), record(2, False)])
        self.assertIsNone(duplicates_only["max_consecutive_failures"])

    def test_failure_definition_matches_success_rate_including_incomplete_200(self):
        results = [dict(record(0), completed=False), record(1, False),
                   dict(record(2), completed=False, error_type="TIMEOUT"), record(3)]
        report = ab.build_live_comparison(results, [])
        self.assertEqual(report["baseline"]["success"]["successes"], 1)
        self.assertEqual(report["baseline"]["max_consecutive_failures"], 3)


class TtftEwmaTests(unittest.TestCase):
    def test_hand_calculated_five_sample_window_and_sliding_window(self):
        # 100 -> 150 -> 225 -> 312.5 -> 406.25；下一窗口最终值为 506.25。
        results = [record(i, ttft=(i + 1) * 100) for i in range(6)]
        stats = ab.sequence_stability(results[:5][::-1])
        self.assertEqual(stats["ttft_ewma_max_ms"], round(406.25, 1))
        self.assertEqual(stats["ttft_ewma_windows"], 1)
        self.assertEqual(ab.sequence_stability(results[::-1])["ttft_ewma_max_ms"], round(506.25, 1))
        self.assertEqual(ab.sequence_stability(results)["ttft_ewma_windows"], 2)

    def test_only_complete_window_final_values_participate_in_maximum(self):
        results = [record(i, ttft=1000 if i == 0 else 0) for i in range(6)]
        stats = ab.sequence_stability(results)
        self.assertEqual(stats["ttft_ewma_max_ms"], 62.5)
        self.assertEqual(stats["ttft_ewma_windows"], 2)

    def test_fewer_than_five_is_undefined_and_five_zero_values_are_valid(self):
        for count in range(5):
            self.assertIsNone(ab.sequence_stability([record(i, ttft=100) for i in range(count)])["ttft_ewma_max_ms"])
        stats = ab.sequence_stability([record(i, ttft=0) for i in range(5)])
        self.assertEqual(stats["ttft_ewma_max_ms"], 0.0)

    def test_failure_missing_ttft_and_non_finite_values_reset_window(self):
        for invalid in (None, "100", True, -1, math.nan, math.inf, -math.inf, 10 ** 1000):
            with self.subTest(ttft=invalid):
                results = [record(i, ttft=100) for i in range(7)]
                results[3]["ttft_ms"] = invalid
                stats = ab.sequence_stability(results)
                self.assertIsNone(stats["ttft_ewma_max_ms"])
                self.assertEqual(stats["ttft_eligible_requests"], 6)
        results = [record(i, ttft=100) for i in range(7)]
        results[3] = record(3, False, ttft=10000)
        self.assertIsNone(ab.sequence_stability(results)["ttft_ewma_max_ms"])

    def test_gap_and_duplicate_indices_break_ewma_window(self):
        results = [record(i, ttft=100) for i in (0, 1, 2, 4, 5, 6, 7)]
        self.assertIsNone(ab.sequence_stability(results)["ttft_ewma_max_ms"])
        results.append(record(3, ttft=100))
        self.assertEqual(ab.sequence_stability(results)["ttft_ewma_max_ms"], 100.0)
        results.append(record(3, ttft=100))
        self.assertIsNone(ab.sequence_stability(results)["ttft_ewma_max_ms"])

    def test_new_valid_window_after_failure_does_not_inherit_previous_values(self):
        results = [record(i, ttft=1000) for i in range(4)]
        results.append(record(4, False, ttft=10000))
        results.extend(record(i, ttft=20) for i in range(5, 10))
        stats = ab.sequence_stability(results)
        self.assertEqual(stats["ttft_ewma_max_ms"], 20.0)
        self.assertEqual(stats["ttft_ewma_windows"], 1)


class StabilityReportTests(unittest.TestCase):
    def test_live_and_final_reports_use_identical_protocol_and_values(self):
        base = [record(i, ttft=(i + 1) * 100) for i in range(6)]
        candidate = [record(i, ok=i not in (0, 1, 2), ttft=20) for i in range(6)]
        live = ab.build_live_comparison(base, candidate)
        final = ab.build_comparison(base, candidate)
        self.assertEqual(live["stability"], final["stability"])
        for report in (live, final):
            self.assertEqual(report["baseline"]["max_consecutive_failures"], 0)
            self.assertEqual(report["candidate"]["max_consecutive_failures"], 3)
            self.assertEqual(report["baseline"]["ttft_ewma_max_ms"], round(506.25, 1))
            self.assertIsNone(report["candidate"]["ttft_ewma_max_ms"])
            self.assertEqual(report["candidate"]["stability"], report["stability"]["candidate"])
            self.assertEqual(report["method"]["statistics_protocol"], "routing-stability-v1")
            protocol = report["stability"]["protocol"]
            self.assertEqual(protocol["order_by"], "index")
            self.assertEqual(protocol["missing_result_policy"], "break_sequence")
            self.assertEqual(protocol["ttft_ewma"]["window_size"], 5)
            self.assertEqual(protocol["ttft_ewma"]["alpha"], .5)
            self.assertEqual(protocol["ttft_ewma"]["seed"], "first_sample")
        self.assertIn("最大连续失败次数", final["metrics"])
        self.assertTrue(any("最大 EWMA" in label for label in final["metrics"]))

    def test_protocol_metadata_is_returned_as_independent_objects(self):
        protocol = ab.stability_protocol()
        protocol["ttft_ewma"]["alpha"] = 0
        self.assertEqual(ab.stability_protocol()["ttft_ewma"]["alpha"], .5)


if __name__ == "__main__":
    unittest.main()
