"""命令行执行器的提前退出、队列故障和取消清理，只使用传输替身。"""
import asyncio
import contextlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx
import smart_route_load as load


class LoadCliDefaultTests(unittest.TestCase):
    def test_cli_defaults_match_workbench_and_keep_explicit_pressure(self):
        for options, concurrency, rpm in (([], 20, 100), (["--concurrency", "7", "--rps", "5.35"], 7, 321)):
            with self.subTest(options=options), \
                    mock.patch("sys.argv", ["smart_route_load.py", "--url", "http://synthetic.invalid", *options]), \
                    mock.patch.object(load, "run", new=mock.AsyncMock(return_value=0)) as run:
                self.assertEqual(load.main(), 0)
                args = run.await_args.args[0]
                self.assertEqual(args.concurrency, concurrency)
                self.assertAlmostEqual(args.rps * 60, rpm)
                self.assertTrue(args.include_usage, "默认必须请求 usage，才能统计缓存命中率")

    def test_cli_allows_explicitly_disabling_usage_collection(self):
        with mock.patch("sys.argv", ["smart_route_load.py", "--url", "http://synthetic.invalid",
                                      "--no-include-usage"]), \
                mock.patch.object(load, "run", new=mock.AsyncMock(return_value=0)) as run:
            self.assertEqual(load.main(), 0)
        self.assertFalse(run.await_args.args[0].include_usage)


class LoadCliLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            concurrency=2, queue_size=1, rps=10000, duration=1, iterations=20,
            mode="smoke", headers={}, api_key=None, http2=False, insecure=False,
            url="https://synthetic.invalid", chat_url=None, dry_run=False,
            model="synthetic-model", include_usage=True, out=None, profile="smoke",
            grade=None, extra_messages=None, temperature=None, max_tokens=None,
            stream=True, connect_timeout=1, read_timeout=1, write_timeout=1,
        )
        self.client = mock.Mock(aclose=mock.AsyncMock())
        patcher = mock.patch.object(load.httpx, "AsyncClient", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    async def test_dry_run_closes_client_without_opening_output(self):
        self.args.dry_run = True
        with mock.patch.object(load, "fetch_models", new=mock.AsyncMock(return_value=["synthetic-model"])), \
                mock.patch.object(load, "open") as open_output:
            self.assertEqual(await load.run(self.args), 0)
        open_output.assert_not_called()
        self.client.aclose.assert_awaited_once()

    async def test_model_discovery_error_is_preserved_and_client_closed(self):
        self.args.model = None
        with mock.patch.object(load, "fetch_models", new=mock.AsyncMock(return_value=[])):
            with self.assertRaisesRegex(SystemExit, "未发现模型"):
                await load.run(self.args)
        self.client.aclose.assert_awaited_once()

    async def test_bounded_queue_finishes_every_scheduled_request(self):
        completed = []

        async def request(client, item, *args, **kwargs):
            await asyncio.sleep(0)
            completed.append(item["id"])
            return {"status_code": 200, "completed": True}

        with mock.patch.object(load, "do_request", side_effect=request):
            self.assertEqual(await asyncio.wait_for(load.run(self.args), timeout=1), 0)
        self.assertEqual(sorted(completed), list(range(self.args.iterations)))
        self.client.aclose.assert_awaited_once()

    async def test_plan_queue_and_retry_attempts_are_retained_with_task_times(self):
        self.args.concurrency = 1
        self.args.iterations = 1
        calls = []

        async def request(client, item, *args, **kwargs):
            calls.append(dict(item))
            rejected = len(calls) == 1
            return {"status_code": 400 if rejected else 200,
                    "completed": not rejected, "error_type": "HTTP_4XX" if rejected else None,
                    "_usage_options_rejected": rejected,
                    "timing": load.request_timing(started_at=100 + len(calls), ended_at=102 + len(calls))}

        with tempfile.TemporaryDirectory(prefix="load-cli-times-") as folder:
            path = Path(folder) / "results.jsonl"
            self.args.out = str(path)
            with mock.patch.object(load, "do_request", side_effect=request):
                self.assertEqual(await load.run(self.args), 0)
            result = json.loads(path.read_text())
            task = json.loads(Path(str(path) + ".task.json").read_text())
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[0]["scheduled_at"], 1_000_000_000)
        self.assertGreaterEqual(calls[0]["enqueued_at"], calls[0]["scheduled_at"])
        self.assertIn("_enqueued_mono", calls[0])
        for field in ("scheduled_at", "enqueued_at", "_scheduled_mono", "_enqueued_mono"):
            self.assertNotIn(field, calls[1])
        self.assertEqual([row["status_code"] for row in result["attempts"]], [400, 200])
        self.assertNotEqual(result["attempts"][0]["timing"], result["attempts"][1]["timing"])
        self.assertEqual(result["timing"], result["attempts"][-1]["timing"])
        self.assertEqual(task["status"], "finished")
        self.assertEqual(task["started_requests"], 1)
        self.assertEqual(task["finished_requests"], 1)
        for field in ("created_at", "started_at", "dispatch_ended_at", "ended_at"):
            self.assertIsNotNone(task["timing"][field])

    async def test_worker_failure_cancels_blocked_producer_and_sibling(self):
        started = 0
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()
        existing_tasks = asyncio.all_tasks()

        async def request(*args, **kwargs):
            nonlocal started
            started += 1
            if started == 1:
                await sibling_started.wait()
                raise RuntimeError("synthetic worker failed")
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_cancelled.set()

        with mock.patch.object(load, "do_request", side_effect=request):
            with self.assertRaisesRegex(RuntimeError, "synthetic worker failed"):
                await asyncio.wait_for(load.run(self.args), timeout=1)
        self.assertEqual(started, 2)
        self.assertTrue(sibling_cancelled.is_set())
        self.assertFalse(asyncio.all_tasks() - existing_tasks)
        self.client.aclose.assert_awaited_once()

    async def test_external_cancellation_closes_output_and_joins_workers(self):
        request_started = asyncio.Event()
        existing_tasks = asyncio.all_tasks()

        async def request(*args, **kwargs):
            request_started.set()
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory(prefix="load-cli-lifecycle-") as folder:
            path = Path(folder) / "results.jsonl"
            self.args.out = str(path)
            real_open = open
            outputs = []

            def open_output(*args, **kwargs):
                handle = real_open(*args, **kwargs)
                outputs.append(handle)
                return handle

            with mock.patch.object(load, "do_request", side_effect=request), \
                    mock.patch.object(load, "open", side_effect=open_output):
                execution = asyncio.create_task(load.run(self.args))
                await asyncio.wait_for(request_started.wait(), timeout=1)
                execution.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await execution
            self.assertEqual(len(outputs), 1)
            self.assertTrue(outputs[0].closed)
            self.assertEqual(path.read_text(), "")
            metadata = json.loads(Path(str(path) + ".task.json").read_text())
            self.assertEqual(metadata["status"], "cancelled")
            self.assertIsNotNone(metadata["timing"]["stop_requested_at"])
            self.assertIsNotNone(metadata["timing"]["ended_at"])
        self.assertFalse(asyncio.all_tasks() - existing_tasks)
        self.client.aclose.assert_awaited_once()

    async def test_cancelled_http_attempt_is_saved_before_cli_exits(self):
        self.args.concurrency = 1
        self.args.iterations = 2
        blocked = asyncio.Event()

        class PendingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                blocked.set()
                await asyncio.Event().wait()

        @contextlib.asynccontextmanager
        async def stream(*args, **kwargs):
            yield httpx.Response(200, stream=PendingStream())

        self.client.stream = stream
        with tempfile.TemporaryDirectory(prefix="load-cli-cancel-times-") as folder:
            path = Path(folder) / "results.jsonl"
            self.args.out = str(path)
            execution = asyncio.create_task(load.run(self.args))
            await asyncio.wait_for(blocked.wait(), timeout=1)
            running = json.loads(Path(str(path) + ".task.json").read_text())
            self.assertEqual(running["status"], "running")
            self.assertIsNotNone(running["timing"]["started_at"])
            self.assertIsNone(running["timing"]["ended_at"])
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["error_type"], "CANCELLED")
            self.assertIsNotNone(rows[0]["timing"]["ended_at"])
            self.assertIsNotNone(rows[0]["timing"]["first_token_at"])
            self.assertEqual(rows[0]["attempts"][0]["timing"], rows[0]["timing"])
            ended = json.loads(Path(str(path) + ".task.json").read_text())
            self.assertEqual(ended["started_requests"], 1)
            self.assertEqual(ended["finished_requests"], 1)


if __name__ == "__main__":
    unittest.main()
