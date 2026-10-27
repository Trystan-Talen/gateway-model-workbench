"""增量统计与完整历史基准对照；全部数据为合成记录。"""
import copy
import random
import unittest

from tpm_metrics import LiveMetrics
from tpm_report import metrics


def record(index=0, **overrides):
    row = {"phase": "formal", "started_at": 1000 + index, "ended_at": 1001 + index,
           "success": True, "usage": {"complete": True, "full_input_tokens": 49500,
                                      "output_tokens": 500, "total_tokens": 50000,
                                      "cache_read_tokens": 47025, "cache_write_tokens": 0},
           "ttft_seconds": .01, "latency_seconds": 1.0,
           "schedule_delay_seconds": .002, "pool_wait_seconds": .0001,
           "error": None, "output_limited": False}
    row.update(overrides)
    return row


class CountingList(list):
    """访问计数确保性能性质，避免依赖机器速度的脆弱耗时断言。"""
    reads = 0

    def __getitem__(self, item):
        self.reads += 1
        return super().__getitem__(item)

    def __iter__(self):
        raise AssertionError("incremental statistics must not rescan the record list")


class LiveMetricsTests(unittest.TestCase):
    def assert_matches(self, live, records, **arguments):
        self.assertEqual(live.snapshot(**arguments), metrics(records, **arguments))

    def test_random_incremental_results_equal_full_history_with_clock_rollback(self):
        rng = random.Random(75123)
        rows, live = [], LiveMetrics()
        for batch in range(24):
            for _ in range(rng.randint(1, 24)):
                index = rng.randrange(180)
                row = record(index, phase=rng.choice(["formal"] * 5 + ["calibration", "warmup"]),
                             success=rng.random() > .2, started_at=1000 + index / 3,
                             ended_at=1000 + rng.randrange(180) / 2,
                             error=rng.choice([None, "http_429", "timeout", "stream_error"]),
                             output_limited=rng.random() < .1)
                for field in ("ttft_seconds", "latency_seconds", "schedule_delay_seconds", "pool_wait_seconds"):
                    row[field] = rng.choice([None, 0, rng.random() * 4])
                row["usage"]["complete"] = rng.random() > .2
                for field in ("cache_read_tokens", "cache_write_tokens"):
                    row["usage"][field] = rng.choice([None, 0, rng.randrange(1000)])
                rows.append(row)
            live.update(rows)
            for now in (990, 1000, 1030, 1090, 1200, 1060):
                self.assert_matches(live, rows, now=now, formal_started_at=1000,
                                    planned=8003, missed=batch, in_flight=7)

    def test_strict_lower_and_inclusive_upper_boundaries_for_both_windows(self):
        rows = [record(i, started_at=start, ended_at=end) for i, (start, end) in enumerate([
            (1050, 1000), (1050.001, 1000.001), (1060, 1060), (1060.001, 1060.001)])]
        live = LiveMetrics()
        live.update(rows)
        self.assert_matches(live, rows, now=1060, formal_started_at=1000)
        data = live.snapshot(now=1060, formal_started_at=1000)
        self.assertEqual(data["full_token_tpm"], 100000)
        self.assertEqual(data["actual_start_rps"], .2)
        self.assertTrue(data["window_full"])
        self.assert_matches(live, rows, now=1059.999, formal_started_at=1000)
        self.assertFalse(live.snapshot(now=1059.999, formal_started_at=1000)["window_full"])

    def test_unknown_cache_is_not_zero_and_failure_usage_does_not_become_throughput(self):
        rows = [record(0), record(1, success=False, error="timeout")]
        rows[0]["usage"].update(cache_read_tokens=None, cache_write_tokens=None)
        live = LiveMetrics()
        live.update(rows)
        self.assert_matches(live, rows, now=1010, formal_started_at=1000)
        data = live.snapshot(now=1010, formal_started_at=1000)
        self.assertEqual(data["full_token_tpm"], 50000)
        self.assertIsNone(data["cache_read_tpm"])
        self.assertIsNone(data["cache_write_tpm"])
        self.assertEqual(data["cache_hit_rate"], .95)
        self.assertEqual(data["timeouts"], 1)
        self.assertEqual(live.snapshot(now=1070)["cache_read_tpm"], 0)

    def test_duplicates_out_of_order_and_clock_rollback_do_not_discard_evidence(self):
        rows, live = [], LiveMetrics()
        for ended_at in (1100, 1000, 1050, 1050, 999, 1100):
            rows.append(record(0, ended_at=ended_at))
            live.update(rows)
            for now in (1100, 1050, 1000, 1200, 1050):
                self.assert_matches(live, rows, now=now, formal_started_at=990)

    def test_repeated_update_does_not_double_count_and_list_replacement_or_shrink_resets(self):
        rows, live = [record(i) for i in range(20)], LiveMetrics()
        live.update(rows)
        live.update(rows)
        self.assert_matches(live, rows, now=1050)
        del rows[7:]
        live.update(rows)
        self.assert_matches(live, rows, now=1050)
        replacement = [record(30, success=False, error="http_429")]
        live.update(replacement)
        self.assert_matches(live, replacement, now=1050)
        live.update([])
        self.assert_matches(live, [], now=1050)

    def test_missing_duration_zero_and_exact_percentile_interpolation(self):
        rows = [record(i, ttft_seconds=value, latency_seconds=value)
                for i, value in enumerate((None, 0, 1, 2, 8))]
        live = LiveMetrics()
        live.update(rows)
        self.assert_matches(live, rows, now=1060)
        data = live.snapshot(now=1060)
        self.assertEqual(data["ttft_p50"], 1.5)
        self.assertAlmostEqual(data["latency_p95"], 7.1)
        for start in (None, 0, 1060, 1070):
            self.assert_matches(live, rows, now=1060, formal_started_at=start)

    def test_updates_read_only_new_records_and_queries_read_no_records(self):
        rows, live = CountingList(record(i) for i in range(8000)), LiveMetrics()
        live.update(rows)
        self.assertEqual(rows.reads, 8000)
        for _ in range(12):
            live.update(rows)
            live.snapshot(now=9000, formal_started_at=1000)
        self.assertEqual(rows.reads, 8000)
        rows.append(record(8000))
        live.update(rows)
        self.assertEqual(rows.reads, 8001)

    def test_snapshots_and_updates_do_not_expose_mutable_aliases(self):
        rows, live = [record(success=False, error="http_429")], LiveMetrics()
        original = copy.deepcopy(rows)
        live.update(rows)
        first = live.snapshot(now=1010)
        preserved = copy.deepcopy(first)
        first["errors"]["http_429"] = 10000
        first["started"] = 99999
        self.assertEqual(live.snapshot(now=1010), preserved)
        self.assertEqual(rows, original)
        previous_snapshot = live.snapshot(now=1010)
        rows.append(record(2))
        live.update(rows)
        self.assertEqual(previous_snapshot, preserved)
        self.assertEqual(live.snapshot(now=1010)["started"], 2)


if __name__ == "__main__":
    unittest.main()
