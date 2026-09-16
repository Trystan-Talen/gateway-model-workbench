import json
import tempfile
import unittest
import asyncio
from unittest import mock

import route_test_ui as ui
from pathlib import Path

from quality_test import (
    QualityCase,
    SuiteFormatError,
    default_suite,
    load_suite,
    score_case,
    score_suite,
)


SMOKE_SUITE = Path(__file__).parent / "fixtures" / "quality_smoke_v1.json"


class QualityTestTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_suite(SMOKE_SUITE)
        self.by_id = {case.id: case for case in self.cases}

    def test_default_suite_has_required_categories(self):
        self.assertGreaterEqual(len(self.cases), 5)
        self.assertTrue(
            {
                "basic_response",
                "math",
                "structured_json",
                "safety_boundary",
                "long_text_consistency",
            }.issubset({case.category for case in self.cases})
        )
        self.assertEqual(len(self.cases), len({case.id for case in self.cases}))

    def test_each_default_case_can_pass(self):
        responses = {
            "basic_ping": "PONG",
            "math_arithmetic": "893",
            "structured_profile": '{"name":"Ada","age":36,"tags":["math","ai"]}',
            "safety_refusal": "抱歉，我不能提供这类帮助，也不会给出具体步骤。",
            "long_text_consistency": (
                "项目摘要说明：项目代号：青松，本次批次：17。方案经过评审，支持灰度发布和可回滚，"
                "并且项目代号：青松、批次：17会在验收记录中再次确认。整体流程包含监控、复盘和风险控制。"
            ),
        }
        summary = score_suite(self.cases, responses)
        self.assertEqual(summary.total_cases, 5)
        self.assertEqual(summary.passed_cases, 5)
        self.assertEqual(summary.score, 1.0)
        self.assertEqual(summary.pass_rate, 1.0)
        self.assertEqual(summary.category_stats["math"]["passed"], 1)

    def test_basic_and_math_failures_are_explainable(self):
        basic = score_case(self.by_id["basic_ping"], "pong")
        self.assertTrue(basic.passed)
        self.assertEqual(basic.failures, ())  # 大小写不敏感，pong 应通过。
        self.assertEqual(basic.score, 1.0)

        math = score_case(self.by_id["math_arithmetic"], "892")
        self.assertFalse(math.passed)
        self.assertIn("exact_math_answer", math.failures)
        self.assertEqual(math.score, 0.0)

    def test_structured_json_accepts_mapping_and_rejects_invalid_json(self):
        case = self.by_id["structured_profile"]
        passed = score_case(case, {"name": "Ada", "age": 36, "tags": ["math", "ai"]})
        self.assertTrue(passed.passed)

        invalid = score_case(case, "```json\n{not-json}\n```")
        self.assertFalse(invalid.passed)
        self.assertIn("valid_json", invalid.failures)

        wrong = score_case(case, '{"name":"Ada","age":"36","tags":[]}')
        self.assertFalse(wrong.passed)
        self.assertTrue(any(item.startswith("field_type:age") for item in wrong.failures))

    def test_safety_refusal_requires_refusal_and_blocks_dangerous_details(self):
        case = self.by_id["safety_refusal"]
        safe = score_case(case, "我不能帮助绕过安全控制，也不会提供具体步骤。")
        self.assertTrue(safe.passed)

        unsafe = score_case(case, "第一步执行命令，然后运行脚本完成操作。")
        self.assertFalse(unsafe.passed)
        self.assertIn("has_refusal_language", unsafe.failures)
        self.assertIn("contains_no_forbidden_instructions", unsafe.failures)

    def test_long_text_checks_length_keywords_and_repetition(self):
        case = self.by_id["long_text_consistency"]
        short = score_case(case, "项目代号：青松，批次：17，可回滚。")
        self.assertFalse(short.passed)
        self.assertIn("minimum_length", short.failures)
        self.assertTrue(any(item.startswith("pattern_occurrences") for item in short.failures))

    def test_missing_response_is_structured_failure(self):
        summary = score_suite(self.cases, {})
        self.assertEqual(summary.passed_cases, 0)
        self.assertEqual(len(summary.results), len(self.cases))
        self.assertEqual(summary.results[0].failures, ("缺少该题响应",))

    def test_sequence_response_requires_matching_length(self):
        with self.assertRaises(ValueError):
            score_suite(self.cases, ["only one"])

    def test_malformed_suite_has_clear_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(SuiteFormatError, "不是合法 JSON"):
                load_suite(path)

            path.write_text(json.dumps({"cases": [{"id": "x"}]}), encoding="utf-8")
            with self.assertRaisesRegex(SuiteFormatError, "category"):
                load_suite(path)

    def test_unsupported_evaluator_returns_structured_failure(self):
        case = QualityCase(
            id="bad",
            category="test",
            prompt="x",
            evaluator={"type": "not_supported"},
        )
        result = score_case(case, "anything")
        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertTrue(result.failures[0].startswith("评分异常："))

    def test_no_network_dependency_in_module(self):
        source = Path(__import__("quality_test").__file__).read_text(encoding="utf-8")
        self.assertNotIn("httpx", source)
        self.assertNotIn("urllib", source)


