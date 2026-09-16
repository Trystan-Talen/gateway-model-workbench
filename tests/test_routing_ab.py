#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照统计（routing_ab）的回归测试。

这个模块决定报告里写「提升」还是写「观测不足」。一旦算术错了，上层会拿一个看起来
很确定、其实没有依据的差值去下结论——而用户是要拿这个结论去判断公司路由好坏的。
所以这里的断言全部对着**已发表的参考值**，不是对着当前实现回抄。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing_ab as ab  # noqa: E402


class WilsonIntervalTests(unittest.TestCase):
    """Wilson 区间：对照已知数值与边界行为。"""

    def test_matches_published_value_for_half(self):
        low, high = ab.wilson_interval(50, 100)
        self.assertAlmostEqual(low, 0.4038, places=4)
        self.assertAlmostEqual(high, 0.5962, places=4)

    def test_stays_inside_unit_interval_at_extremes(self):
        """p 贴近 0/1 时不能给出负数或大于 1 的边界——报告里出现这种值等于自曝其短。"""
        for successes, total in ((0, 10), (10, 10), (0, 1), (1, 1)):
            with self.subTest(successes=successes, total=total):
                low, high = ab.wilson_interval(successes, total)
                self.assertGreaterEqual(low, 0.0)
                self.assertLessEqual(high, 1.0)

    def test_zero_total_is_undefined_not_zero(self):
        self.assertEqual(ab.wilson_interval(0, 0), (None, None))

    def test_narrower_with_more_samples(self):
        small = ab.wilson_interval(50, 100)
        large = ab.wilson_interval(500, 1000)
        self.assertLess(large[1] - large[0], small[1] - small[0])


class FisherExactTests(unittest.TestCase):
    """Fisher 精确检验：对着教科书表值。"""

    def test_tea_tasting_table(self):
        self.assertAlmostEqual(ab.fisher_exact_two_sided(3, 1, 1, 3), 0.4857, places=3)

    def test_strong_asymmetry_is_significant(self):
        self.assertAlmostEqual(ab.fisher_exact_two_sided(1, 9, 9, 1), 0.00109, places=4)

    def test_perfect_separation(self):
        self.assertLess(ab.fisher_exact_two_sided(10, 0, 0, 10), 0.0001)

    def test_identical_groups_are_not_significant(self):
        self.assertGreater(ab.fisher_exact_two_sided(5, 5, 5, 5), 0.9)

    def test_degenerate_tables_return_one(self):
        """全成功或全失败的表没有信息，不能返回 0 假装发现了差异。"""
        for table in ((10, 0, 10, 0), (0, 5, 0, 5), (0, 0, 0, 0), (5, 0, 0, 0)):
            with self.subTest(table=table):
                self.assertEqual(ab.fisher_exact_two_sided(*table), 1.0)


class McNemarTests(unittest.TestCase):
    """配对精确检验：只看不一致的两格。"""

    def test_known_value(self):
        self.assertAlmostEqual(ab.mcnemar_exact(1, 9)["p_value"], 0.021484, places=5)

    def test_no_discordant_pairs_is_uninformative(self):
        self.assertEqual(ab.mcnemar_exact(0, 0)["p_value"], 1.0)

    def test_symmetric_discordance_is_not_significant(self):
        self.assertGreater(ab.mcnemar_exact(5, 5)["p_value"], 0.9)

    def test_discordant_count_is_reported(self):
        self.assertEqual(ab.mcnemar_exact(7, 3)["discordant"], 10)


