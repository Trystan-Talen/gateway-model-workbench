#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路由对照执行器（routing_compare）的端到端测试。

用两个本地回环服务器扮演「固定路由」和「智能路由」：一个恒回缓存命中、一个恒回
缓存未命中。这样可以在不联网、不用真实密钥的前提下，把「智能路由切资源导致缓存
命中率下降」这条完整链路跑通并固定成回归。

重点验证的不是「能跑完」，而是三件会决定结论对错的事：
  1. 两侧请求是交错发出的，不是跑完一侧再跑另一侧；
  2. 两侧结果按 item_id 一一对应，能配成对；
  3. 缓存命中率差异来自真实 usage 字段，而不是把「拿不到 usage」当成未命中。
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing_ab  # noqa: E402
import routing_compare  # noqa: E402
import routing_corpus  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
# 正式档位里最小的一档。测试只发清单的前 TEST_TIER 条——前缀与正式跑法完全一致，
# 只是不发满 1000 条，避免测试变慢。
PROD_TIER = 1000
TEST_TIER = 24


def load_fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), "rb") as handle:
        return handle.read()


class _RouteHandler(BaseHTTPRequestHandler):
    """按模型名回放命中/未命中夹具，并把到达时间记下来供交错断言使用。"""

    fixture = "sse_cache_hit.txt"
    arrivals = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).arrivals.append(time.monotonic())
        raw = load_fixture(type(self).fixture)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for line in raw.split(b"\n"):
                self.wfile.write(line + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


class _HitHandler(_RouteHandler):
    fixture = "sse_cache_hit.txt"
    arrivals = []


class _MissHandler(_RouteHandler):
    fixture = "sse_cache_miss.txt"
    arrivals = []


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _small_corpus(_tier):
    """用正式清单的前 N 条，但只取测试档位那么多——保持 item_id 与正式跑法一致。"""
    return [routing_corpus.build_item(index) for index in range(TEST_TIER)]


def _side(url, name, concurrency=4):
    return {
        "name": name, "url": url, "model": "m", "api_key": "test-key",
        "concurrency": concurrency, "stream": True, "http2": False,
        "include_usage": True, "connect_timeout": 5.0, "read_timeout": 10.0,
        "write_timeout": 5.0, "chat_url": url.rstrip("/") + "/v1/chat/completions",
    }


class InterleavedExecutionTests(unittest.TestCase):
    """交错执行：把「先跑完 A 再跑 B」这个混淆因素排除掉。"""

    def setUp(self):
        _HitHandler.arrivals = []
        _MissHandler.arrivals = []
        self.hit_server, self.hit_url = _serve(_HitHandler)
        self.miss_server, self.miss_url = _serve(_MissHandler)
        self.addCleanup(self.hit_server.shutdown)
        self.addCleanup(self.miss_server.shutdown)

    def _run(self, tier=PROD_TIER):
        comparison = routing_compare.RouteComparison({
            "name": "交错对照", "tier": tier,
            "baseline": _side(self.hit_url, "固定路由"),
            "candidate": _side(self.miss_url, "智能路由"),
        })
        comparison.plan, comparison.total, comparison.planned_duration = (
            routing_compare.build_plan(tier, 60000))
        # 只发前 TEST_TIER 条——清单前缀是共用的，item_id 与正式跑法一致，
        # 但不会为了跑通测试真的发满 1000 条。
        with mock.patch("routing_corpus.build_corpus", side_effect=_small_corpus):
            comparison.execute()
        return comparison

    def test_both_sides_run_and_pair_up(self):
        comparison = self._run()
        snapshot = comparison.snap()
        self.assertEqual(snapshot["status"], "finished", snapshot.get("error"))
        self.assertEqual(len(comparison.records["baseline"]), TEST_TIER)
        self.assertEqual(len(comparison.records["candidate"]), TEST_TIER)
        baseline_ids = {r["item_id"] for r in comparison.records["baseline"]}
        candidate_ids = {r["item_id"] for r in comparison.records["candidate"]}
        self.assertEqual(baseline_ids, candidate_ids)
        self.assertEqual(snapshot["report"]["reliability"]["paired_total"], TEST_TIER)
        self.assertEqual(snapshot["report"]["reliability"]["unpaired_count"], 0)

    def test_requests_are_interleaved_not_batched(self):
        """两侧到达时间必须互相穿插。若一侧全部先到，说明执行顺序被批量化了。"""
        self._run()
        merged = ([(t, "b") for t in _HitHandler.arrivals]
                  + [(t, "c") for t in _MissHandler.arrivals])
        merged.sort()
        sides = [side for _, side in merged]
        # 统计相邻两侧不同的次数；真正交错时这个数会接近总数。
        switches = sum(1 for i in range(1, len(sides)) if sides[i] != sides[i - 1])
        self.assertGreater(switches, len(sides) * 0.5,
                           f"两侧请求没有交错（切换 {switches}/{len(sides)}）")

    def test_cache_hit_rate_drops_and_is_attributable(self):
        """固定路由恒命中、智能路由恒未命中时，报告必须给出可归因的下降。"""
        comparison = self._run()
        report = comparison.snap()["report"]
        self.assertAlmostEqual(report["baseline"]["cache"]["pct"], 100.0, places=2)
        self.assertAlmostEqual(report["candidate"]["cache"]["pct"], 0.0, places=2)
        self.assertAlmostEqual(report["cache"]["delta_pct"], -100.0, places=2)
        self.assertEqual(report["cache"]["attribution"], routing_compare.routing_ab.ATTRIBUTION_OK)

    def test_no_usage_side_is_incomparable_not_zero(self):
        """把智能路由换成一个不回 usage 的网关，命中率必须是「不可比」而不是 0%。"""
        comparison = routing_compare.RouteComparison({
            "name": "不支持 usage 的网关", "tier": PROD_TIER,
            "baseline": _side(self.hit_url, "固定路由"),
            "candidate": _side(self.hit_url, "智能路由"),
        })
        comparison.config["candidate"]["include_usage"] = False
        # 让解析器即使收到 usage 也不采集：模拟网关从不回 usage 的情形。
        with mock.patch("routing_compare.do_request", side_effect=self._usage_less_request), \
                mock.patch("routing_corpus.build_corpus", side_effect=_small_corpus):
            comparison.plan, comparison.total, comparison.planned_duration = (
                routing_compare.build_plan(PROD_TIER, 60000))
            comparison.execute()
        report = comparison.snap()["report"]
        self.assertEqual(report["cache"]["attribution"], routing_ab.ATTRIBUTION_UNKNOWN)
        self.assertIsNone(report["cache"]["candidate"]["pct"])
        self.assertIn("不可比", report["cache"]["note"])

    async def _usage_less_request(self, client, item, payload, headers, timeout_cfg,
                                  chat_url=None, include_usage=False, **kwargs):
        """返回一条成功但无 usage 的结果——模拟只回正文、不回用量的网关。"""
        return {
            "schema_version": 3, "ts": int(time.time() * 1000), "item_id": item.get("id"),
            "mode": item.get("mode"), "profile": item.get("profile"),
            "step_index": item.get("step_index"), "status_code": 200, "completed": True,
            "error_type": None, "error_detail": None, "ttft_ms": 12.0, "total_ms": 30.0,
            "reason": None, "usage_seen": False, "usage_source": None, "usage_level": "NONE",
            "prompt_tokens": None, "cached_tokens": None, "completion_tokens": None,
            "total_tokens": None, "cache_hit": None,
        }

    def test_results_are_written_to_disk_with_owner_only_permissions(self):
        comparison = self._run()
        for side in ("baseline", "candidate"):
            path = comparison.out_paths[side]
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, "结果文件必须是 0600")
            with open(path, encoding="utf-8") as handle:
                first = json.loads(handle.readline())
            self.assertEqual(first["route_side"], side)
            self.assertNotIn("test-key", json.dumps(first), "结果文件里出现了密钥")
            os.unlink(path)

    def test_secrets_are_cleared_after_run(self):
        comparison = self._run()
        self.assertNotIn("api_key", comparison.config["baseline"])
        self.assertNotIn("api_key", comparison.config["candidate"])


