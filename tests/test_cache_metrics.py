#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缓存命中计量的回归测试。

三处口径必须完全一致，任何一处漂移都会让「智能路由 vs 固定路由」的缓存结论失真：

1. ``smart_route_load``：从上游 usage 采出 ``cached_tokens``（已有夹具测试覆盖）；
2. ``route_test_ui.Task``：逐条累加命中/未命中/无 usage 三个计数，并写进 JSONL；
3. ``analyze.cache_stats``：离线复算同一个命中率。

贯穿三条的判定只有一句：``usage.cached_tokens > 0`` 计一次命中；请求命中率分母是
总请求数，没有 usage 的请求按未命中计入分母，同时进 ``no_usage`` 桶，避免把「命中
可统计率」误当成用户要求的总请求命中率。
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
    """离线复算：请求命中率分母是全部请求，Token 比例另行要求完整用量。"""

    def test_hits_over_eligible_only(self):
        records = [result(cached_tokens=100), result(cached_tokens=0), result(cached_tokens=5)]
        stats = analyze.cache_stats(records)
        self.assertEqual(stats["eligible"], 3)
        self.assertEqual(stats["hits"], 2, "cached_tokens>0 才算命中")
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["no_usage"], 0)

    def test_records_without_usage_count_as_miss_for_total_request_rate(self):
        """没有 usage 的记录仍在总请求分母中，并单独保留 no_usage 计数。"""
        records = [result(cached_tokens=10), result(cached_tokens=None), result(cached_tokens=None)]
        stats = analyze.cache_stats(records)
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["no_usage"], 2)
        self.assertEqual(stats["request_rate_pct"], 100.0 / 3)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(analyze.pct(stats["hits"], stats["total"]), "33.33%")

    def test_no_usable_records_yields_empty_stats(self):
        stats = analyze.cache_stats([result(), result()])
        self.assertEqual(stats["eligible"], 0)
        self.assertEqual(stats["no_usage"], 2)
        self.assertEqual(stats["request_rate_pct"], 0.0)
        self.assertEqual(analyze.pct(stats["hits"], stats["total"]), "0.00%")

    def test_zero_and_hundred_percent_endpoints(self):
        """0% 与 100% 都是合法结论，不能因为「没有变化」就显示成缺数据。"""
        miss = analyze.cache_stats([result(cached_tokens=0), result(cached_tokens=0)])
        self.assertEqual(analyze.pct(miss["hits"], miss["total"]), "0.00%")

        hit = analyze.cache_stats([result(cached_tokens=9), result(cached_tokens=9)])
        self.assertEqual(analyze.pct(hit["hits"], hit["total"]), "100.00%")

    def test_invalid_cached_token_values_are_unknown(self):
        invalid_text = result(cached_tokens=0); invalid_text["cached_tokens"] = "not-a-number"
        invalid_nan = result(cached_tokens=0); invalid_nan["cached_tokens"] = float("nan")
        numeric_text = result(cached_tokens=0); numeric_text["cached_tokens"] = "4"
        stats = analyze.cache_stats([invalid_text, invalid_nan, numeric_text])
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["no_usage"], 2)


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
        self.assertEqual(cache["request_total"], 2)
        self.assertEqual(cache["basis"], "usage.cached_tokens>0")

    def test_snap_exposes_token_rate_separately_from_request_rate(self):
        task, _ = self._task()
        hit = result(cached_tokens=64, item_id=1); hit["prompt_tokens"] = 128
        miss = result(cached_tokens=0, item_id=2); miss["prompt_tokens"] = 128
        task.record(hit); task.record(miss)
        cache = task.snap()["cache"]
        self.assertEqual(cache["rate_pct"], 50.0)
        self.assertEqual(cache["token_rate_pct"], 25.0)
        self.assertEqual(cache["prompt_tokens"], 256.0)
        self.assertEqual(cache["cached_tokens"], 64.0)
        self.assertEqual(cache["token_basis"], "usage.cached_tokens/usage.prompt_tokens")

    def test_snap_rate_is_none_without_usable_samples(self):
        """按总请求数计算时，缺失缓存字段的请求属于未命中。"""
        task, _ = self._task()
        task.record(result(cached_tokens=None, item_id=1))
        cache = task.snap()["cache"]
        self.assertEqual(cache["rate_pct"], 0.0)
        self.assertEqual(cache["request_total"], 1)
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

    def test_failed_request_keeps_trace_id_and_structured_error_by_default(self):
        """失败请求不应依赖开启原文采集才可逐条核查。"""
        task, path = self._task()
        failed = result(status_code=502, completed=False, item_id=9)
        failed.update(error_type="HTTP_5XX", error_detail="upstream unavailable",
                      reason="upstream unavailable")
        task.record(failed)
        task.close_output()
        with open(path, encoding="utf-8") as handle:
            row = json.loads(handle.readline())
        self.assertEqual(row["request_id"], f"{task.id}:9")
        self.assertEqual(row["error_type"], "HTTP_5XX")
        self.assertEqual(row["error_detail"], "upstream unavailable")
        recent = task.snap()["recent"][0]
        self.assertEqual(recent["request_id"], row["request_id"])
        self.assertEqual(recent["error_type"], "HTTP_5XX")

    def test_gateway_detail_renders_cache_metric_and_trace_fields(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "app.js"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("缓存命中率（请求）", source)
        self.assertIn("gatewayErrorText", source)
        self.assertIn("request_id", source)

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

    def test_offline_token_rate_matches_online_accumulator(self):
        task, path = self._task()
        for index, cached in enumerate((50, 0, 25)):
            row = result(cached_tokens=cached, item_id=index); row["prompt_tokens"] = 100
            task.record(row)
        online = task.snap()["cache"]
        task.close_output()
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        offline = analyze.cache_stats(rows)
        self.assertEqual(online["token_eligible"], offline["token_eligible"])
        self.assertEqual(online["prompt_tokens"], offline["prompt_tokens"])
        self.assertEqual(online["cached_tokens"], offline["cached_tokens"])
        self.assertEqual(online["token_rate_pct"], offline["token_rate_pct"])


class CacheUiScriptTests(unittest.TestCase):
    """前端脚本必须真的把 cache 数据渲染出来，而不是只存在于接口里。"""

    def test_page_script_mentions_cache_metric(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "route_test_ui.py"), encoding="utf-8") as handle:
            backend = handle.read()
        with open(os.path.join(root, "static", "app.js"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"cache"', backend)
        self.assertIn("usage.cached_tokens>0", backend)
        self.assertIn("token_rate_pct", backend)
        self.assertIn("上游已返回缓存字段", source)


if __name__ == "__main__":
    unittest.main()