class CompareRatesTests(unittest.TestCase):
    """归因判定：不明白说出来的差异必须被标成「不足以归因」。"""

    def test_overlapping_intervals_are_not_attributable(self):
        """小样本上的 2 个百分点差距不能宣布为路由优势。"""
        outcome = ab.compare_rates(92, 100, 94, 100)
        self.assertEqual(outcome["attribution"], ab.ATTRIBUTION_WEAK)
        self.assertTrue(outcome["ci_overlap"])
        self.assertIn("不足以", outcome["note"])

    def test_large_consistent_gap_is_attributable(self):
        outcome = ab.compare_rates(800, 1000, 950, 1000)
        self.assertEqual(outcome["attribution"], ab.ATTRIBUTION_OK)
        self.assertFalse(outcome["ci_overlap"])
        self.assertLess(outcome["p_value"], 0.05)
        self.assertGreater(outcome["delta_pct"], 0)

    def test_missing_side_is_unknown(self):
        outcome = ab.compare_rates(0, 0, 5, 10)
        self.assertEqual(outcome["attribution"], ab.ATTRIBUTION_UNKNOWN)

    def test_direction_of_change_is_signed(self):
        """智能路由变差时必须报负数，不能只报绝对值。"""
        outcome = ab.compare_rates(950, 1000, 800, 1000)
        self.assertLess(outcome["delta_pct"], 0)


class PairingTests(unittest.TestCase):
    """配对：缺一侧的请求必须整条剔除，不能当成失败。"""

    def test_unpaired_items_are_dropped_not_counted_as_failures(self):
        pairs = [
            {"item_id": "a", "baseline_ok": True, "candidate_ok": True},
            {"item_id": "b", "baseline_ok": False, "candidate_ok": None},
        ]
        result = ab.pair_outcomes(pairs)
        self.assertEqual(result["paired_total"], 1)
        self.assertEqual(result["both_ok"], 1)
        self.assertEqual(result["both_fail"], 0)
        self.assertEqual(result["unpaired_count"], 1)
        self.assertEqual(result["unpaired"], ["b"])


class CacheRateTests(unittest.TestCase):
    """缓存口径：与 analyze / 在线累加器三方一致，无 usage 不进分母。"""

    def test_no_usage_records_are_excluded_from_denominator(self):
        records = [
            {"cached_tokens": 1024},
            {"cached_tokens": None},
            {"cached_tokens": 0},
        ]
        stats = ab.cache_hit_rate(records)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["eligible"], 2)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["no_usage"], 1)

    def test_zero_is_a_real_miss_and_stays_in_denominator(self):
        """真的 0 与拿不到 usage 必须区分开——这是整个缓存指标的前提。"""
        stats = ab.cache_hit_rate([{"cached_tokens": 0}, {"cached_tokens": 0}])
        self.assertEqual(stats["eligible"], 2)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["no_usage"], 0)

    def test_cache_comparison_reports_drop_when_candidate_worse(self):
        """智能路由切资源导致缓存命中率下降时，报告要如实给出负的差值。"""
        baseline = [{"cached_tokens": 512} for _ in range(80)] + [{"cached_tokens": 0} for _ in range(20)]
        candidate = [{"cached_tokens": 512} for _ in range(40)] + [{"cached_tokens": 0} for _ in range(60)]
        outcome = ab.compare_cache(baseline, candidate)
        self.assertAlmostEqual(outcome["baseline"]["pct"], 80.0, places=2)
        self.assertAlmostEqual(outcome["candidate"]["pct"], 40.0, places=2)
        self.assertLess(outcome["delta_pct"], 0)
        self.assertEqual(outcome["attribution"], ab.ATTRIBUTION_OK)

    def test_cache_comparison_flags_incomparable_sides(self):
        """一侧完全拿不到 usage 时，必须说「不可比」，不能说「命中率 0%」。"""
        outcome = ab.compare_cache([{"cached_tokens": 512}], [{"cached_tokens": None}])
        self.assertEqual(outcome["attribution"], ab.ATTRIBUTION_UNKNOWN)
        self.assertIn("不可比", outcome["note"])
        self.assertIsNone(outcome["candidate"]["pct"])

    def test_no_usage_counts_are_surfaced(self):
        outcome = ab.compare_cache(
            [{"cached_tokens": 512}, {"cached_tokens": None}],
            [{"cached_tokens": 512}, {"cached_tokens": None}])
        self.assertEqual(outcome["baseline_no_usage"], 1)
        self.assertEqual(outcome["candidate_no_usage"], 1)
        self.assertIn("排除", outcome["note"])


