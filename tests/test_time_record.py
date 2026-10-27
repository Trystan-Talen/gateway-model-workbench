"""统一时间格式与真实请求边界：仅使用内存传输，不访问上游。"""
import asyncio
from datetime import datetime, timezone, timedelta
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

import smart_route_load as load
from time_record import request_timing, task_timing, singapore_timestamp, normalize_timing_tree


class TimeRecordTests(unittest.TestCase):
    def test_epoch_and_offset_are_millisecond_singapore_without_local_timezone(self):
        expected = "2026-10-15T16:12:34.123+08:00"
        self.assertEqual(singapore_timestamp("2026-10-15T16:12:34.123456+08:00"), expected)
        self.assertEqual(singapore_timestamp(datetime(2026, 10, 15, 16, 12, 34, 123456,
                                                tzinfo=timezone(timedelta(hours=8)))), expected)
        self.assertEqual(singapore_timestamp(0), "1970-01-01T08:00:00.000+08:00")

    def test_offsets_convert_same_instant_across_singapore_midnight(self):
        for value in ("2026-10-15T16:00:00.123Z", "2026-10-15T09:00:00.123-07:00",
                      "2026-10-16T00:00:00.123+08:00"):
            self.assertEqual(singapore_timestamp(value), "2026-10-16T00:00:00.123+08:00")
        self.assertEqual(singapore_timestamp("2026-10-15T15:59:59.999Z"),
                         "2026-10-15T23:59:59.999+08:00")

    def test_nested_history_conversion_is_idempotent_and_preserves_original_evidence(self):
        original = {"timing": {"created_at": "2026-10-15T16:00:00.123Z", "timezone": "UTC"},
                    "recent": [{"attempts": [{"timing": {"started_at": "2026-10-15T15:59:59.999Z",
                                                           "ended_at": None, "total_ms": 123.456}}]}],
                    "created": 1792079999.999, "raw_input": {"timing": {"created_at": "keep original"}},
                    "protocol": {"timing": {"created_at": "keep original"}}}
        restored = normalize_timing_tree(original)
        self.assertEqual(restored["timing"]["timezone"], "Asia/Singapore")
        self.assertEqual(restored["timing"]["created_at"], "2026-10-16T00:00:00.123+08:00")
        self.assertEqual(restored["recent"][0]["attempts"][0]["timing"]["total_ms"], 123.456)
        self.assertEqual(restored["created"], original["created"])
        self.assertEqual(restored["raw_input"], original["raw_input"])
        self.assertEqual(restored["protocol"], original["protocol"])
        self.assertEqual(original["timing"]["created_at"], "2026-10-15T16:00:00.123Z")
        self.assertEqual(normalize_timing_tree(restored), restored)

    def test_missing_naive_and_invalid_values_are_not_invented(self):
        for value in (None, True, float("nan"), float("inf"), "2026-10-15T16:12:34", "bad"):
            with self.subTest(value=value):
                self.assertIsNone(singapore_timestamp(value))
        record = request_timing(ended_at=10, total_ms=500, queue_ms=0)
        self.assertIsNone(record["started_at"])
        self.assertEqual(record["queue_ms"], 0)
        self.assertEqual(record["source"], "partial")

    def test_elapsed_is_never_derived_from_wall_clock(self):
        record = request_timing(started_at=100, ended_at=50, total_ms=10)
        self.assertEqual(record["total_ms"], 10)
        self.assertEqual(record["source"], "recorded")
        self.assertIsNone(request_timing(started_at=100, ended_at=200)["total_ms"])
        for value in (-1, True, float("inf"), "10"):
            self.assertIsNone(request_timing(total_ms=value)["total_ms"])

    def test_round_trip_and_legacy_source_remain_explicit(self):
        record = request_timing(started_at=100, total_ms=20, source="legacy")
        self.assertEqual(request_timing(**record), record)
        self.assertIsNone(record["ended_at"])
        task = task_timing(created_at=1, started_at=2, ended_at=3, elapsed_ms=800)
        self.assertEqual(task["clock"], "client")
        self.assertEqual(task["timezone"], "Asia/Singapore")
        self.assertEqual(task["version"], 1)
        self.assertEqual(task["source"], "recorded")
        self.assertIsNone(task["interruption_detected_at"])


