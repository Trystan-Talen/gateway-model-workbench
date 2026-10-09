"""路由报告复用分位点时，保持统计精度、缺失值和成功口径。"""
import unittest
from unittest import mock

import routing_ab as ab


class RoutingStatisticsReuseTests(unittest.TestCase):
    def test_reports_compute_each_side_metric_once_and_keep_unrounded_delta(self):
        def records(values):
            return [{"index": index, "item_id": f"routing-ab-v1-{index:06d}",
                     "ok": True, "cached_tokens": None if index else 0,
                     "ttft_ms": value, "total_ms": value * 2}
                    for index, value in enumerate(values)]

        baseline = records([100.03, 100.041])
        candidate = records([100.138, 100.149])
        for builder in (ab.build_comparison, ab.build_live_comparison):
            with self.subTest(builder=builder.__name__), \
                    mock.patch.object(ab, "percentile", wraps=ab.percentile) as percentile:
                report = builder(baseline, candidate)
            # 报告显示值只有一位小数，差值必须基于原始分位点，不能用显示值重算。
            self.assertEqual(report["ttft"]["baseline_ms"], 100.0)
            self.assertEqual(report["ttft"]["candidate_ms"], 100.1)
            self.assertEqual(report["ttft"]["delta_pct"], 0.11)
            self.assertEqual(report["total_latency"]["delta_pct"], 0.11)
            self.assertEqual(percentile.call_count, 4)
            self.assertEqual(report["baseline"]["ttft_p95_ms"], report["ttft"]["baseline_ms"])
            self.assertEqual(report["candidate"]["total_p95_ms"], report["total_latency"]["candidate_ms"])
            self.assertEqual(report["cache"]["baseline_no_usage"], 1)

    def test_live_and_final_side_statistics_agree_for_incomplete_and_invalid_samples(self):
        records = [
            {"index": 0, "ok": True, "ttft_ms": 10, "total_ms": 20, "cached_tokens": 0},
            {"index": 1, "ok": False, "ttft_ms": 10000, "total_ms": 20000, "cached_tokens": 1024},
            {"index": 3, "class": "ok", "ttft_ms": 30, "total_ms": 40},
            {"index": 4, "status_code": 200, "completed": True,
             "ttft_ms": float("inf"), "total_ms": True, "cached_tokens": -1},
            {"index": 5, "status_code": 200, "completed": True,
             "ttft_ms": float("nan"), "total_ms": "missing", "cached_tokens": None},
        ]
        live = ab.build_live_comparison(records, [])
        final = ab.build_comparison(records, [])
        for side in ("baseline", "candidate"):
            self.assertEqual(live[side], final[side])
        self.assertEqual(final["baseline"]["ttft_p95_ms"], 29.0)
        self.assertEqual(final["baseline"]["total_p95_ms"], 39.0)
        self.assertEqual(final["baseline"]["success"]["successes"], 4)
        self.assertEqual(final["baseline"]["cache"]["total"], 3)
        self.assertEqual(final["baseline"]["cache"]["successes"], 1)
        self.assertIsNone(final["candidate"]["ttft_p95_ms"])


if __name__ == "__main__":
    unittest.main()
