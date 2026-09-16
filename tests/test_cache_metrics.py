#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缓存命中计量的回归测试。

三处口径必须完全一致，任何一处漂移都会让「智能路由 vs 固定路由」的缓存结论失真：

1. ``smart_route_load``：从上游 usage 采出 ``cached_tokens``（已有夹具测试覆盖）；
2. ``route_test_ui.Task``：逐条累加命中/未命中/无 usage 三个计数，并写进 JSONL；
3. ``analyze.cache_stats``：离线复算同一个命中率。

贯穿三条的判定只有一句：``usage.cached_tokens > 0`` 计一次命中；没有 usage 的请求
进 ``no_usage`` 桶，**不进分母**。把「网关不回 usage」算成未命中，会把兼容性差异
误读成缓存退化——这正是本模块最需要防住的错误。
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import analyze
import route_test_ui as ui


def make_task(steps=None):
    """构造一个可直接喂 record() 的 Task，不启动任何网络或后台线程。"""
    config = {
        "name": "cache-test", "url": "http://127.0.0.1:1/v1", "api_key": "k",
        "model": "m", "mode": "smoke", "rpm": 60000, "iterations": 1,
        "concurrency": 1, "read_timeout": 5,
    }
    steps = steps or [{"index": 1, "rpm": 60000, "count": 1, "duration": 1}]
    task = ui.Task(config, steps, 1)
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    task.out_path = path
    task._out = open(path, "a", encoding="utf-8", buffering=1)
    return task, path


def result(status_code=200, completed=True, cached_tokens=None, item_id=1):
    """构造一条 record() 能消费的结果记录。"""
    row = {
        "schema_version": 3, "ts": int(time.time() * 1000), "item_id": item_id,
        "mode": "smoke", "profile": "smoke", "step_index": 1, "grade": None,
        "status_code": status_code, "completed": completed, "error_type": None,
        "error_detail": None, "ttft_ms": 10, "total_ms": 100, "reason": None,
        "usage_seen": cached_tokens is not None, "usage_level": "NONE",
        "prompt_tokens": None, "cached_tokens": cached_tokens,
        "completion_tokens": None, "total_tokens": None,
        "cache_hit": None if cached_tokens is None else bool(cached_tokens > 0),
    }
    return row


class AnalyzeCacheStatsTests(unittest.TestCase):
    """离线复算：命中率的分母只算「真的带 cached_tokens」的记录。"""

    def test_hits_over_eligible_only(self):
        records = [result(cached_tokens=100), result(cached_tokens=0), result(cached_tokens=5)]
        stats = analyze.cache_stats(records)
        self.assertEqual(stats["eligible"], 3)
        self.assertEqual(stats["hits"], 2, "cached_tokens>0 才算命中")
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["no_usage"], 0)

    def test_records_without_usage_are_excluded_not_counted_as_miss(self):
        """没有 usage 的记录进 no_usage 桶，绝不能拉低命中率。"""
        records = [result(cached_tokens=10), result(cached_tokens=None), result(cached_tokens=None)]
        stats = analyze.cache_stats(records)
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["no_usage"], 2)
        self.assertEqual(stats["total"], 3)
        # 命中率按 1/1 算，而不是被稀释成 1/3。
        self.assertEqual(analyze.pct(stats["hits"], stats["eligible"]), "100.00%")

    def test_no_usable_records_yields_empty_stats(self):
        stats = analyze.cache_stats([result(), result()])
        self.assertEqual(stats["eligible"], 0)
        self.assertEqual(stats["no_usage"], 2)
        self.assertEqual(analyze.pct(stats["hits"], stats["eligible"]), "—")

    def test_zero_and_hundred_percent_endpoints(self):
        """0% 与 100% 都是合法结论，不能因为「没有变化」就显示成缺数据。"""
        miss = analyze.cache_stats([result(cached_tokens=0), result(cached_tokens=0)])
        self.assertEqual(analyze.pct(miss["hits"], miss["eligible"]), "0.00%")

        hit = analyze.cache_stats([result(cached_tokens=9), result(cached_tokens=9)])
        self.assertEqual(analyze.pct(hit["hits"], hit["eligible"]), "100.00%")