class LatencyTests(unittest.TestCase):
    def test_percentile_matches_linear_interpolation(self):
        self.assertAlmostEqual(ab.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], .95), 9.55, places=3)

    def test_percentile_ignores_non_numeric(self):
        """JSONL 里混进 null 或字符串时不能崩，也不能把它们当成 0 拉低分位数。"""
        self.assertAlmostEqual(ab.percentile([1, None, "x", 3], .5), 2.0, places=3)

    def test_empty_percentile_is_none(self):
        self.assertIsNone(ab.percentile([], .5))

    def test_latency_delta_is_signed(self):
        outcome = ab.compare_latency([100, 200], [300, 400], .5)
        self.assertGreater(outcome["delta_pct"], 0)


class BuildComparisonTests(unittest.TestCase):
    """端到端：把两侧逐条结果拼成报告。"""

    @staticmethod
    def _record(item_id, ok, cached=None, ttft=100.0, total=200.0):
        return {"item_id": item_id, "ok": ok, "status_code": 200 if ok else 500,
                "completed": True, "cached_tokens": cached,
                "ttft_ms": ttft, "total_ms": total}

    def test_pairs_by_item_id_and_reports_every_metric(self):
        baseline, candidate = [], []
        for index in range(40):
            item_id = f"routing-ab-v1-{index:06d}"
            baseline.append(self._record(item_id, ok=index % 10 != 0, cached=512 if index % 2 else 0))
            candidate.append(self._record(item_id, ok=True, cached=0))
        report = ab.build_comparison(baseline, candidate)
        self.assertEqual(report["baseline"]["requests"], 40)
        self.assertEqual(report["reliability"]["paired_total"], 40)
        self.assertEqual(report["reliability"]["unpaired_count"], 0)
        self.assertIn("缓存命中率", report["metrics"])
        self.assertLess(report["cache"]["delta_pct"], 0)
        self.assertEqual(report["method"]["cache_basis"], "usage.cached_tokens>0")

    def test_shared_ids_only_are_paired(self):
        baseline = [self._record("a", True), self._record("b", True)]
        candidate = [self._record("b", False), self._record("c", True)]
        report = ab.build_comparison(baseline, candidate)
        self.assertEqual(report["reliability"]["paired_total"], 1)
        self.assertEqual(report["reliability"]["unpaired_count"], 0)
        self.assertEqual(report["reliability"]["mcnemar"]["discordant"], 1)

    def test_uncertain_outcome_adds_a_limitation(self):
        """未通过归因判定时，限制说明里必须出现「不要据此宣布」的字样。"""
        baseline = [self._record(f"x{i}", i % 2 == 0) for i in range(50)]
        candidate = [self._record(f"x{i}", i % 2 == 0) for i in range(50)]
        report = ab.build_comparison(baseline, candidate)
        self.assertEqual(report["success"]["attribution"], ab.ATTRIBUTION_WEAK)
        self.assertTrue(any("不要据此" in item for item in report["limitations"]))

    def test_class_field_is_understood(self):
        """结果字典可能用 ``class`` 而不是 ``ok``，两种都要能读。"""
        baseline = [{"item_id": "a", "class": "ok", "cached_tokens": 5}]
        candidate = [{"item_id": "a", "class": "r5xx", "cached_tokens": 0}]
        report = ab.build_comparison(baseline, candidate)
        self.assertEqual(report["baseline"]["success"]["successes"], 1)
        self.assertEqual(report["candidate"]["success"]["successes"], 0)


if __name__ == "__main__":
    unittest.main()