class PlanTests(unittest.TestCase):
    def test_unknown_tier_is_rejected(self):
        with self.assertRaises(ValueError):
            routing_compare.validate_tier(1234)

    def test_all_advertised_tiers_are_accepted(self):
        for tier in routing_corpus.TIERS:
            self.assertEqual(routing_compare.validate_tier(tier), tier)

    def test_tier_whitelist_rejects_non_ints_and_bools(self):
        # True 在 Python 里等于 1，但「档位 = True」不是用户的意图，必须挡掉。
        for bad in (True, "1000", 1000.0, None):
            with self.assertRaises(ValueError):
                routing_compare.validate_tier(bad)

    def test_executor_accepts_any_positive_int_but_not_zero(self):
        # 执行器放宽到正整数，测试才能用小档位跑同一条代码路径。
        steps, total, _ = routing_compare.build_plan(24, 60000)
        self.assertEqual((steps[0]["count"], total), (24, 24))
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                routing_compare.build_plan(bad, 600)

    def test_plan_covers_the_tier(self):
        steps, total, duration = routing_compare.build_plan(1000, 600)
        self.assertEqual(total, 1000)
        self.assertEqual(steps[0]["count"], 1000)
        self.assertGreater(duration, 0)

    def test_snapshot_never_exposes_api_key(self):
        comparison = routing_compare.RouteComparison({
            "name": "脱敏", "tier": 1000,
            "baseline": _side("http://x/v1", "固定路由"),
            "candidate": _side("http://y/v1", "智能路由"),
        })
        comparison.plan, comparison.total, comparison.planned_duration = (
            routing_compare.build_plan(1000, 600))
        text = json.dumps(comparison.snap(), ensure_ascii=False)
        self.assertNotIn("test-key", text)


if __name__ == "__main__":
    unittest.main()