class TaskCacheAccumulatorTests(unittest.TestCase):
    """在线累加：Task.record 三个计数与 JSONL 落盘字段。"""

    def tearDown(self):
        # 用例里若忘记关闭，退出时清掉临时文件。
        for path in getattr(self, "_paths", []):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _task(self):
        task, path = make_task()
        self._paths = getattr(self, "_paths", []) + [path]
        self.addCleanup(task.close_output)
        return task, path

    def test_counts_split_into_hit_miss_and_no_usage(self):
        task, _ = self._task()
        task.record(result(cached_tokens=128, item_id=1))   # 命中
        task.record(result(cached_tokens=0, item_id=2))     # 未命中
        task.record(result(cached_tokens=None, item_id=3))  # 无 usage
        self.assertEqual(task.cache_stat, {"eligible": 2, "hits": 1, "no_usage": 1})

    def test_snap_exposes_rate_and_basis(self):
        task, _ = self._task()
        task.record(result(cached_tokens=64, item_id=1))
        task.record(result(cached_tokens=0, item_id=2))
        cache = task.snap()["cache"]
        self.assertEqual(cache["eligible"], 2)
        self.assertEqual(cache["hits"], 1)
        self.assertEqual(cache["no_usage"], 0)
        self.assertEqual(cache["rate_pct"], 50.0)
        self.assertEqual(cache["basis"], "usage.cached_tokens>0")

    def test_snap_rate_is_none_without_usable_samples(self):
        """没有任何带 usage 的样本时给 None，不能谎报 0%。"""
        task, _ = self._task()
        task.record(result(cached_tokens=None, item_id=1))
        cache = task.snap()["cache"]
        self.assertIsNone(cache["rate_pct"])
        self.assertEqual(cache["no_usage"], 1)

    def test_cache_fields_are_written_to_jsonl(self):
        """计量字段必须落盘，否则离线复算拿不到证据。"""
        task, path = self._task()
        task.record(result(cached_tokens=256, item_id=7))
        task.close_output()
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["cached_tokens"], 256)
        self.assertTrue(row["usage_seen"])
        self.assertTrue(row["cache_hit"])
        self.assertEqual(row["item_id"], 7)

    def test_jsonl_omits_raw_payload_unless_capture_enabled(self):
        """未开启原始记录时，键名本身就不该出现——避免误以为存了正文。"""
        task, path = self._task()
        task.record(result(cached_tokens=1, item_id=1))
        task.close_output()
        with open(path, encoding="utf-8") as handle:
            row = json.loads(handle.readline())
        self.assertNotIn("raw_input", row)
        self.assertNotIn("raw_output", row)

    def test_offline_recompute_matches_online_accumulator(self):
        """在线累加与离线复算必须是同一个结论——两处口径漂移会让报告自相矛盾。"""
        task, path = self._task()
        samples = [512, 0, None, 32, None, 0, 1024]
        for index, cached in enumerate(samples):
            task.record(result(cached_tokens=cached, item_id=index))
        online = task.snap()["cache"]
        task.close_output()
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        offline = analyze.cache_stats(rows)
        self.assertEqual(online["eligible"], offline["eligible"])
        self.assertEqual(online["hits"], offline["hits"])
        self.assertEqual(online["no_usage"], offline["no_usage"])


class CacheUiScriptTests(unittest.TestCase):
    """前端脚本必须真的把 cache 数据渲染出来，而不是只存在于接口里。"""

    def test_page_script_mentions_cache_metric(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "route_test_ui.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"cache"', source)
        self.assertIn("usage.cached_tokens>0", source)


if __name__ == "__main__":
    unittest.main()
