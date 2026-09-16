"""任务状态机、结果产物生命周期与发起前预检的回归测试。

这三块是「结论可信度」的地基：
- 状态机保证任务不会停在语义模糊的中间态；
- artifact 生命周期保证报告引用的结果文件可核对、可删除、可发现已丢失；
- 预检保证风险在提交之前暴露，而不是事后解释。
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import preflight
import route_test_ui as ui
import storage
import task_status


class TaskStatusTests(unittest.TestCase):
    def test_terminal_state_priority(self):
        # 失败优先于停止：既被停止又出过错时，不能让任务看起来“只是被停了”。
        self.assertEqual(task_status.normalize_terminal_state("running", done=3, total=10, has_errors=True, stopped=True), "stopped")
        self.assertEqual(task_status.normalize_terminal_state("running", done=3, total=10, has_errors=True), "finished_with_errors")
        self.assertEqual(task_status.normalize_terminal_state("running", done=10, total=10, has_errors=False), "finished")
        self.assertEqual(task_status.normalize_terminal_state("running", done=4, total=10, has_errors=False), "stopped")

    def test_normalize_never_invents_success(self):
        # 一个都没完成时不能返回“已完成”，否则报告会说“全部执行完毕”。
        self.assertEqual(task_status.normalize_terminal_state("running", done=0, total=10), "stopped")

    def test_apply_transition_rejects_illegal_jump(self):
        class Holder:
            status = "waiting"

        holder = Holder()
        with self.assertRaises(task_status.StatusTransitionError):
            task_status.apply_transition(holder, "finished")
        # 非法迁移不能改动对象状态。
        self.assertEqual(holder.status, "waiting")

    def test_apply_transition_force_bypasses_table_but_not_vocabulary(self):
        class Holder:
            status = "running"

        holder = Holder()
        task_status.apply_transition(holder, "finished", force=True)
        self.assertEqual(holder.status, "finished")
        with self.assertRaises(task_status.StatusTransitionError):
            task_status.apply_transition(holder, "not_a_state", force=True)

    def test_all_states_have_labels(self):
        for state in task_status.STATES:
            self.assertTrue(task_status.status_label(state))
        self.assertTrue(task_status.TERMINAL_STATES)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = self.folder.name

    def _write(self, name, content="x", mode=0o600):
        path = os.path.join(self.base, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(path, mode)
        return path

    def test_only_artifact_names_are_candidates(self):
        self.assertTrue(storage.is_artifact_name("run_1234_abcd.jsonl"))
        self.assertTrue(storage.is_artifact_name("quality_run_abcd.json"))
        # 配置、账号、历史索引都在同一目录，绝不能被当成结果文件清理。
        for name in ("tasks.json", "task_index.json", "accounts.json", "route_test_ui.py"):
            self.assertFalse(storage.is_artifact_name(name))

    def test_unlink_refuses_non_artifact_path(self):
        path = self._write("task_index.json")
        self.assertFalse(storage.unlink_artifact(path))
        self.assertTrue(os.path.exists(path))

    def test_fingerprint_detects_truncation(self):
        path = self._write("run_1_aaaa.jsonl", "abcdef")
        size, digest = storage.file_fingerprint(path)
        self.assertEqual(size, 6)
        with open(path, "w", encoding="utf-8") as f:
            f.write("abc")
        self.assertNotEqual(storage.file_fingerprint(path)[1], digest)

    def test_summary_separates_referenced_from_orphans(self):
        self._write("run_1_aaaa.jsonl", "a" * 10)
        self._write("run_2_bbbb.jsonl", "b" * 10)
        self._write("run_3_cccc.jsonl", "c" * 10)
        summary = storage.storage_summary(self.base, {"run_1_aaaa.jsonl": "任务 A"})
        self.assertEqual(summary["artifact_count"], 3)
        self.assertEqual(summary["referenced_bytes"], 10)
        self.assertEqual(summary["orphan_count"], 2)
        self.assertEqual(summary["orphan_bytes"], 20)
        self.assertFalse(summary["warn"])

    def test_cleanup_only_removes_unreferenced(self):
        self._write("run_1_aaaa.jsonl")
        self._write("run_2_bbbb.jsonl")
        result = storage.cleanup_orphans(self.base, {"run_1_aaaa.jsonl": "任务 A"})
        self.assertEqual(result["removed"], ["run_2_bbbb.jsonl"])
        self.assertTrue(os.path.exists(os.path.join(self.base, "run_1_aaaa.jsonl")))

    def test_cleanup_respects_age_guard(self):
        self._write("run_2_bbbb.jsonl")
        result = storage.cleanup_orphans(self.base, {}, older_than_seconds=3600)
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["skipped"], ["run_2_bbbb.jsonl"])


class ArtifactLifecycleTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ui, "persist_history")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task_with_artifact(self):
        with mock.patch.object(ui, "_load_api_key", return_value="sk-test"):
            cfg = ui.validate_config({
                "name": "生命周期测试", "url": "http://127.0.0.1:1/v1/chat/completions",
                "model": "m", "mode": "smoke", "concurrency": 1, "rpm": 60, "iterations": 1,
            })
        steps, total = ui.build_plan(cfg)
        return ui.Task(cfg, steps, total)

    def test_open_output_registers_artifact(self):
        task = self._task_with_artifact()
        with mock.patch.object(ui, "BASE_DIR", tempfile.mkdtemp()):
            task.open_output()
            self.addCleanup(lambda: task.out_path and os.path.exists(task.out_path) and os.unlink(task.out_path))
            self.assertEqual(task.artifact_path, task.out_path)
            task.close_output()
            self.assertEqual(task.artifact_state, "present")
            self.assertIsNotNone(task.artifact_bytes)
            self.assertTrue(os.path.exists(task.out_path))

    def test_delete_artifact_removes_file(self):
        folder = tempfile.mkdtemp()
        with mock.patch.object(ui, "BASE_DIR", folder):
            task = self._task_with_artifact()
            task.open_output()
            task.close_output()
            path = task.out_path
            self.assertTrue(os.path.exists(path))
            self.assertTrue(task.delete_artifact())
            self.assertFalse(os.path.exists(path))
            self.assertEqual(task.artifact_state, "deleted")

    def test_manager_delete_removes_artifact(self):
        folder = tempfile.mkdtemp()
        with mock.patch.object(ui, "BASE_DIR", folder):
            task = self._task_with_artifact()
            task.open_output()
            task.close_output()
            path = task.out_path
            # 删除只允许作用于已结束的任务；执行中的任务必须先停止。
            task.status = "finished"
            with ui.MANAGER.lock:
                ui.MANAGER.tasks[task.id] = task
            self.addCleanup(lambda: ui.MANAGER.tasks.pop(task.id, None))
            error = ui.MANAGER.delete(task.id)
            self.assertIsNone(error)
            self.assertFalse(os.path.exists(path))
            self.assertIsNone(ui.MANAGER.get(task.id))

    def test_restore_marks_missing_artifact_expired(self):
        snapshot = {
            "id": "abc12345", "name": "历史任务", "status": "finished", "total": 1,
            "out_path": os.path.join(tempfile.mkdtemp(), "run_1_gone.jsonl"),
            "config": {"url": "http://127.0.0.1:1/v1/chat/completions", "model": "m",
                       "mode": "smoke", "iterations": 1, "rpm": 60, "concurrency": 1},
            "artifact": {"bytes": 123, "sha256": "deadbeef"},
        }
        task = ui._restore_task_snapshot(snapshot)
        self.assertEqual(task.artifact_state, "expired")
        self.assertTrue(task.artifact_expired_reason)

    def test_restore_detects_size_change(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "run_1_size.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        snapshot = {
            "id": "abc12345", "name": "历史任务", "status": "finished", "total": 1,
            "out_path": path,
            "config": {"url": "http://127.0.0.1:1/v1/chat/completions", "model": "m",
                       "mode": "smoke", "iterations": 1, "rpm": 60, "concurrency": 1},
            # 记录里的大小与实际不符：说明文件被外部改过，不能再当作可信证据。
            "artifact": {"bytes": 999999},
        }
        task = ui._restore_task_snapshot(snapshot)
        self.assertEqual(task.artifact_state, "expired")

    def test_quality_task_registers_artifact(self):
        with mock.patch.object(ui, "_load_api_key", return_value="sk-test"):
            cfg = ui.validate_quality_config({
                "name": "质量生命周期", "suite_path": None,
                "resources": [{"name": "r1", "url": "http://127.0.0.1:1/v1/chat/completions",
                               "model": "m", "api_key": ""}],
            })
        task = ui.QualityTask(cfg)
        self.assertEqual(task.artifact_path, task.out_path)
        self.assertIn("artifact", task.snap())
        self.assertIn("status_label", task.snap())


class PreflightTests(unittest.TestCase):
    def test_local_target_does_not_require_confirmation(self):
        cfg = {"url": "http://127.0.0.1:8666/v1/chat/completions", "model": "m", "mode": "smoke",
               "concurrency": 4, "rpm": 600, "iterations": 100, "read_timeout": 60}
        result = preflight.estimate_run(cfg)
        self.assertFalse(result["requires_confirmation"])
        self.assertNotIn("production_target", [f["code"] for f in result["findings"]])

    def test_public_domain_requires_confirmation(self):
        cfg = {"url": "https://api.example.com/v1/chat/completions", "model": "m", "mode": "smoke",
               "concurrency": 4, "rpm": 600, "iterations": 100, "read_timeout": 60}
        result = preflight.estimate_run(cfg)
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["level"], "risk")
        self.assertIn("production_target", [f["code"] for f in result["findings"]])

    def test_concurrency_shortfall_is_reported(self):
        cfg = {"url": "http://127.0.0.1:8666/v1/chat/completions", "model": "m", "mode": "smoke",
               "concurrency": 1, "rpm": 6000, "iterations": 1000, "read_timeout": 120}
        result = preflight.estimate_run(cfg)
        self.assertFalse(result["capacity"]["sufficient"])
        self.assertIn("concurrency_below_plan", [f["code"] for f in result["findings"]])

    def test_quality_short_suite_is_flagged(self):
        cfg = {"resources": [{"url": "http://127.0.0.1:8666/v1/chat/completions", "model": "m"}],
               "capture_raw": False}
        result = preflight.estimate_quality_run(cfg, case_count=12)
        self.assertIn("small_suite", [f["code"] for f in result["findings"]])
        self.assertEqual(result["planned_requests"], 12)

    def test_quality_same_endpoint_pair_is_flagged(self):
        resource = {"url": "http://127.0.0.1:8666/v1/chat/completions", "model": "m"}
        result = preflight.estimate_quality_run({"resources": [dict(resource), dict(resource)]}, case_count=120)
        self.assertIn("same_endpoint", [f["code"] for f in result["findings"]])

    def test_estimates_scale_with_raw_capture(self):
        base = {"url": "http://127.0.0.1:8666/v1/chat/completions", "model": "m", "mode": "smoke",
                "concurrency": 4, "rpm": 600, "iterations": 1000, "read_timeout": 60}
        plain = preflight.estimate_run(dict(base, capture_raw=False))
        raw = preflight.estimate_run(dict(base, capture_raw=True))
        self.assertGreater(raw["estimated_bytes"], plain["estimated_bytes"])


class PreflightEndpointTests(unittest.TestCase):
    def _call(self, query=""):
        handler = ui.Handler.__new__(ui.Handler)
        handler.path = "/preflight" + query
        captured = {}

        # 打补丁的是类属性，替换函数会被当成普通方法调用，第一个参数仍是 handler 实例。
        def fake_json(self, data, status=200, headers=None):
            captured["payload"] = data
            captured["status"] = status
            return None

        with mock.patch.object(ui, "_current_user", return_value={"username": "tester"}):
            with mock.patch.object(ui.Handler, "_json", fake_json):
                ui.Handler.do_GET(handler)
        return captured
    def test_endpoint_degrades_instead_of_failing(self):
        # 预检只是辅助决策：估算函数自己抛错时必须降级成可读原因，不能 500 掉整个页面。
        with mock.patch.object(ui, "read_saved_config", return_value={"url": "http://127.0.0.1:1/x"}):
            with mock.patch.object(ui, "estimate_run", side_effect=TypeError("iterations 不是数字")):
                captured = self._call()
        self.assertEqual(200, captured["status"])
        self.assertIn("unavailable", captured["payload"])
        self.assertFalse(captured["payload"]["requires_confirmation"])

    def test_reader_endpoint_returns_500_not_a_dropped_connection(self):
        # do_GET 之前没有兜底，异常会让连接直接断开；现在必须返回可读的 500。
        with mock.patch.object(ui, "read_saved_config", side_effect=RuntimeError("索引损坏")):
            captured = self._call()
        self.assertEqual(500, captured["status"])
        self.assertNotIn("索引损坏", captured["payload"]["error"])
        self.assertTrue(captured["payload"]["request_id"])

    def test_endpoint_returns_estimate_for_local_target(self):
        cfg = {"url": "http://127.0.0.1:8666/v1/chat/completions", "model": "m", "mode": "smoke",
               "concurrency": 4, "rpm": 600, "iterations": 120, "read_timeout": 60, "capture_raw": False}
        with mock.patch.object(ui, "read_saved_config", return_value=cfg):
            captured = self._call()
        self.assertEqual(200, captured["status"])
        self.assertEqual(120, captured["payload"]["planned_requests"])
        self.assertFalse(captured["payload"]["requires_confirmation"])


class RiskConfirmationGateTests(unittest.TestCase):
    """生产类目标的确认闸门必须在服务端生效。

    界面上的确认弹窗只是体验层：如果服务端不拦，脚本、curl 或重放请求
    都能绕过它，直接对线上环境发起真实负载。
    """

    LOCAL = {"url": "http://127.0.0.1:8666/v1/chat/completions", "model": "m", "mode": "smoke",
             "concurrency": 4, "rpm": 600, "iterations": 120, "read_timeout": 60}
    PUBLIC = {"url": "https://api.example.com/v1/chat/completions", "model": "m", "mode": "smoke",
              "concurrency": 4, "rpm": 600, "iterations": 120, "read_timeout": 60}

    def test_local_target_passes_without_acknowledgement(self):
        self.assertIsNone(ui.require_risk_confirmation(dict(self.LOCAL), {}))

    def test_public_target_is_blocked_without_acknowledgement(self):
        message = ui.require_risk_confirmation(dict(self.PUBLIC), {})
        self.assertIsNotNone(message)
        self.assertIn("api.example.com", message)

    def test_public_target_passes_with_explicit_acknowledgement(self):
        self.assertIsNone(ui.require_risk_confirmation(dict(self.PUBLIC), {"risk_acknowledged": True}))

    def test_acknowledgement_must_be_boolean_true(self):
        # 字符串 "true" 或 1 都不算显式确认：闸门只认真正的布尔真值，避免被拼接绕过。
        for value in ("true", 1, "yes", [1], {"ok": True}):
            self.assertIsNotNone(ui.require_risk_confirmation(dict(self.PUBLIC), {"risk_acknowledged": value}))

    def test_unparseable_config_degrades_instead_of_blocking(self):
        # 预检失败时不能把创建流程卡死：宁可放行由后续校验报错，也不要因估算失败而误拦。
        broken = {"url": "https://api.example.com/x", "model": "m", "mode": "smoke",
                  "concurrency": "not-a-number", "rpm": 600, "iterations": 10}
        self.assertIsNone(ui.require_risk_confirmation(broken, {}))

    def test_empty_draft_degrades_instead_of_400(self):
        # 抽屉刚打开时草稿里没有 URL，预检必须返回可读的「不足以预检」，
        # 而不是抛错让接口返回 400——否则每开一次新建面板就多一条失败请求。
        for draft in ({}, {"url": "", "model": ""}, {"url": "http://127.0.0.1:1/v1"}):
            payload = ui.preflight_or_degrade(dict(draft), kind="run")
            self.assertFalse(payload["requires_confirmation"])
            self.assertIn("unavailable", payload)

    def test_preflight_route_never_400s_on_a_partial_draft(self):
        # 端到端地钉住这一点：走真正的路由分支，而不是只测降级函数。
        handler = ui.Handler.__new__(ui.Handler)
        handler.path = "/preflight"
        handler.headers = {}
        captured = {}
        original = {name: getattr(ui.Handler, name)
                    for name in ("_json", "_body", "_begin", "_note")}
        ui.Handler._json = lambda self, data, status=200, headers=None: captured.update(
            payload=data, status=status)
        ui.Handler._body = lambda self: {"kind": "run", "config": {}}
        ui.Handler._begin = lambda self: None
        ui.Handler._note = lambda self, *a, **k: None
        real_user, ui._current_user = ui._current_user, lambda self: {"username": "t", "role": "admin"}
        try:
            handler.do_POST()
        finally:
            ui._current_user = real_user
            for name, func in original.items():
                setattr(ui.Handler, name, func)
        self.assertEqual(200, captured["status"])
        self.assertIn("unavailable", captured["payload"])

    def test_draft_preflight_reads_the_draft_not_saved_config(self):
        # 预检吃的是表单草稿：用户把地址改成公网域名，即使没提交过也要立刻看到需要确认。
        payload = ui.preflight_or_degrade(dict(self.PUBLIC), kind="run")
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual("api.example.com", payload["target"]["host"])
        local = ui.preflight_or_degrade(dict(self.LOCAL), kind="run")
        self.assertFalse(local["requires_confirmation"])
        self.assertEqual(120, local["planned_requests"])

    def test_quality_draft_preflight_flags_short_suite(self):
        payload = ui.preflight_or_degrade(
            {"resources": [{"url": "https://api.example.com/v1/chat/completions", "model": "m"}],
             "capture_raw": False, "case_count": 12},
            kind="quality",
        )
        self.assertIn("small_suite", [f["code"] for f in payload["findings"]])
        self.assertTrue(payload["requires_confirmation"])

    def test_task_creation_endpoint_blocks_production_target(self):
        public = dict(self.PUBLIC)
        handler = ui.Handler.__new__(ui.Handler)
        handler.path = "/tasks"
        handler.headers = {}
        captured = {}

        def fake_json(self, data, status=200, headers=None):
            captured["payload"] = data
            captured["status"] = status
            return None

        with mock.patch.object(ui, "_current_user", return_value={"username": "tester"}):
            with mock.patch.object(ui.Handler, "_body", lambda self: dict(public)):
                with mock.patch.object(ui.Handler, "_json", fake_json):
                    with mock.patch.object(ui.MANAGER, "add") as add:
                        ui.Handler.do_POST(handler)
        self.assertEqual(400, captured["status"])
        self.assertTrue(captured["payload"]["requires_confirmation"])
        add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