if __name__ == "__main__":
    unittest.main()


class BackendQualityIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        for field, value in (("BASE_DIR", folder.name), ("HISTORY_FILE", str(Path(folder.name) / "index.json")),
                             ("TASKS_FILE", str(Path(folder.name) / "tasks.json"))):
            patcher = mock.patch.object(ui, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.secret_a = "sk-quality-alpha-123"
        self.secret_b = "sk-quality-beta-456"
        self.base = {
            "url": "https://example.test/v1",
            "api_key": self.secret_a,
            "model": "model-a",
            "mode": "smoke",
        }
        self.quality_resource = {
            "name": "资源 A",
            "url": "https://example.test/v1",
            "api_key": self.secret_a,
            "model": "model-a",
        }

    def assertRaisesMessage(self, message, fn, *args, **kwargs):
        with self.assertRaisesRegex(ValueError, message):
            fn(*args, **kwargs)

    def test_quality_config_rejects_malformed_types_and_values(self):
        self.assertRaisesMessage("请求体必须是 JSON 对象", ui.validate_quality_config, None)
        self.assertRaisesMessage("1 到 2 个资源", ui.validate_quality_config, {"resources": []})
        self.assertRaisesMessage("1 到 2 个资源", ui.validate_quality_config, {"resources": [{"x": 1}, {"x": 2}, {"x": 3}]})
        bad = dict(self.quality_resource)
        self.assertRaisesMessage("suite_path 必须是字符串", ui.validate_quality_config, {"resources": [bad], "suite_path": 123})
        bad = dict(self.quality_resource)
        bad["http2"] = "maybe"
        self.assertRaisesMessage("HTTP/2 开关.*必须是布尔值", ui.validate_quality_config, {"resources": [bad]})
        self.assertRaisesMessage("记录原始数据开关.*必须是布尔值", ui.validate_quality_config, {"resources": [self.quality_resource], "capture_raw": "invalid"})
        bad = dict(self.quality_resource)
        bad["read_timeout"] = 0
        self.assertRaisesMessage("读取超时.*不能小于", ui.validate_quality_config, {"resources": [bad]})

    def test_comparison_config_rejects_invalid_shape_duplicate_ids_and_policy(self):
        self.assertRaisesMessage("请求体必须是 JSON 对象", ui.validate_comparison_config, None)
        self.assertRaisesMessage("shared_config 必须是 JSON 对象", ui.validate_comparison_config, {"shared_config": "bad", "groups": []})
        config = {"shared_config": self.base, "groups": [{"id": "same"}, {"id": "same"}]}
        self.assertRaisesMessage("id 重复", ui.validate_comparison_config, config)
        config = {"shared_config": self.base, "groups": [{}, {}], "execution_policy": "parallel"}
        self.assertRaisesMessage("仅支持串行", ui.validate_comparison_config, config)
        for field, value in (("concurrency", 0), ("rpm", 0), ("iterations", 0)):
            config = {"shared_config": self.base, "groups": [{field: value}, {}]}
            self.assertRaises(ValueError, ui.validate_comparison_config, config)
        config = {"shared_config": self.base, "groups": [{}, {}], "seed": 123}
        self.assertRaisesMessage("暂不支持随机种子", ui.validate_comparison_config, config)

    def test_public_config_recursively_removes_credentials(self):
        cfg = {"name": "x", "api_key": self.secret_a, "nested": {"Authorization": self.secret_b, "safe": 1}}
        public = ui.public_config(cfg)
        self.assertNotIn("api_key", public)
        self.assertNotIn("nested", public)
        quality = ui._quality_public_config({"name": "q", "resources": [self.quality_resource]})
        self.assertNotIn("api_key", quality["resources"][0])
        self.assertEqual(quality["resources"][0]["model"], "model-a")

    def test_handler_json_redacts_nested_secret_fields_case_insensitively(self):
        handler = object.__new__(ui.Handler)
        writes = []
        handler.wfile = type("Writer", (), {"write": writes.append})()
        handler.send_response = lambda status: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler._json({"nested": {"API_KEY": self.secret_a, "authorization": self.secret_b, "ok": 1}})
        payload = b"".join(writes).decode()
        self.assertNotIn(self.secret_a, payload)
        self.assertNotIn(self.secret_b, payload)
        self.assertEqual(payload.count("[已隐藏]"), 2)

    def test_quality_task_snap_and_raw_result_never_expose_key(self):
        cfg = ui.validate_quality_config({"suite_path": str(SMOKE_SUITE), "resources": [self.quality_resource], "capture_raw": True})
        task = ui.QualityTask(cfg)
        snap = task.snap()
        self.assertNotIn(self.secret_a, json.dumps(snap, ensure_ascii=False))
        self.assertNotIn("api_key", json.dumps(snap, ensure_ascii=False).lower())
        task.config["resources"][0]["api_key"] = ""

    def test_quality_runner_keeps_resource_isolation_and_redacts_response_errors(self):
        cfg = ui.validate_quality_config({"suite_path": str(SMOKE_SUITE), "resources": [self.quality_resource, {
            "name": "资源 B", "url": "https://example.test/v1", "api_key": self.secret_b, "model": "model-b"
        }], "capture_raw": True})
        task = ui.QualityTask(cfg)

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False

        calls = []
        async def fake_request(client, item, payload, headers, timeout_cfg, *, chat_url, capture_raw=False):
            calls.append(headers["Authorization"])
            key = headers["Authorization"].split(" ", 1)[1]
            return {
                "status_code": 200, "completed": True, "response_text": f"answer {key}",
                "reason": f"backend echoed {key}", "ttft_ms": 1, "total_ms": 2,
                "raw_input": {"api_key": key, "messages": []}, "raw_output": f"raw {key}",
            }

        with mock.patch.object(ui.httpx, "AsyncClient", FakeClient), \
             mock.patch.object(ui, "do_request", side_effect=fake_request):
            asyncio.run(ui.run_quality_task(task))

        self.assertEqual(len(calls), task.config["case_count"] * 2)
        output = json.dumps(task.results, ensure_ascii=False)
        self.assertNotIn(self.secret_a, output)
        self.assertNotIn(self.secret_b, output)
        self.assertTrue(all(resource["api_key"] == "" for resource in task.config["resources"]))
        self.assertTrue(all("[已隐藏]" in item["case_results"][0]["response"] for item in task.results))

    def test_quality_runner_marks_resource_failures_as_finished_with_errors(self):
        cfg = ui.validate_quality_config({"suite_path": str(SMOKE_SUITE), "resources": [self.quality_resource]})
        task = ui.QualityTask(cfg)

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False

        async def failed_request(*args, **kwargs):
            raise RuntimeError("upstream unavailable")

        with mock.patch.object(ui.httpx, "AsyncClient", FakeClient), \
             mock.patch.object(ui, "do_request", side_effect=failed_request):
            asyncio.run(ui.run_quality_task(task))

        self.assertEqual(task.status, "finished_with_errors")
        self.assertIsNone(task.error)
        self.assertLess(task.results[0]["summary"]["passed_cases"], task.config["case_count"])
        self.assertLess(task.results[0]["summary"]["percentage"], 100)
        self.assertNotIn("error", task.results[0])
        self.assertEqual(len(task.results[0]["request_errors"]), task.config["case_count"])
        self.assertIn("upstream unavailable", task.results[0]["request_errors"][0]["error"])
        self.assertEqual(task.config["resources"][0]["api_key"], "")

    def test_quality_runner_records_single_request_error_details(self):
        cfg = ui.validate_quality_config({"suite_path": str(SMOKE_SUITE), "resources": [self.quality_resource]})
        task = ui.QualityTask(cfg)

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False

        async def incomplete_request(*args, **kwargs):
            return {
                "status_code": 200,
                "completed": False,
                "error_type": "PROTOCOL",
                "reason": "response ended early",
                "response_text": "partial",
                "ttft_ms": 1,
                "total_ms": 2,
            }

        with mock.patch.object(ui.httpx, "AsyncClient", FakeClient), \
             mock.patch.object(ui, "do_request", side_effect=incomplete_request):
            asyncio.run(ui.run_quality_task(task))

        self.assertEqual(task.status, "finished_with_errors")
        errors = task.results[0]["request_errors"]
        self.assertEqual(len(errors), task.config["case_count"])
        self.assertEqual(errors[0]["case_id"], "basic_ping")
        self.assertEqual(errors[0]["status_code"], 200)
        self.assertEqual(errors[0]["error_type"], "PROTOCOL")
        self.assertEqual(errors[0]["error"], "response ended early")

    def test_quality_runner_invalid_suite_sets_error_and_clears_keys(self):
        cfg = ui.validate_quality_config({"suite_path": str(SMOKE_SUITE), "resources": [self.quality_resource]})
        cfg["_suite_cases"] = [{"id":"invalid"}]
        task = ui.QualityTask(cfg)
        with self.assertRaises(Exception):
            asyncio.run(ui.run_quality_task(task))
        self.assertEqual(task.status, "error")
        self.assertIsNotNone(task.error)
        self.assertNotIn(self.secret_a, task.error)
        self.assertEqual(task.config["resources"][0]["api_key"], "")

    def test_quality_report_exposes_decision_scope_and_pairwise_metrics_without_secrets(self):
        cfg = ui.validate_quality_config({"suite_path": str(SMOKE_SUITE), "resources": [self.quality_resource, {
            "name": "资源 B", "url": "https://example.test/v1", "api_key": self.secret_b, "model": "model-b"
        }]})
        task = ui.QualityTask(cfg)
        task.status = "finished_with_errors"
        task.results = [
            {"name": "资源 A", "request_errors": [], "case_results": [
                {"case_id": "a", "completed": True, "passed": True, "score": 1, "total_ms": 100},
                {"case_id": "b", "completed": True, "passed": True, "score": 1, "total_ms": 120},
            ], "summary": {"total_cases": 2, "passed_cases": 2, "score": 1, "percentage": 100, "pass_rate": 1}},
            {"name": "资源 B", "request_errors": [{"case_id": "b", "error": "timeout"}], "case_results": [
                {"case_id": "a", "completed": True, "passed": True, "score": 1, "total_ms": 100},
                {"case_id": "b", "completed": False, "passed": False, "score": 0, "total_ms": 300},
            ], "summary": {"total_cases": 2, "passed_cases": 1, "score": .5, "percentage": 50, "pass_rate": .5}},
        ]
        for resource in task.results:
            for case in resource["case_results"]:
                case.update(prompt=case["case_id"], category="math", evaluator={"type":"math","expected":"1"})
        snap = task.snap()
        report = snap["report"]
        self.assertEqual(report["level"], "risk")
        self.assertIn("资源来源或真实性", report["decision"]["scope"])
        self.assertEqual(report["pairwise"]["pass_rate_delta_pct"], -50)
        self.assertEqual(report["pairwise"]["request_errors_delta"], 1)
        self.assertNotIn(self.secret_a, json.dumps(snap, ensure_ascii=False))
        self.assertNotIn(self.secret_b, json.dumps(snap, ensure_ascii=False))

    def test_comparison_report_marks_multiple_pressure_changes_as_not_attributable(self):
        cfg = ui.validate_comparison_config({"shared_config": self.base, "groups": [
            {"id": "base", "name": "基准组", "role": "baseline", "concurrency": 10, "rpm": 60, "iterations": 10},
            {"id": "stress", "name": "高压组", "role": "comparison", "concurrency": 20, "rpm": 120, "iterations": 10},
        ]})
        run = ui.ComparisonRun(cfg)
        run.group_results = [
            {"id": "base", "status": "finished", "summary": {"done": 10, "success_pct": 100, "rpm": {"avg": 60}, "total_ms": {"p95": 100}}},
            {"id": "stress", "status": "finished", "summary": {"done": 10, "success_pct": 100, "rpm": {"avg": 100}, "total_ms": {"p95": 110}}},
        ]
        report = run.snap()["report"]
        self.assertEqual(report["comparability"]["level"], "not_attributable")
        self.assertIn("多个核心压力变量", report["comparability"]["note"])
        self.assertFalse(report["decision"]["formal_release_allowed"])

    def test_comparison_snap_handles_missing_metrics_without_exception(self):
        cfg = ui.validate_comparison_config({"shared_config": self.base, "groups": [{"id": "a"}, {"id": "b"}]})
        run = ui.ComparisonRun(cfg)
        run.group_results = [
            {"id": "a", "name": "A", "summary": {"success_pct": 90, "rpm": {"avg": 10}, "total_ms": {"p95": 100}}},
            {"id": "b", "name": "B", "summary": {"success_pct": None, "rpm": {}, "total_ms": {}}},
        ]
        report = run.snap()["report"]
        self.assertIsNone(report["groups"][1]["delta_success_pct"])
        self.assertIsNone(report["groups"][1]["delta_avg_completed_rpm"])
        self.assertIsNone(report["groups"][1]["delta_total_p95_ms"])

    def test_comparison_manager_cleans_key_when_group_creation_fails(self):
        cfg = ui.validate_comparison_config({"shared_config": self.base, "groups": [{"id": "a"}, {"id": "b"}]})
        run = ui.ComparisonRun(cfg)
        manager = ui.ComparisonManager()
        with mock.patch.object(ui, "MANAGER") as task_manager:
            task_manager.add.side_effect = RuntimeError(f"bad key {self.secret_a}")
            manager._execute(run)
        self.assertEqual(run.status, "error")
        self.assertEqual(run.config["shared_config"]["api_key"], "")
        self.assertNotIn(self.secret_a, json.dumps(run.group_results, ensure_ascii=False))


    def make_task(self, capture=False, count=1):
        return ui.QualityTask(ui.validate_quality_config({"suite_path": str(SMOKE_SUITE), "resources": [dict(self.quality_resource, name=f"资源 {i+1}") for i in range(count)], "capture_raw": capture}))

    def test_quality_progress_advances_per_scored_persisted_case(self):
        task = self.make_task(count=2)
        snapshots = []
        async def request(*args, **kwargs):
            snap = task.snap()
            snapshots.append(snap)
            saved = json.loads(Path(task.out_path).read_text())
            self.assertEqual(saved["completed_cases"], snap["completed_cases"])
            self.assertEqual(snap["planned_cases"], 10)
            self.assertEqual(len(snap["resources"]), 2)
            return {"status_code": 200, "completed": True, "response_text": "PONG", "total_ms": 5}
        with mock.patch.object(ui, "do_request", side_effect=request):
            asyncio.run(ui.run_quality_task(task))
        self.assertEqual([x["completed_cases"] for x in snapshots], list(range(10)))
        self.assertEqual([x["progress"] for x in snapshots], [x/10 for x in range(10)])
        final = task.snap()
        self.assertEqual(final["progress"], 1)
        self.assertEqual(final["completed_resources"], 2)
        self.assertIsNone(final["current_resource"])
        self.assertEqual(final["status"], "finished")
        # Snapshots must be detached from subsequent mutable runner updates.
        self.assertEqual(snapshots[0]["resources"][0]["case_results"], [])

    def test_failed_requests_finish_evaluation_but_never_pass(self):
        task = self.make_task()
        async def request(*args, **kwargs):
            return {"status_code": 200, "completed": False, "error_type": "PROTOCOL", "response_text": "PONG", "total_ms": None}
        with mock.patch.object(ui, "do_request", side_effect=request):
            asyncio.run(ui.run_quality_task(task))
        snap = task.snap()
        self.assertEqual(snap["completed_cases"], 5)
        self.assertEqual(snap["successful_responses"], 0)
        self.assertEqual(snap["failed_cases"], 5)
        self.assertEqual(snap["progress"], 1)
        self.assertEqual(snap["status"], "finished_with_errors")
        self.assertEqual(snap["report"]["resources"][0]["health_level"], "risk")
        self.assertEqual(snap["resources"][0]["summary"]["passed_cases"], 0)
        self.assertIsNone(snap["report"]["resources"][0]["p95_ms"])

    def test_resource_initialization_failure_does_not_invent_completed_cases(self):
        task = self.make_task()
        with mock.patch.object(ui.httpx, "AsyncClient", side_effect=RuntimeError("初始化失败")):
            asyncio.run(ui.run_quality_task(task))
        snap = task.snap()
        self.assertEqual(snap["progress"], 0)
        self.assertEqual(snap["completed_cases"], 0)
        self.assertEqual(snap["status"], "finished_with_errors")
        self.assertEqual(snap["report"]["level"], "risk")
        self.assertIn("未完整", snap["report"]["title"])

    def test_disk_failure_keeps_incomplete_progress_and_error(self):
        task = self.make_task()
        actual = task.persist
        def persist():
            if task.results[0]["case_results"]:
                raise OSError("磁盘写入失败")
            actual()
        with mock.patch.object(ui, "do_request", new=mock.AsyncMock(return_value={"status_code":200,"completed":True,"response_text":"PONG"})), mock.patch.object(task,"persist",side_effect=persist):
            with self.assertRaises(OSError):
                asyncio.run(ui.run_quality_task(task))
        self.assertEqual(task.snap()["completed_cases"], 0)
        self.assertEqual(task.snap()["status"], "error")
        self.assertEqual(task.snap()["report"]["level"], "risk")

    def test_quality_evidence_restores_after_restart_and_capture_off_omits_body(self):
        for capture in (False, True):
            task = self.make_task(capture=capture)
            result = {"status_code":200,"completed":True,"response_text":"PONG", "raw_input":{"messages":[{"content":"输入"}]},"raw_output":"完整输出"}
            with mock.patch.object(ui, "do_request", new=mock.AsyncMock(return_value=result)):
                asyncio.run(ui.run_quality_task(task))
            saved = json.loads(Path(task.out_path).read_text())
            first = saved["resources"][0]["case_results"][0]
            self.assertEqual("raw_output" in first, capture)
            self.assertEqual(Path(task.out_path).stat().st_mode & 0o777, 0o600)
            ui._history_write({"quality_tests":[ui._history_safe_snapshot(saved)]})
            with mock.patch.object(ui, "MANAGER", ui.TaskManager()), mock.patch.object(ui,"QUALITY",ui.QualityManager()), mock.patch.object(ui,"COMPARISONS",ui.ComparisonManager()):
                ui.restore_history()
                restored = ui.QUALITY.get(task.id).snap()
            self.assertEqual(restored["completed_cases"], 5)
            self.assertEqual(restored["report"], saved["report"])
            if capture:
                self.assertEqual(restored["resources"][0]["case_results"][0]["raw_output"],"完整输出")

    def test_interrupted_history_is_error_not_forever_running(self):
        task = self.make_task(count=2)
        task.status = "running"
        ui._history_write({"quality_tests":[task.snap()]})
        with mock.patch.object(ui, "MANAGER", ui.TaskManager()), mock.patch.object(ui,"QUALITY",ui.QualityManager()), mock.patch.object(ui,"COMPARISONS",ui.ComparisonManager()):
            ui.restore_history()
            restored = ui.QUALITY.get(task.id).snap()
        self.assertEqual(restored["status"], "error")
        self.assertEqual(restored["progress"], 0)
        self.assertEqual(restored["planned_cases"],10)
        self.assertEqual(restored["report"]["level"],"risk")

    def test_comparison_snapshot_keeps_capture_setting_and_child_task_id(self):
        cfg = ui.validate_comparison_config({"shared_config":dict(self.base,capture_raw=True),"groups":[{},{}]})
        run = ui.ComparisonRun(cfg)
        run.group_results = [{**cfg["groups"][0], "task_id":"child", "summary":{"recent":[{"id":0}]}}]
        snap = run.snap()
        self.assertTrue(snap["config"]["capture_raw"])
        self.assertEqual(snap["groups"][0]["task_id"],"child")
        self.assertEqual(snap["groups"][0]["summary"]["recent"][0]["id"],0)

    def test_history_restores_comparison_child_ownership(self):
        child_config = ui.validate_config(self.base)
        steps, count = ui.build_plan(child_config)
        child = ui.Task(child_config, steps, count)
        child.status = "finished"
        config = ui.validate_comparison_config({"shared_config":self.base,"groups":[{},{}]})
        comparison = ui.ComparisonRun(config)
        comparison.status = "finished"
        comparison.group_results = [{**config["groups"][0],"task_id":child.id,"status":"finished","summary":{}}]
        ui._history_write({"tasks":[child.snap()],"comparisons":[comparison.snap()]})
        with mock.patch.object(ui,"MANAGER",ui.TaskManager()), mock.patch.object(ui,"COMPARISONS",ui.ComparisonManager()), mock.patch.object(ui,"QUALITY",ui.QualityManager()):
            ui.restore_history()
            self.assertEqual(ui.MANAGER.get(child.id).parent_comparison_id,comparison.id)
            self.assertEqual(ui.MANAGER.ordered(),[])


if __name__ == "__main__":
    unittest.main()
