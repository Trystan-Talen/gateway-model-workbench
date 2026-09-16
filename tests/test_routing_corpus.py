#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照测试集（routing_corpus）的回归测试。

这个测试集是「智能路由 vs 固定路由」结论的地基：如果它不是确定性的、分档之间不是
严格可比、同一段内前缀不一致，那么上层算出来的缓存命中率差异就没有意义。所以这里
盯住的都是**会让结论失真的性质**，不是普通的功能点。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing_corpus as rc  # noqa: E402


class CorpusDeterminismTests(unittest.TestCase):
    """确定性：同一版本、同一档位，任何时间生成都必须逐字节一致。"""

    def test_same_item_is_reproducible(self):
        first = rc.build_item(123)
        second = rc.build_item(123)
        self.assertEqual(first, second)
        self.assertEqual(first["prefix_sha256"], second["prefix_sha256"])

    def test_corpus_is_reproducible(self):
        self.assertEqual(rc.build_corpus(1000), rc.build_corpus(1000))

    def test_prefix_cache_does_not_change_results(self):
        """内部前缀缓存不能改变结果——否则先算哪条会影响后算哪条。"""
        cold = rc._PREFIX_CACHE.copy()
        try:
            rc._PREFIX_CACHE.clear()
            without_cache = [rc.build_item(i)["prefix_sha256"] for i in range(60)]
        finally:
            rc._PREFIX_CACHE.clear()
            rc._PREFIX_CACHE.update(cold)
        with_cache = [rc.build_item(i)["prefix_sha256"] for i in range(60)]
        self.assertEqual(without_cache, with_cache)


class CorpusTierTests(unittest.TestCase):
    """分档：高档位必须严格包含低档位，否则跨档位比较不成立。"""

    def test_tier_is_a_prefix_of_larger_tier(self):
        small = rc.build_corpus(1000)
        large = rc.build_corpus(3000)
        self.assertEqual(small, large[:1000])

    def test_item_ids_are_stable_across_tiers(self):
        """同一条请求在任何档位里都必须是同一个 item_id —— A/B 配对全靠它。"""
        self.assertEqual(rc.build_item(999)["item_id"], rc.build_corpus(3000)[999]["item_id"])
        self.assertEqual(rc.build_item(0)["item_id"], rc.build_item(0)["item_id"])

    def test_tier_counts(self):
        self.assertEqual(len(rc.build_corpus(1000)), 1000)
        self.assertEqual(len(rc.build_corpus(5000)), 5000)

    def test_unknown_tier_is_rejected(self):
        for bad in (0, 500, 12345, -1):
            with self.subTest(tier=bad):
                with self.assertRaises(ValueError):
                    rc.build_corpus(bad)

    def test_iter_corpus_matches_build_corpus(self):
        self.assertEqual(list(rc.iter_corpus(1000)), rc.build_corpus(1000))

    def test_tiers_are_ordered_and_all_choices(self):
        self.assertEqual(list(rc.TIERS), sorted(rc.TIERS))
        self.assertEqual(len(rc.all_tier_summaries()), len(rc.TIERS))


class CorpusCacheShapeTests(unittest.TestCase):
    """缓存纹理：同一段内共享前缀、不同段前缀不同 —— 否则命中率指标测不出东西。"""

    def test_items_in_same_burst_share_the_same_prefix(self):
        """同段内前缀必须逐字节相同。这是缓存有戏的前提，不能被任何改动破坏。"""
        burst = [rc.build_item(i) for i in range(rc.BURST_SIZE)]
        fingerprints = {item["prefix_sha256"] for item in burst}
        self.assertEqual(len(fingerprints), 1, "同一段内的请求前缀不一致，缓存无法命中")
        # 但结尾必须不同，否则请求会被当成完全相同的重放。
        self.assertEqual(len({item["prompt"] for item in burst}), len(burst))

    def test_different_bursts_have_different_prefixes(self):
        """不同段前缀必须不同；否则整轮请求共用一个前缀，两条路由都会满命中。"""
        first = rc.build_item(0)["prefix_sha256"]
        second = rc.build_item(rc.BURST_SIZE)["prefix_sha256"]
        third = rc.build_item(rc.BURST_SIZE * 2)["prefix_sha256"]
        self.assertEqual(len({first, second, third}), 3)

    def test_prefix_lengths_cycle_through_all_variants(self):
        """前缀长度要覆盖短/中/长，避免只测到一种缓存行为。"""
        seen = set()
        for burst in range(len(rc.PREFIX_LENGTHS) * 2):
            seen.add(rc.build_item(burst * rc.BURST_SIZE)["shared_prefix_chars"])
        # 允许同长度档位因换行拼接产生少量误差，只要数量级不同即可。
        self.assertGreaterEqual(len(seen), len(rc.PREFIX_LENGTHS))

    def test_shared_prefix_is_actually_long_enough_to_cache(self):
        """前缀要足够长才有缓存价值；太短则前缀缓存不会启用，指标同样失真。"""
        self.assertGreaterEqual(rc.build_item(0)["shared_prefix_chars"], 300)

    def test_burst_index_matches_position(self):
        for index in (0, 1, rc.BURST_SIZE - 1, rc.BURST_SIZE, rc.BURST_SIZE * 3 + 7):
            with self.subTest(index=index):
                self.assertEqual(rc.build_item(index)["burst_index"], index // rc.BURST_SIZE)


class CorpusManifestTests(unittest.TestCase):
    """清单：报告里必须能写明测试集身份，且不能把正文塞进历史索引。"""

    def test_manifest_exposes_no_prompt_text(self):
        text = json.dumps(rc.corpus_manifest(1000), ensure_ascii=False)
        self.assertNotIn("基线资料", text, "清单里混入了请求正文，历史索引会被撑大")

    def test_manifest_is_stable_and_tier_specific(self):
        self.assertEqual(rc.corpus_manifest(1000), rc.corpus_manifest(1000))
        self.assertNotEqual(rc.corpus_manifest(1000)["item_id_digest"],
                            rc.corpus_manifest(3000)["item_id_digest"])

    def test_manifest_records_version(self):
        manifest = rc.corpus_manifest(1000)
        self.assertEqual(manifest["id"], rc.CORPUS_VERSION)
        self.assertEqual(manifest["tier"], 1000)


class CorpusSafetyTests(unittest.TestCase):
    """安全：测试集是合成语料，不能夹带密钥样式的内容。"""

    def test_no_secret_like_strings(self):
        blob = "".join(item["prompt"] for item in rc.build_corpus(1000))
        for marker in ("sk-", "Bearer ", "api_key", "Authorization"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, blob)


if __name__ == "__main__":
    unittest.main()
