"""用量兼容降级只依赖模拟传输，限流/鉴权失败不得额外请求。"""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

import routing_compare
import smart_route_load as load


def _config(stream=True):
    def side(label):
        return {
            "name": label, "url": "https://synthetic.invalid", "model": "synthetic-model",
            "api_key": "synthetic-secret", "chat_url": "https://synthetic.invalid/v1/chat/completions",
            "concurrency": 1, "stream": stream, "include_usage": True,
            "connect_timeout": 1, "read_timeout": 1, "write_timeout": 1,
        }
    return {"name": "模拟降级", "tier": 1000, "rpm": 600,
            "baseline": side("baseline"), "candidate": side("candidate")}


class UsageRetryPolicyTests(unittest.TestCase):
    def worker_requests(self, status, body, *, stream=True, repeat_error=False):
        calls = []
        run = routing_compare.RouteComparison(_config(stream))

        def respond(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1 or repeat_error:
                return httpx.Response(status, json=body)
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

        async def execute():
            queue = asyncio.Queue()
            for index in range(2):
                queue.put_nowait({"item_id": f"synthetic-{index}", "index": index,
                                  "prompt": "synthetic prompt"})
            queue.put_nowait(None)
            run.progress["inflight"] = 2
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await run._run_side_worker("candidate", run.config["candidate"],
                                           queue, client, asyncio.Semaphore(1))

        asyncio.run(execute())
        return run, calls

    def test_rate_limit_and_auth_errors_keep_usage_and_are_not_retried(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                run, calls = self.worker_requests(
                    status, {"error": {"message": "unsupported stream_options"}})
                self.assertEqual(len(calls), 2)
                self.assertEqual([p["stream_options"]["include_usage"] for p in calls], [True, True])
                self.assertTrue(run.config["candidate"]["include_usage"])
                self.assertEqual(run.records["candidate"][0]["status_code"], status)
                self.assertFalse(run.records["candidate"][0]["completed"])
                self.assertNotIn("_usage_options_rejected", run.records["candidate"][0])

    def test_unrelated_validation_error_does_not_disable_usage(self):
        for body in (
            {"error": {"message": "unknown model", "param": "model"}},
            {"error": {"message": "unknown model"}, "request": {"stream_options": {"include_usage": True}}},
            {"error": {"message": "invalid stream_options value"}},
            {"error": {"message": "unknown value", "param": "model"}, "stream_options": True},
            {"detail": [{"loc": ["body", "model"], "msg": "unknown value"},
                        {"loc": ["body", "stream_options"], "msg": "invalid value"}]},
        ):
            with self.subTest(body=body):
                run, calls = self.worker_requests(400, body)
                self.assertEqual(len(calls), 2)
                self.assertTrue(run.config["candidate"]["include_usage"])

    def test_explicit_usage_rejection_retries_once_and_disables_option(self):
        cases = (
            (400, {"error": {"message": "unknown field stream_options"}}),
            (400, {"error": {"message": "include_usage is not supported"}}),
            (422, {"detail": [{"type": "extra_forbidden", "loc": ["body", "stream_options"],
                               "msg": "Extra inputs are not permitted"}]}),
        )
        for status, body in cases:
            with self.subTest(status=status, body=body):
                run, calls = self.worker_requests(status, body)
                self.assertEqual(len(calls), 3)
                self.assertIn("stream_options", calls[0])
                self.assertNotIn("stream_options", calls[1])
                self.assertNotIn("stream_options", calls[2])
                self.assertFalse(run.config["candidate"]["include_usage"])
                self.assertEqual(len(run.records["candidate"]), 2)
                self.assertTrue(all(r["completed"] for r in run.records["candidate"]))

    def test_non_streaming_request_never_uses_usage_fallback(self):
        run, calls = self.worker_requests(400, {"error": {"message": "unknown stream_options"}}, stream=False)
        self.assertEqual(len(calls), 2)
        self.assertTrue(run.config["candidate"]["include_usage"])
        self.assertTrue(all("stream_options" not in payload for payload in calls))

    def test_rejected_retry_does_not_trigger_another_retry(self):
        run, calls = self.worker_requests(
            400, {"error": {"message": "unknown field stream_options"}}, repeat_error=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual([r["status_code"] for r in run.records["candidate"]], [400, 400])
        self.assertTrue(all("stream_options" not in payload for payload in calls[1:]))

    def test_fallback_requires_explicit_rejection_and_active_usage(self):
        rejection = {"status_code": 400, "_usage_options_rejected": True}
        self.assertTrue(load.should_retry_without_usage(rejection, True))
        self.assertFalse(load.should_retry_without_usage(rejection, False))
        self.assertFalse(load.should_retry_without_usage(rejection, True, False))
        self.assertFalse(load.should_retry_without_usage({**rejection, "usage_seen": True}, True))
        self.assertFalse(load.should_retry_without_usage({"status_code": 400}, True))

    def test_cli_does_not_retry_429_or_serialize_private_marker(self):
        calls = []

        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(429, json={"error": {"message": "rate limit"}})

        original_client = httpx.AsyncClient
        with tempfile.TemporaryDirectory(prefix="usage-retry-") as folder:
            path = Path(folder) / "results.jsonl"
            args = SimpleNamespace(
                concurrency=1, queue_size=10, rps=10000, duration=1, iterations=2,
                mode="smoke", headers={}, api_key="synthetic-secret", http2=False,
                insecure=False, url="https://synthetic.invalid", chat_url=None,
                dry_run=False, model="synthetic-model", include_usage=True,
                out=str(path), profile="smoke", grade=None, extra_messages=None,
                temperature=None, max_tokens=None, stream=True,
                connect_timeout=1, read_timeout=1, write_timeout=1,
            )
            with mock.patch.object(load.httpx, "AsyncClient", side_effect=lambda **kwargs:
                                   original_client(transport=httpx.MockTransport(respond), **kwargs)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(asyncio.run(load.run(args)), 0)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(payload["stream_options"]["include_usage"] for payload in calls))
        self.assertEqual([r["status_code"] for r in records], [429, 429])
        self.assertTrue(all("_usage_options_rejected" not in r for r in records))
        self.assertTrue(all(r["usage_retry_policy"] == load.USAGE_RETRY_POLICY for r in records))

    def test_new_run_records_policy_and_old_history_keeps_unknown_version(self):
        run = routing_compare.RouteComparison(_config())
        self.assertEqual(run.snap()["retry_policy"], load.USAGE_RETRY_POLICY)
        self.assertEqual(routing_compare.public_config(run.config)["retry_policy"], load.USAGE_RETRY_POLICY)
        old_snapshot = run.snap()
        old_snapshot.pop("retry_policy")
        restored = routing_compare.RouteComparison.from_snapshot(old_snapshot)
        self.assertIsNone(restored.snap()["retry_policy"])

    def test_terminal_report_records_executed_retry_policy(self):
        run = routing_compare.RouteComparison(_config())
        with tempfile.TemporaryDirectory(prefix="usage-policy-") as folder, \
                mock.patch.object(routing_compare, "_results_dir", return_value=folder), \
                mock.patch.object(run, "_execute_async", new=mock.AsyncMock()):
            run.execute()
        self.assertEqual(run.snap()["report"]["method"]["usage_retry_policy"], load.USAGE_RETRY_POLICY)


if __name__ == "__main__":
    unittest.main()
