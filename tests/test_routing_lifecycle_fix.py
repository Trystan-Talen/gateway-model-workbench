"""隔离路由对照回归：全程使用内存模拟请求，结果只写临时目录。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


import routing_compare as routing


class MemoryClient:
    """故意不提供网络方法，任何漏掉的模拟都不能发出请求。"""
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def config(concurrency=3):
    def side(label):
        return {
            "name": label, "url": f"http://{label}.invalid", "model": "mock-model",
            "api_key": f"synthetic-{label}-secret", "concurrency": concurrency,
            "stream": True, "http2": False, "include_usage": True,
            "connect_timeout": 1, "read_timeout": 1, "write_timeout": 1,
            "chat_url": f"http://{label}.invalid/v1/chat/completions",
        }
    return {"name": "隔离回归", "tier": 1000, "rpm": 600000,
            "baseline": side("baseline"), "candidate": side("candidate")}


def corpus(count=12):
    return [{"item_id": f"item-{i}", "index": i, "prompt": "synthetic prompt"}
            for i in range(count)]


def result(job):
    return {
        "schema_version": 3, "ts": 1, "item_id": job["id"],
        "status_code": 200, "completed": True, "usage_seen": True,
        "usage_source": "usage", "usage_level": "FULL", "prompt_tokens": 100,
        "cached_tokens": 50, "completion_tokens": 1, "total_tokens": 101,
        "cache_hit": True, "ttft_ms": 1, "total_ms": 30,
    }


class RoutingLifecycleRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="routing-fix-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        for patcher in (
            mock.patch.object(routing, "_results_dir", return_value=str(self.temp)),
            mock.patch.object(routing.httpx, "AsyncClient", MemoryClient),
            mock.patch.object(routing.routing_corpus, "build_corpus", return_value=corpus()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_run(self, callback=None, rpm=600000):
        run = routing.RouteComparison(config(), on_change=callback)
        run.plan, run.total, run.planned_duration = routing.build_plan(1000, rpm)
        return run

    def assert_no_secrets(self, run):
        self.assertNotIn("synthetic-baseline-secret", str(run.error))
        self.assertNotIn("synthetic-candidate-secret", str(run.error))
        for side in ("baseline", "candidate"):
            self.assertNotIn("api_key", run.config[side])

    def test_configured_concurrency_is_reached_and_never_exceeded(self):
        active = {"baseline": 0, "candidate": 0}
        peak = dict(active)

        async def request(client, job, *args, chat_url, **kwargs):
            side = "baseline" if "baseline.invalid" in chat_url else "candidate"
            active[side] += 1
            peak[side] = max(peak[side], active[side])
            try:
                await asyncio.sleep(0.03)
                return result(job)
            finally:
                active[side] -= 1

        run = self.make_run()
        with mock.patch.object(routing, "do_request", side_effect=request):
            run.execute()
        self.assertEqual(run.status, "finished", run.error)
        self.assertEqual(peak, {"baseline": 3, "candidate": 3})
        self.assertEqual(active, {"baseline": 0, "candidate": 0})
        self.assertEqual([len(rows) for rows in run.records.values()], [12, 12])
        self.assertEqual(run._report["reliability"]["paired_total"], 12)

    def test_worker_exception_is_error_and_all_requests_are_collected(self):
        active = 0
        cancelled = 0

        async def request(client, job, *args, chat_url, **kwargs):
            nonlocal active, cancelled
            active += 1
            try:
                if "baseline.invalid" in chat_url:
                    await asyncio.sleep(0.02)
                    raise RuntimeError("synthetic-baseline-secret synthetic-candidate-secret")
                await asyncio.sleep(1)
                return result(job)
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1

        run = self.make_run()
        with mock.patch.object(routing, "do_request", side_effect=request):
            run.execute()
        self.assertEqual(run.status, "error")
        self.assertIn("***", run.error)
        self.assertEqual(active, 0)
        self.assertGreater(cancelled, 0)
        self.assertEqual(run.progress["inflight"], 0)
        self.assertEqual(run._files, {})
        self.assert_no_secrets(run)

    def test_result_write_exception_does_not_become_finished(self):
        async def request(client, job, *args, **kwargs):
            return result(job)

        run = self.make_run()
        with mock.patch.object(routing, "do_request", side_effect=request), \
                mock.patch.object(run, "_record", side_effect=OSError("disk full")):
            run.execute()
        self.assertEqual(run.status, "error")
        self.assertEqual(run.error, "disk full")
        self.assert_no_secrets(run)

    def test_report_exception_still_clears_secrets_and_persists_error(self):
        async def request(client, job, *args, **kwargs):
            return result(job)

        snapshots = []
        run = self.make_run(lambda: snapshots.append(run.snap()))
        with mock.patch.object(routing, "do_request", side_effect=request), \
                mock.patch.object(routing.routing_ab, "build_comparison",
                                  side_effect=RuntimeError("synthetic-candidate-secret")):
            run.execute()
        self.assertEqual(run.status, "error")
        self.assertEqual(snapshots[-1]["status"], "error")
        self.assert_no_secrets(run)

    def test_stop_wakes_slow_schedule_and_keeps_inflight_results(self):
        started = threading.Event()
        calls = []

        async def request(client, job, *args, chat_url, **kwargs):
            calls.append((chat_url, job["id"]))
            started.set()
            await asyncio.sleep(0.08)
            return result(job)

        snapshots = []
        run = self.make_run(lambda: snapshots.append(run.snap()), rpm=0.01)
        with mock.patch.object(routing, "do_request", side_effect=request):
            thread = threading.Thread(target=run.execute, daemon=True)
            thread.start()
            self.assertTrue(started.wait(2))
            run.stop()
            self.assertEqual(run.status, "stopping")
            thread.join(2)
        self.assertFalse(thread.is_alive(), "停止不应等待 6000 秒的下一次调度")
        self.assertEqual(run.status, "stopped")
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(sum(map(len, run.records.values())), len(calls))
        self.assertIn("stopping", [item["status"] for item in snapshots])
        self.assertEqual(snapshots[-1]["status"], "stopped")

    def test_stop_before_execution_never_sends_request(self):
        run = self.make_run()
        run.stop()
        with mock.patch.object(routing, "do_request") as request:
            run.execute()
        request.assert_not_called()
        self.assertEqual(run.status, "stopped")
        self.assert_no_secrets(run)

    def test_stop_does_not_start_usage_fallback_request(self):
        run = self.make_run()
        calls = []

        async def request(client, job, *args, **kwargs):
            calls.append(job["id"])
            run.stop()
            return {**result(job), "status_code": 400, "usage_seen": False,
                    "_usage_options_rejected": True}

        with mock.patch.object(routing, "do_request", side_effect=request):
            run.execute()
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.status, "stopped")

    def test_terminal_report_is_persisted_without_another_http_action(self):
        saved = threading.Event()
        snapshots = []
        history = self.temp / "history.json"

        def persist():
            data = manager.ordered()
            snapshots.append(data[0]["status"])
            history.write_text(json.dumps(data), encoding="utf-8")
            if data[0]["status"] in ("finished", "error", "stopped"):
                saved.set()

        async def request(client, job, *args, **kwargs):
            await asyncio.sleep(0.01)
            return result(job)

        manager = routing.RoutingCompareManager(on_change=persist)
        with mock.patch.object(routing, "validate_config", side_effect=lambda raw: raw), \
                mock.patch.object(routing, "do_request", side_effect=request):
            run = manager.create(config())
            self.assertTrue(saved.wait(2), "执行收尾必须主动保存索引")
        latest = json.loads(history.read_text(encoding="utf-8"))[0]
        self.assertEqual(latest["status"], "finished", latest["error"])
        self.assertIsNotNone(latest["report"])
        self.assertEqual(latest["sides"]["baseline"]["records"], 12)
        restored = routing.RouteComparison.from_snapshot(latest)
        self.assertEqual(restored.status, "finished")
        self.assertEqual(restored._report, run._report)
        self.assertEqual(snapshots, ["waiting", "running", "finished"])

    def test_delete_rejects_stopping_and_queued_runs(self):
        manager = routing.RoutingCompareManager()
        run = self.make_run()
        manager.runs[run.id] = run
        for status in ("waiting", "running", "stopping"):
            run.status = status
            self.assertIsNotNone(manager.delete(run.id))
            self.assertIs(manager.get(run.id), run)

    def test_raw_capture_switch_controls_request_and_disk_and_redacts_both_keys(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                flags = []

                async def request(client, job, *args, capture_raw=False, **kwargs):
                    flags.append(capture_raw)
                    return {**result(job), "raw_input": {"prompt": "synthetic-baseline-secret"},
                            "raw_output": "synthetic-candidate-secret",
                            "reason": "synthetic-baseline-secret synthetic-candidate-secret"}

                run = self.make_run()
                for side in ("baseline", "candidate"):
                    run.config[side]["capture_raw"] = enabled
                with mock.patch.object(routing, "do_request", side_effect=request):
                    run.execute()
                self.assertTrue(flags)
                self.assertEqual(set(flags), {enabled})
                for file in run.out_paths.values():
                    text = Path(file).read_text(encoding="utf-8")
                    self.assertNotIn("synthetic-baseline-secret", text)
                    self.assertNotIn("synthetic-candidate-secret", text)
                    record = json.loads(text.splitlines()[0])
                    self.assertEqual("raw_input" in record, enabled)
                    self.assertEqual("raw_output" in record, enabled)
                self.assertNotIn("raw_output", run.records["baseline"][0])
                self.assertEqual(run.snap()["config"]["capture_raw"], enabled)
                restored = routing.RouteComparison.from_snapshot(run.snap())
                self.assertEqual(restored.snap()["config"]["capture_raw"], enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
