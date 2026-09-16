#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路由对照 HTTP 接口层的测试。

执行器本身的行为已经在 test_routing_compare 里跑通了，这里只盯接口层的三件事：
  1. 索引里读回来的记录不能带密钥，也不能在恢复后「看起来还在跑」；
  2. 删除记录必须把两侧结果文件一起删掉，否则文件会永久留在磁盘上；
  3. 列表接口暴露的档位与指标口径，和界面真正要渲染的东西对得上。
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import route_test_ui as ui  # noqa: E402
import routing_ab  # noqa: E402
import routing_compare  # noqa: E402
import routing_corpus  # noqa: E402


def _side(name, url="http://127.0.0.1:9/v1", records=10):
    return {"name": name, "url": url, "model": "m", "records": records}


def _snapshot(**overrides):
    snap = {
        "id": "abcdef123456", "name": "历史路由对照", "status": "finished",
        "error": None, "created": 1000.0, "started_at": 1001.0, "finished_at": 1002.0,
        "tier": 1000, "rpm": 600, "concurrency": {"baseline": 8, "candidate": 8},
        "corpus": routing_corpus.corpus_manifest(1000),
        "sides": {"baseline": _side("固定路由"), "candidate": _side("智能路由")},
        "report": {"success": {"delta_pct": 1.5}, "cache": {"delta_pct": -12.0}},
        "limits": ["黑盒观测"],
    }
    snap.update(overrides)
    return snap


class HistorySnapshotTests(unittest.TestCase):
    def test_history_snapshot_never_carries_prompt_text_or_keys(self):
        """索引是每次启动都读的文件，不能塞进一万条 prompt，更不能塞密钥。"""
        raw = _snapshot()
        raw["sides"]["baseline"]["api_key"] = "sk-should-not-persist"
        safe = ui._routing_history_snapshot(raw)
        text = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn("sk-should-not-persist", text)
        self.assertNotIn("prompt", text)
        # corpus 只保留指纹与计数，正文不进索引。
        self.assertNotIn("item_text", text)

    def test_history_snapshot_keeps_the_report(self):
        """报告是这条记录存在的理由，摘要里必须留下。"""
        safe = ui._routing_history_snapshot(_snapshot())
        self.assertEqual(safe["report"]["cache"]["delta_pct"], -12.0)
        self.assertEqual(safe["tier"], 1000)


class RestoreTests(unittest.TestCase):
    def tearDown(self):
        with ui.ROUTING_COMPARISONS.lock:
            for run_id in list(ui.ROUTING_COMPARISONS.runs):
                if run_id.startswith("restoretest"):
                    ui.ROUTING_COMPARISONS.runs.pop(run_id, None)

    def test_interrupted_run_restores_as_error_not_running(self):
        """重启后不能显示成「还在跑」——那个执行线程已经不存在了。"""
        run = routing_compare.RouteComparison.from_snapshot(_snapshot(
            id="restoretest1", status="running", error=None))
        self.assertEqual(run.status, "error")
        self.assertIn("重启", run.error)
        self.assertTrue(run.history_only)

    def test_restored_run_can_be_snapshotted_without_keys(self):
        run = routing_compare.RouteComparison.from_snapshot(_snapshot(id="restoretest2"))
        snap = run.snap()
        text = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("api_key", text)
        self.assertEqual(snap["tier"], 1000)
        self.assertEqual(snap["sides"]["baseline"]["records"], 10)

    def test_restored_run_gets_result_paths_derived_from_id(self):
        run = routing_compare.RouteComparison.from_snapshot(_snapshot(id="restoretest3"))
        run.out_paths = ui.routing_result_paths(run.id)
        for side in ("baseline", "candidate"):
            self.assertTrue(run.out_paths[side].endswith(f"routing-restoretest3-{side}.jsonl"))
        # 恢复的记录不能被当成孤儿文件清掉。
        with ui.ROUTING_COMPARISONS.lock:
            ui.ROUTING_COMPARISONS.runs[run.id] = run
        try:
            referenced = ui.referenced_artifacts()
            self.assertIn(os.path.basename(run.out_paths["baseline"]), referenced)
        finally:
            with ui.ROUTING_COMPARISONS.lock:
                ui.ROUTING_COMPARISONS.runs.pop(run.id, None)


class DeleteTests(unittest.TestCase):
    def test_delete_removes_both_result_files(self):
        """结果文件以 run.id 命名，记录一删就再也够不到，必须一起删。"""
        run = routing_compare.RouteComparison({
            "name": "待删除", "tier": 1000,
            "baseline": {"name": "固定路由", "url": "http://127.0.0.1:9/v1", "model": "m",
                         "concurrency": 1, "chat_url": "http://127.0.0.1:9/v1/chat/completions"},
            "candidate": {"name": "智能路由", "url": "http://127.0.0.1:9/v1", "model": "m",
                          "concurrency": 1, "chat_url": "http://127.0.0.1:9/v1/chat/completions"},
        })
        run.plan, run.total, run.planned_duration = routing_compare.build_plan(1000, 600)
        with ui.ROUTING_COMPARISONS.lock:
            ui.ROUTING_COMPARISONS.runs[run.id] = run
        paths = ui.routing_result_paths(run.id)
        os.makedirs(os.path.dirname(paths["baseline"]), exist_ok=True)
        for path in paths.values():
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
        self.assertIsNone(ui.ROUTING_COMPARISONS.delete(run.id))
        for path in paths.values():
            self.assertFalse(os.path.exists(path), f"结果文件未被删除：{path}")
        self.assertIsNone(ui.ROUTING_COMPARISONS.get(run.id))

    def test_delete_on_unknown_id_reports_instead_of_raising(self):
        self.assertEqual(ui.ROUTING_COMPARISONS.delete("no-such-run"), "路由对照不存在")


class ApiPayloadTests(unittest.TestCase):
    def test_metric_labels_cover_cache_as_a_first_class_metric(self):
        keys = {item["key"] for item in routing_ab.METRIC_LABELS}
        self.assertIn("cache", keys)
        self.assertIn("success", keys)
        for item in routing_ab.METRIC_LABELS:
            self.assertTrue(item["label"] and item["basis"], item)

    def test_public_config_hides_keys_but_keeps_tiers(self):
        cfg = routing_compare.validate_config({
            "tier": 1000, "rpm": 600,
            "baseline": {"name": "固定路由", "url": "http://127.0.0.1:9/v1", "api_key": "sk-a",
                         "model": "m", "concurrency": 4, "mode": "smoke"},
            "candidate": {"name": "智能路由", "url": "http://127.0.0.1:9/v1", "api_key": "sk-b",
                          "model": "m", "concurrency": 4, "mode": "smoke"},
        })
        public = routing_compare.public_config(cfg)
        text = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("sk-a", text)
        self.assertNotIn("sk-b", text)
        self.assertEqual([item["tier"] for item in public["tiers"]],
                         list(routing_corpus.TIERS))

    def tearDown(self):
        with ui.ROUTING_COMPARISONS.lock:
            ui.ROUTING_COMPARISONS.runs.clear()


if __name__ == "__main__":
    unittest.main()
