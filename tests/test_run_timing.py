"""模块级时间证据回归：墙钟变更、历史、逐请求落盘和取消收尾。"""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import route_test_ui as ui
import routing_compare as routing
import run_timing
from run_timing import RunClock, request_evidence, restored_timing
from smart_route_load import attempt_timing
from time_record import request_timing, singapore_timestamp


class RunClockTests(unittest.TestCase):
    def test_wall_clock_rollback_does_not_change_elapsed_queue_or_drain(self):
        clock_source = SimpleNamespace(time=mock.Mock(side_effect=[100, 90, 85, 80]),
                                       monotonic=mock.Mock(side_effect=[0, 10, 15, 20]))
        with mock.patch.object(run_timing, "time", clock_source):
            clock = RunClock()
            clock.mark("started_at")
            clock.mark("dispatch_ended_at")
            clock.mark("ended_at")
            clock.mark("ended_at")
            timing = clock.snapshot()
        self.assertEqual(timing["created_at"], singapore_timestamp(100))
        self.assertEqual(timing["ended_at"], singapore_timestamp(80))
        self.assertEqual(timing["queue_ms"], 10000)
        self.assertEqual(timing["elapsed_ms"], 10000)
        self.assertEqual(timing["drain_ms"], 5000)

    def test_stopped_before_execution_keeps_start_and_elapsed_unknown(self):
        clock = RunClock()
        clock.mark("stop_requested_at")
        clock.mark("dispatch_ended_at")
        clock.mark("ended_at")
        timing = clock.snapshot()
        self.assertIsNone(timing["started_at"])
        self.assertIsNone(timing["elapsed_ms"])
        self.assertIsNone(timing["queue_ms"])
        self.assertIsNotNone(timing["stop_requested_at"])
        self.assertIsNotNone(timing["ended_at"])

    def test_finish_dispatch_preserves_last_actual_attempt_not_report_time(self):
        clock_source = SimpleNamespace(time=mock.Mock(side_effect=[100, 101, 102, 103, 109]),
                                       monotonic=mock.Mock(side_effect=[0, 1, 2, 3, 9]))
        with mock.patch.object(run_timing, "time", clock_source):
            clock = RunClock()
            clock.mark("started_at")
            clock.dispatched()
            clock.dispatched()
            clock.finish_dispatch()
            clock.mark("ended_at")
            timing = clock.snapshot()
        self.assertEqual(timing["dispatch_ended_at"], singapore_timestamp(103))
        self.assertEqual(timing["drain_ms"], 6000)

    def test_history_recovery_time_is_only_interruption_detection(self):
        with mock.patch.object(run_timing, "time", SimpleNamespace(time=lambda: 9000)):
            timing = restored_timing({"created": 10, "started_at": 20, "elapsed": 7}, interrupted=True)
        self.assertEqual(timing["created_at"], singapore_timestamp(10))
        self.assertEqual(timing["started_at"], singapore_timestamp(20))
        self.assertEqual(timing["elapsed_ms"], 7000)
        self.assertIsNone(timing["ended_at"])
        self.assertIsNone(timing["dispatch_ended_at"])
        self.assertEqual(timing["interruption_detected_at"], singapore_timestamp(9000))
        self.assertEqual(timing["source"], "legacy")

    def test_old_request_duration_does_not_manufacture_first_or_end(self):
        timing = request_evidence({"ts": 100000, "ttft_ms": 10, "total_ms": 30})
        self.assertEqual(timing["started_at"], singapore_timestamp(100))
        self.assertIsNone(timing["first_token_at"])
        self.assertIsNone(timing["ended_at"])
        self.assertEqual(timing["source"], "legacy")