class MemoryStream(httpx.AsyncByteStream):
    def __init__(self, *chunks, error=None, block=None):
        self.chunks = chunks
        self.error = error
        self.block = block

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.block:
            self.block.set()
            await asyncio.Event().wait()
        if self.error:
            raise self.error


class RequestTimeTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, stream, status=200, streaming=True, item=None):
        transport = httpx.MockTransport(lambda request: httpx.Response(status, stream=stream))
        async with httpx.AsyncClient(transport=transport) as client:
            return await load.do_request(client, item or {"id": 1},
                                         {"model": "synthetic", "stream": streaming}, {},
                                         dict(connect=1, read=1, write=1, pool=1),
                                         chat_url="https://synthetic.invalid/chat/completions")

    async def test_stream_records_plan_queue_first_body_and_end_despite_wall_clock_jump(self):
        fake_clock = SimpleNamespace(time=mock.Mock(side_effect=[1000, 500, 600]),
                                     monotonic=mock.Mock(side_effect=[100, 100.5, 101.5]))
        stream = MemoryStream(b': heartbeat\n\n',
                              b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                              b'data: [DONE]\n\n')
        with mock.patch.object(load, "time", fake_clock):
            result = await self.request(stream, item={"id": 1, "scheduled_at": 900,
                                                      "enqueued_at": 950,
                                                      "_scheduled_mono": 98, "_enqueued_mono": 99})
        timing = result["timing"]
        self.assertTrue(result["completed"])
        self.assertEqual(result["ts"], 1000000)
        self.assertEqual(timing["started_at"], singapore_timestamp(1000))
        self.assertEqual(timing["first_token_at"], singapore_timestamp(500))
        self.assertEqual(timing["ended_at"], singapore_timestamp(600))
        self.assertEqual(timing["queue_ms"], 1000)
        self.assertEqual(timing["schedule_lag_ms"], 2000)
        self.assertEqual(timing["ttft_ms"], 500)
        self.assertEqual(timing["total_ms"], 1500)
        self.assertNotIn("_scheduled_mono", result)

    async def test_json_body_has_end_but_no_stream_first_token(self):
        result = await self.request(MemoryStream(b'{"choices":[{"message":{"content":"ok"}}]}'),
                                    streaming=False)
        self.assertTrue(result["completed"])
        self.assertIsNone(result["ttft_ms"])
        self.assertIsNone(result["timing"]["first_token_at"])
        self.assertIsNotNone(result["timing"]["ended_at"])

    async def test_http_timeout_and_stream_errors_keep_end_time(self):
        cases = ((MemoryStream(b'{"error":"limited"}'), 429, "HTTP_429"),
                 (MemoryStream(b'{"error":"server"}'), 503, "HTTP_5XX"),
                 (MemoryStream(error=httpx.ReadTimeout("synthetic timeout")), 200, "TIMEOUT"),
                 (MemoryStream(b'data: {"error":{"message":"synthetic failure"}}\n\n'), 200, "PROTOCOL"))
        for stream, status, expected in cases:
            with self.subTest(expected=expected):
                result = await self.request(stream, status=status)
                self.assertFalse(result["completed"])
                self.assertEqual(result["error_type"], expected)
                self.assertIsNotNone(result["timing"]["started_at"])
                self.assertIsNotNone(result["timing"]["ended_at"])
                self.assertIsNone(result["timing"]["first_token_at"])

    async def test_cancel_re_raises_with_time_evidence(self):
        blocked = asyncio.Event()
        task = asyncio.create_task(self.request(MemoryStream(
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', block=blocked)))
        await blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as raised:
            await task
        result = raised.exception.request_result
        self.assertFalse(result["completed"])
        self.assertEqual(result["error_type"], "CANCELLED")
        self.assertIsNotNone(result["timing"]["first_token_at"])
        self.assertIsNotNone(result["timing"]["ended_at"])
        self.assertGreaterEqual(result["timing"]["total_ms"], 0)


if __name__ == "__main__":
    unittest.main()
