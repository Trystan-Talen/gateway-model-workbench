"""命令行执行器的提前退出、队列故障和取消清理，只使用传输替身。"""
import asyncio
import contextlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import smart_route_load as load


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
        self.assertFalse(asyncio.all_tasks() - existing_tasks)
        self.client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