class LocalClient:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class ModuleTimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="module-timing-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for field, value in (("BASE_DIR", folder.name),
                             ("HISTORY_FILE", str(self.folder / "history.json")),
                             ("TASKS_FILE", str(self.folder / "tasks.json"))):
            patcher = mock.patch.object(ui, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ui, "persist_history")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ui.httpx, "AsyncClient", LocalClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = {"url": "https://synthetic.invalid/v1", "api_key": "synthetic-secret",
                     "model": "synthetic", "mode": "smoke", "rpm": 60000,
                     "iterations": 1, "concurrency": 1}

    def gateway(self):
        config = ui.validate_config(self.base)
        steps, total = ui.build_plan(config)
        task = ui.Task(config, steps, total)
        task.open_output()
        self.addCleanup(task.close_output)
        return task

    def quality(self):
        resource = {key: self.base[key] for key in ("url", "api_key", "model")}
        resource["name"] = "合成资源"
        return ui.QualityTask(ui.validate_quality_config({
            "suite_path": str(Path(__file__).parent / "fixtures" / "quality_smoke_v1.json"),
            "resources": [resource], "capture_raw": False,
        }))

    def routing(self):
        def side(name):
            return {"name": name, "url": "https://synthetic.invalid/v1", "model": "synthetic",
                    "api_key": "synthetic-secret", "concurrency": 1, "stream": True,
                    "http2": False, "include_usage": True, "connect_timeout": 1,
                    "read_timeout": 1, "write_timeout": 1,
                    "chat_url": "https://synthetic.invalid/v1/chat/completions"}
        patcher = mock.patch.object(routing, "_results_dir", return_value=str(self.folder))
        patcher.start()
        self.addCleanup(patcher.stop)
        return routing.RouteComparison({"name": "时间证据", "tier": 1000, "rpm": 600000,
                                        "baseline": side("基准"), "candidate": side("对比")})

    @staticmethod
    def result(index=1, **overrides):
        result = {"schema_version": 3, "ts": (100 + index) * 1000, "item_id": index,
                  "step_index": 1, "status_code": 200, "completed": True,
                  "error_type": None, "response_text": "PONG", "total_ms": 25,
                  "ttft_ms": None, "timing": request_timing(started_at=100 + index,
                                                           ended_at=101 + index, total_ms=25)}
        result.update(overrides)
        return result

    async def test_gateway_jsonl_recent_and_history_preserve_same_request_times(self):
        task = self.gateway()
        result = self.result()
        result["attempts"] = [attempt_timing(result, 1)]
        task.record(result)
        row = json.loads(Path(task.out_path).read_text())
        recent = task.snap()["recent"][0]
        self.assertEqual(row["timing"], result["timing"])
        self.assertEqual(recent["timing"], row["timing"])
        self.assertEqual(recent["attempts"], row["attempts"])
        snapshot = task.snap()
        restored = ui._restore_task_snapshot(snapshot, task.out_path).snap()
        self.assertEqual(restored["recent"][0]["timing"], row["timing"])

    async def test_waiting_gateway_stop_does_not_create_start(self):
        task = self.gateway()
        manager = ui.TaskManager()
        manager.tasks[task.id] = task
        manager.queue.append(task.id)
        self.assertIsNone(manager.stop(task.id))
        snapshot = task.snap()
        self.assertEqual(snapshot["status"], "stopped")
        self.assertIsNone(snapshot["timing"]["started_at"])
        self.assertIsNone(snapshot["timing"]["elapsed_ms"])
        self.assertIsNotNone(snapshot["timing"]["stop_requested_at"])
        self.assertIsNotNone(snapshot["timing"]["ended_at"])

    async def test_gateway_restoring_old_running_record_does_not_set_end_to_now(self):
        snapshot = {"id": "legacy-time", "name": "旧记录", "created": 10,
                    "started_at": 20, "elapsed": 30, "status": "running",
                    "config": dict(self.base), "total": 1, "done": 0}
        restored = ui._restore_task_snapshot(snapshot).snap()
        self.assertEqual(restored["status"], "stopped")
        self.assertEqual(restored["timing"]["started_at"], singapore_timestamp(20))
        self.assertIsNone(restored["timing"]["ended_at"])
        self.assertIsNotNone(restored["timing"]["interruption_detected_at"])

    async def test_pressure_comparison_preserves_parent_and_each_group_time(self):
        config = ui.validate_comparison_config({"name": "时间对照", "shared_config": dict(self.base),
            "groups": [{"id": "a", "name": "基准", "role": "baseline", "iterations": 1,
                        "concurrency": 1, "rpm": 100},
                       {"id": "b", "name": "对比", "role": "comparison", "iterations": 1,
                        "concurrency": 1, "rpm": 100}]})
        children = [self.gateway(), self.gateway()]
        for index, child in enumerate(children, 1):
            child.started_at = child._clock.mark("started_at")
            child._clock.dispatched()
            child._clock.finish_dispatch()
            child.record(self.result(index))
            child.finished_at = child._clock.mark("ended_at")
            child.status = "finished"
        run = ui.ComparisonRun(config)
        with mock.patch.object(ui.MANAGER, "add", side_effect=children):
            ui.ComparisonManager()._execute(run)
        snapshot = run.snap()
        self.assertEqual(snapshot["status"], "finished")
        for key in ("created_at", "started_at", "ended_at"):
            self.assertIsNotNone(snapshot["timing"][key])
        for row, child in zip(snapshot["groups"], children):
            self.assertEqual(row["summary"]["timing"], child.snap()["timing"])
            self.assertEqual(row["summary"]["recent"][0]["timing"], child.snap()["recent"][0]["timing"])

    async def test_gateway_compatibility_retry_then_cancel_keeps_both_attempts(self):
        task = self.gateway()
        task.status = "running"
        calls = []

        async def request(client, item, *args, **kwargs):
            calls.append(dict(item))
            if len(calls) == 1:
                return self.result(1, status_code=400, completed=False, error_type="HTTP_4XX",
                                   _usage_options_rejected=True)
            cancelled = asyncio.CancelledError()
            cancelled.request_result = self.result(2, status_code=200, completed=False, error_type="CANCELLED")
            raise cancelled

        with mock.patch.object(ui, "do_request", side_effect=request), \
                mock.patch.object(ui.MANAGER, "finish", side_effect=lambda current: current.close_output()):
            await ui.run_task(task)
        row = json.loads(Path(task.out_path).read_text())
        self.assertEqual(len(calls), 2)
        self.assertIn("scheduled_at", calls[0])
        self.assertNotIn("scheduled_at", calls[1])
        self.assertEqual([a["status_code"] for a in row["attempts"]], [400, 200])
        self.assertEqual(row["attempts"][-1]["error_type"], "CANCELLED")
        self.assertEqual(row["timing"], row["attempts"][-1]["timing"])
        self.assertIsNotNone(task.snap()["timing"]["ended_at"])

    async def test_quality_every_case_time_survives_scoring_disk_and_history(self):
        task = self.quality()
        calls = []

        async def request(*args, **kwargs):
            calls.append(self.result(len(calls) + 1))
            return calls[-1]

        with mock.patch.object(ui, "do_request", side_effect=request):
            await ui.run_quality_task(task)
        saved = json.loads(Path(task.out_path).read_text())
        rows = saved["resources"][0]["case_results"]
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["timing"] for row in rows], [row["timing"] for row in calls])
        self.assertEqual([row["execution_sequence"] for row in rows], [1, 2, 3, 4, 5])
        ui._history_write({"quality_tests": [ui._history_safe_snapshot(saved)]})
        with mock.patch.object(ui, "MANAGER", ui.TaskManager()), \
                mock.patch.object(ui, "QUALITY", ui.QualityManager()), \
                mock.patch.object(ui, "COMPARISONS", ui.ComparisonManager()):
            ui.restore_history()
            restored = ui.QUALITY.get(task.id).snap()
        self.assertEqual(restored["resources"][0]["case_results"], rows)
        self.assertEqual(restored["timing"], saved["timing"])

    async def test_quality_cancel_keeps_case_evidence_and_finishes_lifecycle(self):
        task = self.quality()
        cancelled_result = self.result(1, completed=False, error_type="CANCELLED")

        async def request(*args, **kwargs):
            error = asyncio.CancelledError()
            error.request_result = cancelled_result
            raise error

        with mock.patch.object(ui, "do_request", side_effect=request):
            with self.assertRaises(asyncio.CancelledError):
                await ui.run_quality_task(task)
        saved = json.loads(Path(task.out_path).read_text())
        rows = saved["resources"][0]["case_results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timing"], cancelled_result["timing"])
        self.assertIn(saved["status"], ("stopped", "error"))
        self.assertIsNotNone(saved["timing"]["ended_at"])
        self.assertIsNotNone(saved["timing"]["dispatch_ended_at"])

    async def test_quality_stopped_before_execution_does_not_record_a_request(self):
        task = self.quality()
        manager = ui.QualityManager()
        manager.tasks[task.id] = task
        self.assertIsNone(manager.stop(task.id))
        with mock.patch.object(ui, "do_request", new=mock.AsyncMock()) as request:
            await ui.run_quality_task(task)
        request.assert_not_awaited()
        snapshot = task.snap()
        self.assertEqual(snapshot["status"], "stopped")
        self.assertIsNone(snapshot["timing"]["started_at"])
        self.assertIsNone(snapshot["timing"]["elapsed_ms"])
        self.assertEqual(snapshot["resources"][0]["case_results"], [])

    async def test_routing_cancel_keeps_retry_times_in_file_and_request_page(self):
        run = self.routing()
        calls = []

        async def request(client, item, *args, **kwargs):
            calls.append(dict(item))
            if len(calls) == 1:
                return self.result(1, status_code=400, completed=False, error_type="HTTP_4XX",
                                   _usage_options_rejected=True)
            error = asyncio.CancelledError()
            error.request_result = self.result(2, completed=False, error_type="CANCELLED")
            raise error

        run._open_outputs()
        queue = asyncio.Queue()
        await queue.put({"item_id": "routing-ab-v1-000000", "index": 0, "prompt": "synthetic",
                         "scheduled_at": 100, "enqueued_at": 100.1})
        try:
            with mock.patch.object(routing, "do_request", side_effect=request):
                with self.assertRaises(asyncio.CancelledError):
                    await run._run_side_worker("baseline", run.config["baseline"], queue,
                                               None, asyncio.Semaphore(1))
        finally:
            run._close_outputs()
        stored = json.loads(Path(run.out_paths["baseline"]).read_text())
        page = run.request_page("baseline")["items"][0]
        self.assertEqual(len(calls), 2)
        self.assertNotIn("scheduled_at", calls[1])
        self.assertEqual(stored["timing"], stored["attempts"][-1]["timing"])
        self.assertEqual(page["timing"], stored["timing"])
        self.assertEqual(page["attempts"], stored["attempts"])
        self.assertEqual(page["error_type"], "CANCELLED")

    async def test_routing_cancelled_execution_is_not_finished(self):
        run = self.routing()
        with mock.patch.object(run, "_execute_async", new=mock.AsyncMock(side_effect=asyncio.CancelledError)):
            await asyncio.to_thread(run.execute)
        snapshot = run.snap()
        self.assertIn(snapshot["status"], ("stopped", "error"))
        self.assertIsNotNone(snapshot["timing"]["ended_at"])

    async def test_routing_stopped_before_execute_has_no_start_time(self):
        run = self.routing()
        run.stop()
        with mock.patch.object(run, "_execute_async", new=mock.AsyncMock()) as execute:
            await asyncio.to_thread(run.execute)
        execute.assert_not_awaited()
        snapshot = run.snap()
        self.assertEqual(snapshot["status"], "stopped")
        self.assertIsNone(snapshot["timing"]["started_at"])
        self.assertIsNone(snapshot["timing"]["elapsed_ms"])


if __name__ == "__main__":
    unittest.main()
