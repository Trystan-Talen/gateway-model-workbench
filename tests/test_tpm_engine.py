"""Local synthetic streams only; no paid provider is contacted."""
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from tpm_config import validate_config
from tpm_engine import TpmRun
from tpm_report import build_report, metrics
from tpm_usage import normalize_usage


def config(**updates):
    raw = dict(url="https://mock.invalid/v1", model="synthetic", api_key="not-a-real-key",
               input_tokens=128, expected_output_tokens=4, max_output_tokens=16,
               calibration_requests=2, template_groups=1, warmup_requests=0,
               context_window_tokens=256, protocol_overhead_tokens=0,
               cache_write_price_mode="input", cache_mode="avoid_cache",
               concurrency=16, pool_connections=16, budget_usd=5,
               target_tpm=132 * 60 * 25, target_duration_seconds=.12,
               ramp_duration_seconds=.05, timeout_seconds=.4)
    raw.update(updates)
    return raw


def usage(inputs=128, outputs=4):
    return {"prompt_tokens": inputs, "completion_tokens": outputs,
            "prompt_tokens_details": {"cached_tokens": 0}, "total_tokens": inputs + outputs}


def sse(block):
    return ("data: " + (block if isinstance(block, str) else json.dumps(block)) + "\n\n").encode()


class Stream(httpx.AsyncByteStream):
    def __init__(self, blocks, delay=0):
        self.blocks, self.delay = blocks, delay

    async def __aiter__(self):
        for block in self.blocks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield block


class Upstream:
    def __init__(self, *, status=200, delay=0, mode="ok"):
        self.status, self.delay, self.mode = status, delay, mode
        self.starts, self.bodies = [], []

    async def __call__(self, request):
        self.starts.append(time.monotonic())
        self.bodies.append(json.loads(request.content))
        blocks = [sse({"choices": [{"delta": {"content": "合成答案"}, "finish_reason": None}]}),
                  sse({"choices": [{"delta": {}, "finish_reason": "length" if self.mode == "length" else "stop"}]}),
                  sse({"choices": [], "usage": usage()}), sse("[DONE]")]
        if self.mode == "repeated_usage":
            blocks.insert(1, sse({"choices": [], "usage": usage(64, 2)}))
            blocks.insert(2, sse({"choices": [], "usage": usage(64, 2)}))
        if self.mode == "missing_usage":
            blocks.pop(-2)
        if self.mode == "stream_error":
            blocks.insert(1, sse({"error": {"message": "synthetic failure"}}))
        if self.mode == "incomplete":
            blocks.pop()
        if self.mode == "heartbeat":
            blocks = [b": heartbeat\n\n"] * 100
        if self.mode == "usage_then_heartbeat":
            blocks = [sse({"choices": [], "usage": usage()})] + [b": heartbeat\n\n"] * 100
        if self.mode == "trailing_heartbeat":
            blocks += [b": heartbeat\n\n"] * 100
        return httpx.Response(self.status, stream=Stream(blocks, self.delay),
                              headers={"content-type": "text/event-stream"})


class TpmEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_tail_slot_wait_is_strict_and_only_refines_the_last_narrow_slot(self):
        class Clock:
            value = 1000.0

            def monotonic(self):
                return self.value

        for final_slot, margin, expected_waits in ((True, .0008, 3), (False, .0008, 2), (True, .003, 2)):
            with self.subTest(final_slot=final_slot, margin=margin):
                run, clock, waits = TpmRun(config()), Clock(), []
                due = clock.value + .02

                async def controlled_wait(seconds):
                    waits.append(seconds)
                    self.assertGreater(seconds, 0)
                    # Simulate a premature timer wake as well as the optional
                    # early wake; neither is permission to begin the request.
                    if len(waits) < expected_waits:
                        clock.value = due - (.001 if len(waits) == 1 else .0001)
                    else:
                        clock.value = due
                    await asyncio.sleep(0)

                run._sleep = controlled_wait
                with patch("tpm_engine.time", clock):
                    await run._wait_for_slot(due, due + margin, final_slot=final_slot)
                self.assertEqual(len(waits), expected_waits)
                self.assertAlmostEqual(waits[0], .018 if final_slot and margin < .002 else .02)
                self.assertEqual(clock.value, due, "an early timer wake must never admit early")

    async def test_tail_slot_wait_obeys_stop_and_cancel_in_either_wait(self):
        class Clock:
            value = 1000.0

            def monotonic(self):
                return self.value

        for interruption in ("stop", "cancel"):
            for wait_number in (1, 2):
                with self.subTest(interruption=interruption, wait_number=wait_number):
                    run, clock, waits = TpmRun(config()), Clock(), []
                    reached = asyncio.Event()

                    async def controlled_wait(seconds):
                        waits.append(seconds)
                        if len(waits) == wait_number:
                            reached.set()
                            if interruption == "stop":
                                run._stop.set()
                            else:
                                await asyncio.Event().wait()
                        else:
                            clock.value += seconds

                    run._sleep = controlled_wait
                    with patch("tpm_engine.time", clock):
                        task = asyncio.create_task(run._wait_for_slot(1000.02, 1000.0208, final_slot=True))
                        await reached.wait()
                        if interruption == "cancel":
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        else:
                            await task
                    self.assertEqual(len(waits), wait_number)
                    self.assertLess(clock.value, 1000.02)

    async def test_stage_deadline_rejects_each_unsent_admission_boundary(self):
        # Deterministic clock advances model coroutine/pool delay, long body
        # preparation, ledger lock contention and final metadata preparation.
        for boundary in ("pool", "body", "reserve", "metadata", "stop_after_reserve"):
            with self.subTest(boundary=boundary):
                class Clock:
                    value = 1000.0

                    def monotonic(self):
                        return self.value

                    def time(self):
                        return 1700000000 + self.value - 1000

                clock, server = Clock(), Upstream()
                run = TpmRun(config(), transport=httpx.MockTransport(server))
                await run._open()
                run.phase = run.status = "running"
                deadline = 1000.001
                body_calls = []

                class Corpus:
                    def request_bytes(self, *args, **kwargs):
                        body_calls.append(True)
                        if boundary == "body":
                            clock.value = deadline
                        return b'{}'

                run.corpus = Corpus()
                original_reserve = run.ledger.reserve

                def reserve(*args):
                    result = original_reserve(*args)
                    if boundary == "reserve":
                        clock.value = deadline
                    elif boundary == "stop_after_reserve":
                        run._stop.set()
                    return result

                from time_record import request_timing as real_request_timing

                def prepare_timing(**kwargs):
                    value = real_request_timing(**kwargs)
                    if boundary == "metadata":
                        clock.value = deadline
                    return value

                try:
                    with patch("tpm_engine.time", clock), patch.object(run.ledger, "reserve", side_effect=reserve) as reserved, \
                            patch("tpm_engine.request_timing", side_effect=prepare_timing):
                        if boundary == "pool":
                            await run._pool.acquire()
                            # Block every pool permit so the coroutine really waits.
                            for _ in range(run.config["pool_connections"] - 1):
                                await run._pool.acquire()
                            task = asyncio.create_task(run._request("formal", 3, due=1000, deadline=deadline))
                            await asyncio.sleep(0)
                            clock.value = deadline
                            run._pool.release()
                            row = await task
                        else:
                            row = await run._request("formal", 3, due=1000, deadline=deadline)
                    self.assertIsNone(row)
                    self.assertEqual(reserved.call_count, int(boundary not in {"pool", "body"}))
                    self.assertEqual(len(body_calls), int(boundary != "pool"))
                    self.assertEqual(server.starts, [], "a rejected admission must never reach the upstream")
                    self.assertEqual(list(run.requests), [])
                    self.assertEqual(run._inflight, {})
                    reason = "admission_stopped" if boundary == "stop_after_reserve" else "stage_elapsed"
                    self.assertEqual(dict(run._missed_reasons), {reason: 1})
                    self.assertEqual(dict(run._missed_by_stage["3"]), {reason: 1})
                    fees = run.ledger.snapshot()
                    self.assertEqual((fees["known_cost_usd"], fees["reserved_cost_usd"],
                                      fees["unknown_requests"], fees["inflight_reservations"]), (0, 0, 0, 0))
                    if reserved.call_count:
                        self.assertFalse(run.ledger.release_unsent(f"{run.id}-1"), "unsent release is idempotent")
                finally:
                    await run._close()

    async def test_admitted_request_may_finish_after_its_stage_deadline(self):
        mono_origin = time.monotonic()

        class Clock:
            value = mono_origin

            def monotonic(self):
                return self.value

            def time(self):
                return 1700000000 + self.value - mono_origin

        clock, server = Clock(), Upstream()

        async def finish_after_stage(request):
            clock.value = mono_origin + .01
            return await server(request)

        run = TpmRun(config(), transport=httpx.MockTransport(finish_after_stage))
        await run._open()
        run.phase = run.status = "running"

        class Corpus:
            def request_bytes(self, *args, **kwargs):
                return b'{}'

        run.corpus = Corpus()
        try:
            with patch("tpm_engine.time", clock):
                row = await run._request("formal", 3, due=mono_origin - .001, deadline=mono_origin + .001)
            self.assertTrue(row["success"])
            self.assertEqual(len(server.starts), 1)
            self.assertGreater(row["latency_seconds"], row["stage_deadline_margin_seconds"])
            self.assertAlmostEqual(row["stage_deadline_margin_seconds"], .001)
            self.assertAlmostEqual(row["signed_schedule_delay_seconds"], .001)
            self.assertEqual(row["schedule_delay_seconds"], row["signed_schedule_delay_seconds"])
            self.assertEqual(run._missed, 0)
            self.assertEqual(run.ledger.snapshot()["reserved_cost_usd"], 0)
        finally:
            await run._close()

    async def test_total_timeout_keeps_admission_deadline_after_local_timing_work(self):
        class Clock:
            value = 1000.0

            def monotonic(self):
                return self.value

            def time(self):
                return 1700000000 + self.value - 1000

        clock, server, deadlines = Clock(), Upstream(), []
        run = TpmRun(config(timeout_seconds=.1), transport=httpx.MockTransport(server))
        await run._open()
        run.phase = run.status = "running"

        class Corpus:
            def request_bytes(self, *args, **kwargs):
                return b'{}'

        run.corpus = Corpus()
        from time_record import request_timing as real_request_timing

        def delayed_metadata(**kwargs):
            result = real_request_timing(**kwargs)
            if kwargs.get("ended_at") is None:
                clock.value += .075
            return result

        @asynccontextmanager
        async def capture_deadline(when):
            # Capture the absolute deadline contract without handing the virtual
            # clock's epoch to asyncio's real timer. Real heartbeat/deadline tests
            # separately exercise cancellation while streaming.
            deadlines.append((when, clock.monotonic()))
            yield

        try:
            with patch("tpm_engine.time", clock), patch("tpm_engine.request_timing", side_effect=delayed_metadata), \
                    patch("tpm_engine.asyncio.timeout_at", side_effect=capture_deadline):
                row = await run._request("formal", 4, due=1000, deadline=1001)
            self.assertTrue(row["success"])
            self.assertEqual(len(deadlines), 1)
            absolute_deadline, entered_at = deadlines[0]
            self.assertAlmostEqual(absolute_deadline, 1000.1)
            self.assertAlmostEqual(entered_at, 1000.075)
            self.assertAlmostEqual(absolute_deadline - entered_at, .025)
            self.assertEqual(row["signed_schedule_delay_seconds"], 0)
            self.assertAlmostEqual(row["latency_seconds"], .075)
            self.assertEqual(len(server.starts), 1)
        finally:
            await run._close()

    async def test_arrival_wait_timeout_stop_and_external_cancellation(self):
        run = TpmRun(config())
        run._loop = asyncio.get_running_loop()
        run._wake = asyncio.Event()
        started = time.monotonic()
        await run._sleep(.01)
        self.assertGreaterEqual(time.monotonic() - started, .01)
        waiting = asyncio.create_task(run._sleep(60))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertFalse(run._wake.is_set())
        waiting = asyncio.create_task(run._sleep(60))
        await asyncio.sleep(0)
        run.stop()
        await asyncio.wait_for(waiting, .2)
        await asyncio.wait_for(run._sleep(60), .2)

    async def test_writer_flushes_each_batch_off_loop_before_acknowledging(self):
        run = TpmRun(config())
        calls = []
        loop_thread = threading.get_ident()

        class Sink:
            def write(self, payload):
                calls.append(("write", threading.get_ident(), payload))

            def flush(self):
                calls.append(("flush", threading.get_ident(), run._writer._unfinished_tasks))

        run._sink = Sink()
        run._writer = asyncio.Queue()
        for number in range(2):
            run._writer.put_nowait(json.dumps({"number": number}, separators=(",", ":")))
        writer = asyncio.create_task(run._write_records())
        await asyncio.wait_for(run._writer.join(), .5)
        self.assertEqual([call[0] for call in calls], ["write", "flush"])
        self.assertNotEqual(calls[0][1], loop_thread)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[1][2], 2)
        self.assertEqual([json.loads(line)["number"] for line in calls[0][2].splitlines()], [0, 1])
        await run._writer.put(None)
        await writer

    async def test_failed_flush_keeps_evidence_unacknowledged(self):
        run = TpmRun(config())

        class Sink:
            def write(self, payload):
                pass

            def flush(self):
                raise OSError("synthetic flush failure")

        run._sink = Sink()
        run._writer = asyncio.Queue()
        run._writer.put_nowait('{"number":1}')
        with self.assertRaisesRegex(OSError, "synthetic flush failure"):
            await run._write_records()
        self.assertEqual(run._writer._unfinished_tasks, 1)

    async def prepared(self, upstream=None, **updates):
        upstream = upstream or Upstream()
        run = TpmRun(config(**updates), transport=httpx.MockTransport(upstream))
        await run.calibrate()
        self.assertEqual(run.phase, "awaiting_run", run.calibration)
        return run, upstream

    async def test_active_elapsed_uses_monotonic_clock_then_freezes_at_end(self):
        fresh = TpmRun(config())
        self.assertIsNone(fresh.snap()["timing"]["elapsed_ms"])
        run, _ = await self.prepared()
        initial = run.snap()["timing"]["elapsed_ms"]
        self.assertGreater(initial, 0)
        self.assertIsNone(run.snap()["timing"]["ended_at"])
        await asyncio.sleep(.01)
        self.assertGreater(run.snap()["timing"]["elapsed_ms"], initial)
        run.stop()
        final = run.snap()["timing"]["elapsed_ms"]
        self.assertIsNotNone(run.snap()["timing"]["ended_at"])
        await asyncio.sleep(.01)
        self.assertEqual(run.snap()["timing"]["elapsed_ms"], final)
        self.assertEqual(run.report["timing"]["elapsed_ms"], final)

    async def test_request_and_task_timing_survives_summary_export_and_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            run = TpmRun(config(), output_path=path, transport=httpx.MockTransport(Upstream(delay=.001)))
            await run.calibrate()
            self.assertIsNone(run.snap()["timing"]["ended_at"])
            await run.run()
            timing = run.snap()["timing"]
            self.assertRegex(timing["created_at"], r"^\d{4}-\d\d-\d\dT.*\.\d{3}\+08:00$")
            self.assertLessEqual(timing["created_at"], timing["started_at"])
            self.assertLessEqual(timing["dispatch_ended_at"], timing["ended_at"])
            self.assertGreater(timing["elapsed_ms"], 0)
            self.assertGreaterEqual(timing["drain_ms"], 0)
            self.assertEqual(run.report["timing"], timing)
            persisted = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["timing"] for row in persisted], [row["timing"] for row in run.export()["requests"]])
            for row in persisted:
                measured = row["timing"]
                self.assertEqual(measured["source"], "recorded")
                self.assertLessEqual(measured["enqueued_at"], measured["started_at"])
                self.assertLessEqual(measured["started_at"], measured["first_token_at"])
                self.assertLessEqual(measured["first_token_at"], measured["ended_at"])
                self.assertAlmostEqual(measured["total_ms"], row["latency_seconds"] * 1000, places=3)
                if row["phase"] == "formal":
                    self.assertIsNotNone(measured["scheduled_at"])
                    self.assertGreaterEqual(measured["schedule_lag_ms"], 0)
                else:
                    self.assertIsNone(measured["scheduled_at"])

    async def test_wall_clock_rollback_does_not_change_request_duration(self):
        run, _ = await self.prepared()
        clock = [1700000000.0]
        upstream = Upstream(delay=.001)
        async def rollback(request):
            clock[0] -= 100
            return await upstream(request)
        run._transport = httpx.MockTransport(rollback)
        await run._open()
        try:
            with patch("tpm_engine.time.time", side_effect=lambda: clock[0]):
                row = await run._request("calibration")
            self.assertLess(row["timing"]["ended_at"], row["timing"]["started_at"])
            self.assertGreater(row["timing"]["total_ms"], 0)
            self.assertGreater(row["timing"]["ttft_ms"], 0)
        finally:
            await run._close()

    async def test_calibration_recomputes_rate_and_preserves_hard_output_limit(self):
        run, server = await self.prepared(expected_output_tokens=8)
        self.assertAlmostEqual(run.plan["target_rps"], run.config["target_tpm"] / 132 / 60)
        self.assertEqual(run.calibration["mean_output_tokens"], 4)
        self.assertTrue(all(body["max_tokens"] == 16 and body["stream"] for body in server.bodies))
        self.assertGreater(run.calibration["request_body_bytes"], 500)
        self.assertGreater(run.ledger.snapshot()["known_cost_usd"], 0)
        self.assertNotIn("api_key", run.snap()["config"])
        await run.run()
        self.assertIn(run.status, {"finished", "finished_with_errors"})
        self.assertGreater(len(server.starts), 2)
        self.assertFalse(run.report["primary_window"]["valid"])

    async def test_auto_calibration_rebuilds_actual_pool_and_scheduler_capacity(self):
        server = Upstream(delay=.02)
        run = TpmRun(config(execution_mode="auto", expected_output_tokens=8), transport=httpx.MockTransport(server))
        initial_capacity = run.config["concurrency"]
        initial_plan_id = run.plan_id
        real_client = httpx.AsyncClient
        with patch("tpm_engine.httpx.AsyncClient", wraps=real_client) as clients:
            with patch("tpm_engine.percentile", return_value=.08):
                await run.calibrate()
            self.assertEqual(run.phase, "awaiting_run")
            self.assertEqual(len(server.starts), 2)
            self.assertNotEqual(run.plan_id, initial_plan_id)
            self.assertEqual(run.plan["execution_plan"]["source"], "calibration_p95")
            self.assertAlmostEqual(run.plan["target_rps"], 25)
            self.assertEqual((run.config["concurrency"], run.config["pool_connections"]), (3, 3))
            self.assertEqual(run.config["budget_usd"], 5)
            self.assertEqual(clients.call_args_list[0].kwargs["limits"].max_connections, initial_capacity)
            await run.run()
            self.assertEqual(clients.call_count, 2)
            limits = clients.call_args_list[1].kwargs["limits"]
            self.assertEqual((limits.max_connections, limits.max_keepalive_connections), (3, 3))
        events = []
        for row in run.requests:
            if row["phase"] == "formal":
                events.extend(((row["started_at"], 1), (row["ended_at"], -1)))
        active = peak = 0
        for _, change in sorted(events):
            active += change
            peak = max(peak, active)
        self.assertGreaterEqual(peak, 2)
        self.assertLessEqual(peak, 3)
        self.assertEqual(active, 0)

    async def test_manual_execution_capacity_is_not_overwritten_by_calibration(self):
        with patch("tpm_engine.percentile", return_value=.08):
            run, _ = await self.prepared(execution_mode="manual", concurrency=2, pool_connections=7)
        self.assertEqual(run.plan["required_concurrency"], 3)
        self.assertEqual((run.config["concurrency"], run.config["pool_connections"]), (2, 7))
        self.assertEqual((run.plan["concurrency"], run.plan["pool_connections"]), (2, 7))

    async def test_repeated_stream_usage_only_final_snapshot_billed(self):
        run, _ = await self.prepared(Upstream(mode="repeated_usage"))
        self.assertTrue(all(row["usage"]["total_tokens"] == 132 for row in run.requests))
        expected = 2 * (128 * .304 + 4 * 1.064) / 1e6
        self.assertAlmostEqual(run.ledger.snapshot()["known_cost_usd"], expected)

    async def test_http_and_stream_errors_do_not_retry_or_pass(self):
        for mode, status, expected in (("ok", 429, "http_429"), ("ok", 500, "http_5xx"),
                                      ("stream_error", 200, "stream_error"),
                                      ("length", 200, "output_limit"),
                                      ("incomplete", 200, "incomplete_stream")):
            with self.subTest(mode=mode, status=status):
                server = Upstream(mode=mode, status=status)
                run = TpmRun(config(), transport=httpx.MockTransport(server))
                await run.calibrate()
                self.assertEqual(len(server.starts), 2)
                self.assertTrue(all(row["error"] == expected for row in run.requests))
                self.assertTrue(all(row["error_detail"]["code"] == expected for row in run.requests))
                self.assertTrue(all("ended_at" in row["error_detail"] for row in run.requests))
                for row in run.requests:
                    self.assertIsNotNone(row["timing"]["started_at"])
                    self.assertIsNotNone(row["timing"]["ended_at"])
                    self.assertGreaterEqual(row["timing"]["total_ms"], 0)
                    if status != 200:
                        self.assertIsNone(row["timing"]["first_token_at"])
                self.assertEqual(run.status, "error")
                if mode == "length":
                    self.assertTrue(all(row["output_limited"] for row in run.requests))
                    self.assertGreater(run.ledger.snapshot()["known_cost_usd"], 0)
                else:
                    self.assertGreater(run.ledger.snapshot()["reserved_cost_usd"], 0)

    async def test_missing_usage_blocks_claim_and_keeps_unknown_reserve(self):
        run = TpmRun(config(), transport=httpx.MockTransport(Upstream(mode="missing_usage")))
        await run.calibrate()
        self.assertFalse(run.calibration["passed"])
        self.assertGreater(run.ledger.snapshot()["reserved_cost_usd"], 0)
        self.assertEqual(run.ledger.snapshot()["unknown_requests"], 2)

    async def test_total_timeout_expires_despite_heartbeats(self):
        server = Upstream(mode="heartbeat", delay=.015)
        run = TpmRun(config(timeout_seconds=.05), transport=httpx.MockTransport(server))
        began = time.monotonic()
        await run.calibrate()
        self.assertLess(time.monotonic() - began, .3)
        self.assertTrue(all(row["error"] == "timeout" for row in run.requests))
        for row in run.requests:
            self.assertIsNotNone(row["timing"]["ended_at"])
            self.assertIsNone(row["timing"]["first_token_at"])
            self.assertGreaterEqual(row["timing"]["total_ms"], 45)
        self.assertGreater(run.ledger.snapshot()["reserved_cost_usd"], 0)

    async def test_done_ends_stream_without_waiting_for_connection_close(self):
        server = Upstream(mode="trailing_heartbeat", delay=.005)
        run, _ = await self.prepared(server, timeout_seconds=.05)
        self.assertTrue(all(row["success"] for row in run.requests))

    async def test_nonfinal_usage_snapshot_cannot_release_timeout_bill_reserve(self):
        server = Upstream(mode="usage_then_heartbeat", delay=.01)
        run = TpmRun(config(timeout_seconds=.05), transport=httpx.MockTransport(server))
        await run.calibrate()
        self.assertTrue(all(row["error"] == "timeout" for row in run.requests))
        self.assertTrue(all(row["usage"]["raw"] == usage() for row in run.requests))
        self.assertTrue(all(not row["usage"]["billing_complete"] for row in run.requests))
        self.assertGreater(run.ledger.snapshot()["reserved_cost_usd"], 0)

    async def test_template_warmups_are_sequential_and_charged(self):
        run, _ = await self.prepared(cache_mode="prefer_cache", template_groups=3, warmup_requests=2)
        warmups = [row for row in run.requests if row["phase"] == "warmup"]
        self.assertEqual([row["group"] for row in warmups], [0, 0, 1, 1, 2, 2])
        self.assertTrue(all(left["ended_at"] <= right["started_at"] for left, right in zip(warmups, warmups[1:])))
        self.assertGreater(sum(row["known_cost_usd"] for row in warmups), 0)

    async def test_every_template_requires_complete_reliable_warmup(self):
        for failure in ("missing_usage", "output_over_limit", "context_overflow", "http_failure"):
            with self.subTest(failure=failure):
                server = Upstream()

                async def handler(request):
                    response = await server(request)
                    if len(server.starts) != 2:  # First warmup, following the initial calibration probe.
                        return response
                    if failure == "http_failure":
                        return httpx.Response(500)
                    invalid_usage = usage(250, 4) if failure == "context_overflow" else usage(128, 17) if failure == "output_over_limit" else None
                    blocks = [sse({"choices": [{"delta": {"content": "合成答案"}, "finish_reason": "stop"}]})]
                    if invalid_usage is not None:
                        blocks.append(sse({"choices": [], "usage": invalid_usage}))
                    blocks.append(sse("[DONE]"))
                    return httpx.Response(200, stream=Stream(blocks))

                run = TpmRun(config(cache_mode="prefer_cache", template_groups=2, warmup_requests=2),
                             transport=httpx.MockTransport(handler))
                await run.calibrate()
                self.assertEqual(run.status, "error")
                self.assertFalse(run.calibration["passed"])
                self.assertTrue(any("预热" in issue for issue in run.calibration["issues"]))
                with self.assertRaises(ValueError):
                    await run.run()

    async def test_low_cache_hit_stops_without_switching_to_uncached(self):
        run, _ = await self.prepared(cache_mode="prefer_cache", target_tpm=132 * 60 * 100,
                                     target_duration_seconds=.6)
        await run.run()
        self.assertEqual(run.stop_reason, "cache_hit_low")
        self.assertEqual(run.config["cache_mode"], "prefer_cache")
        self.assertGreater(run.snap()["metrics"]["missed"], 0)

    async def test_final_metrics_are_frozen_when_opening_history_later(self):
        run, _ = await self.prepared()
        await run.run()
        baseline = run.snap()["metrics"]["full_token_tpm"]
        self.assertGreater(baseline, 0)
        with patch("tpm_engine.time.time", return_value=time.time() + 600):
            self.assertEqual(run.snap()["metrics"]["full_token_tpm"], baseline)

    async def test_live_statistics_match_completed_evidence_and_summary_skips_evidence(self):
        run, _ = await self.prepared()
        await run.run()
        for now in (run.finished_at, run.finished_at + 10, run.finished_at + 60):
            expected = metrics(run.requests, now=now, formal_started_at=run.formal_started_at,
                               planned=run._planned, missed=run._missed)
            self.assertEqual(run._metrics(now), {**expected, "all_in_flight": 0})

        class DoNotCopy:
            def __deepcopy__(self, memo):
                raise AssertionError("list summary must not copy long evidence")

        run.series = run.report = run.calibration = DoNotCopy()
        snapshot = run.snap(summary=True)
        self.assertNotIn("series", snapshot)
        self.assertNotIn("recent_requests", snapshot)
        snapshot["metrics"]["errors"]["caller_change"] = 1
        snapshot["fees"]["known_cost_usd"] = -1
        self.assertNotIn("caller_change", run.snap(summary=True)["metrics"]["errors"])
        self.assertGreaterEqual(run.snap(summary=True)["fees"]["known_cost_usd"], 0)

    def assert_missed_reasons_reconcile(self, run):
        snap = run.snap()
        reasons = snap["scheduling"]["missed_by_reason"]
        self.assertEqual(sum(reasons.values()), snap["metrics"]["missed"])
        self.assertEqual(sum(sum(stage.values()) for stage in snap["scheduling"]["missed_by_stage"].values()),
                         snap["metrics"]["missed"])
        return reasons

    async def test_interruption_retains_bill_reserve(self):
        run, server = await self.prepared(target_duration_seconds=.5)
        server.mode, server.delay = "heartbeat", .01
        task = asyncio.create_task(run.run())
        while len(server.starts) == 2:
            await asyncio.sleep(.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(run.status, "error")
        self.assertEqual(run.stop_reason, "interrupted")
        self.assertGreater(run.ledger.snapshot()["reserved_cost_usd"], 0)
        self.assertEqual(run.report["planned"], run.report["actual_started"] + run.report["missed"])
        self.assertGreater(self.assert_missed_reasons_reconcile(run).get("not_admitted", 0), 0)

    async def test_scheduler_exception_reconciles_unstarted_plan_without_retry(self):
        run, server = await self.prepared()

        async def broken_sleep(seconds):
            raise RuntimeError("synthetic scheduling failure")

        run._sleep = broken_sleep
        with self.assertRaises(RuntimeError):
            await run.run()
        self.assertEqual(run.status, "error")
        self.assertEqual(len(server.starts), 2)
        self.assertEqual(run.report["actual_started"], 0)
        self.assertEqual(run.report["planned"], run.report["missed"])
        self.assertEqual(self.assert_missed_reasons_reconcile(run)["not_admitted"], run.report["planned"])

    async def test_stop_drains_inflight_and_does_not_send_queued_work(self):
        run, server = await self.prepared(target_duration_seconds=.5)
        server.delay = .03
        task = asyncio.create_task(run.run())
        while len(server.starts) == 2:
            await asyncio.sleep(.001)
        run.stop()
        sent = len(server.starts)
        self.assertEqual(run.phase, "draining")
        await task
        self.assertEqual(len(server.starts), sent)
        self.assertEqual(run.status, "stopped")
        self.assertTrue(run.requests[-1]["success"])
        self.assertGreater(run.report["drain_seconds"], 0)
        timing = run.snap()["timing"]
        self.assertIsNotNone(timing["stop_requested_at"])
        self.assertLessEqual(timing["stop_requested_at"], timing["dispatch_ended_at"])
        self.assertLessEqual(timing["dispatch_ended_at"], timing["ended_at"])
        self.assertGreater(timing["drain_ms"], 0)
        self.assertEqual(run.ledger.snapshot()["reserved_cost_usd"], 0)
        reasons = self.assert_missed_reasons_reconcile(run)
        self.assertGreater(reasons.get("dispatch_stopped", 0) + reasons.get("not_admitted", 0), 0)

    async def test_end_of_dispatch_preserves_original_deadline(self):
        run, server = await self.prepared(timeout_seconds=.1)
        server.mode, server.delay = "heartbeat", .02
        started = time.monotonic()
        await run.run()
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(all(row["error"] == "timeout" for row in run.requests if row["phase"] == "formal"))

    async def test_capacity_drops_slots_without_backlog_or_compensation_burst(self):
        run, server = await self.prepared(concurrency=1, pool_connections=1, target_tpm=132 * 60 * 100)
        server.delay = .03
        await run.run()
        self.assertGreater(run.snap()["metrics"]["missed"], 0)
        self.assertEqual(run.snap()["metrics"]["in_flight"], 0)
        formal = server.starts[2:]
        self.assertTrue(all(right - left >= .1 for left, right in zip(formal, formal[1:])))
        self.assertGreater(self.assert_missed_reasons_reconcile(run).get("concurrency_limit", 0), 0)

    async def test_scheduler_lag_discards_elapsed_slots(self):
        run, server = await self.prepared(target_tpm=132 * 60 * 100)
        wall_origin, mono_origin = time.time(), time.monotonic()

        class Clock:
            value = mono_origin

            def monotonic(self):
                return self.value

            def time(self):
                return wall_origin + self.value - mono_origin

        clock = Clock()
        frozen, offset = [], 0
        for stage in run.plan["stages"]:
            for slot in range(stage["planned_requests"]):
                frozen.append((stage["index"], slot, mono_origin + offset + slot / stage["rps"]))
            offset += stage["duration_seconds"]
        target_late_due = next(due for stage, slot, due in frozen if (stage, slot) == (4, 2))
        injections, starts = [], []

        async def controlled_wait(seconds):
            # Let each admitted task start before advancing virtual time. The
            # real event loop clock is untouched; no real timer tolerance is used.
            await asyncio.sleep(0)
            wake = clock.value + max(0, seconds)
            if not injections:
                # Stage 1 slots are 0 / 40 ms, ending at 50 ms. Waking at 48 ms
                # correctly leaves only 2 ms before stage 2's first planned slot.
                wake = mono_origin + .048
                injections.append("initial")
            elif len(injections) == 1 and abs(wake - target_late_due) < 1e-9:
                # At 100 RPS, 25 ms lateness discards exactly slots 2 and 3.
                wake += .025
                injections.append("target")
            clock.value = wake

        async def record_admission(phase, stage_index=None, due=None, group=0, *, deadline=None):
            self.assertFalse(run._stop.is_set())
            started = clock.monotonic()
            expected_deadline = mono_origin + sum(stage["duration_seconds"] for stage in run.plan["stages"]
                                                 if stage["index"] <= stage_index)
            self.assertAlmostEqual(deadline, expected_deadline, places=8)
            self.assertLess(started, deadline)
            starts.append((stage_index, due, started))
            row = {"id": f"synthetic-admission-{len(starts)}", "phase": phase,
                   "stage_index": stage_index, "scheduled_at": wall_origin + due - mono_origin,
                   "started_at": clock.time(), "ended_at": clock.time(),
                   "success": True, "usage": normalize_usage(usage()),
                   "ttft_seconds": 0, "latency_seconds": 0, "pool_wait_seconds": 0,
                   "schedule_delay_seconds": started - due, "error": None}
            with run._lock:
                run.requests.append(row)
            return row

        run._sleep, run._request = controlled_wait, record_admission
        with patch("tpm_engine.time", clock), patch("task_status.time", clock):
            await run.run()

        missing = {(1, 0), (4, 2), (4, 3)}
        expected = [(stage, slot, due) for stage, slot, due in frozen if (stage, slot) not in missing]
        self.assertEqual(injections, ["initial", "target"])
        self.assertEqual(len(starts), len(expected))
        self.assertEqual(len({(stage, due) for stage, due, _ in starts}), len(starts))
        by_slot = {}
        for actual, planned in zip(starts, expected):
            stage, due, started = actual
            expected_stage, slot, expected_due = planned
            self.assertEqual(stage, expected_stage)
            self.assertAlmostEqual(due, expected_due, places=8)
            self.assertGreaterEqual(started, due)
            expected_start = (mono_origin + .048 if (stage, slot) == (1, 1)
                              else target_late_due + .025 if (stage, slot) == (4, 4)
                              else expected_due)
            self.assertAlmostEqual(started, expected_start, places=8)
            by_slot[(stage, slot)] = started
        # A short cross-stage interval is legitimate, not evidence of catch-up.
        self.assertAlmostEqual(by_slot[(2, 0)] - by_slot[(1, 1)], .002, places=8)
        self.assertEqual(self.assert_missed_reasons_reconcile(run), {"scheduler_late": 3})
        self.assertEqual(run.report["planned"], len(frozen))
        self.assertEqual(run.report["actual_started"] + run.report["missed"], len(frozen))
        self.assertEqual(len(server.starts), 2, "the scheduling unit test uses no formal upstream requests")

    async def test_budget_stops_before_send_and_awaiting_stop_is_synchronous(self):
        run, server = await self.prepared()
        run.ledger.budget = Decimal(str(run.ledger.snapshot()["known_cost_usd"]))
        await run.run()
        self.assertEqual(len(server.starts), 2)
        self.assertEqual(run.stop_reason, "budget_stop")
        self.assertEqual(self.assert_missed_reasons_reconcile(run).get("budget_denied"), 1)
        run2, _ = await self.prepared()
        run2.stop()
        self.assertEqual((run2.phase, run2.status), ("finished", "stopped"))
        with self.assertRaises(ValueError):
            await run2.run()

    async def test_duplicate_start_and_append_only_evidence(self):
        server = Upstream()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            run = TpmRun(config(), transport=httpx.MockTransport(server), output_path=path)
            await run.calibrate()
            with self.assertRaises(ValueError):
                await run.calibrate()
            task = asyncio.create_task(run.run())
            await asyncio.sleep(.005)
            with self.assertRaises(ValueError):
                await run.run()
            await task
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), len(run.requests))
            self.assertTrue(all("messages" not in row and "output" not in row for row in rows))
            self.assertEqual(len(run.export()["requests"]), len(rows))
            self.assertNotIn("requests", run.snap())

    async def test_writer_failure_cannot_deadlock_close(self):
        server = Upstream()
        with tempfile.TemporaryDirectory() as directory:
            run = TpmRun(config(), transport=httpx.MockTransport(server),
                         output_path=Path(directory) / "requests.jsonl")
            original = run._write_records

            async def broken_writer():
                await asyncio.sleep(.001)
                raise OSError("synthetic disk full")

            run._write_records = broken_writer
            with self.assertRaises(OSError):
                await asyncio.wait_for(run.calibrate(), .5)
            run._write_records = original

    async def test_encoding_failure_retains_settled_evidence_and_stops_without_deadlock(self):
        server = Upstream()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            run = TpmRun(config(), transport=httpx.MockTransport(server), output_path=path)
            await run.calibrate()
            original = run.requests.append
            failed_ids = []

            def fail_once(row):
                if not failed_ids:
                    failed_ids.append(row["id"])
                    raise ValueError("synthetic record encoding failure")
                return original(row)

            with patch.object(run.requests, "append", side_effect=fail_once), \
                    patch.object(run.ledger, "settle", wraps=run.ledger.settle) as settle:
                await asyncio.wait_for(run.run(), 1)
            records = list(run.requests)
            formal = [row for row in records if row["phase"] == "formal"]
            self.assertEqual(run.stop_reason, "persistence_error")
            self.assertEqual(run.status, "error")
            self.assertTrue(run.report["evidence_incomplete"])
            self.assertEqual(run.report["verdict"], "insufficient_evidence")
            self.assertEqual(len(records), len(server.starts))
            self.assertEqual(settle.call_count, len(formal))
            self.assertEqual(sum(row["id"] == failed_ids[0] for row in records), 1)
            self.assertFalse(run._inflight)
            self.assertEqual(run.ledger.snapshot()["inflight_reservations"], 0)
            self.assertGreater(run.ledger.snapshot()["known_cost_usd"], 0)
            self.assertAlmostEqual(sum(row["known_cost_usd"] for row in records),
                                   run.ledger.snapshot()["known_cost_usd"])
            written = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(written), len(records) - 1)
            self.assertNotIn(failed_ids[0], [row["id"] for row in written])
            self.assertEqual(run.export()["requests"], records)

    async def test_cold_completed_evidence_matches_file_snapshots_and_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            run = TpmRun(config(), transport=httpx.MockTransport(Upstream()), output_path=path)
            await run.calibrate()
            await run._open()
            try:
                # Deterministic completion count exercises cold evidence without
                # depending on host timer precision in a unit test.
                for _ in range(150):
                    await run._request("formal", stage_index=1)
                    await asyncio.sleep(0)
            finally:
                await run._close()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 152)
            self.assertEqual(rows, list(run.requests))
            self.assertEqual(rows, run.export()["requests"])
            self.assertEqual(rows[-100:], run.snap()["recent_requests"])
            now = rows[-1]["ended_at"]
            expected = metrics(rows, now=now, formal_started_at=run.formal_started_at)
            observed = run._metrics(now)
            for key, value in expected.items():
                self.assertEqual(observed[key], value, key)
            exported = run.export()
            exported["requests"][0]["usage"]["raw"]["total_tokens"] = -1
            exported["requests"][-1]["timing"]["ended_at"] = "changed"
            self.assertEqual(rows, run.export()["requests"])

    async def test_encoding_failure_stops_next_ready_request_before_done_callback(self):
        run, server = await self.prepared()
        await run._open()
        run.phase, run._busy = "running", True
        before = len(server.starts)
        try:
            with patch.object(run.requests, "append", side_effect=ValueError("synthetic encoding failure")):
                first = asyncio.create_task(run._request("formal", 1))
                second = asyncio.create_task(run._request("formal", 1))
                results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), .5)
            self.assertIsInstance(results[0], ValueError)
            self.assertIsNone(results[1])
            self.assertEqual(len(server.starts), before + 1)
            self.assertEqual(len(run.requests), before + 1)
            self.assertEqual(run.stop_reason, "persistence_error")
            self.assertTrue(run._stop.is_set())
            self.assertFalse(run._inflight)
            self.assertEqual(run.ledger.snapshot()["inflight_reservations"], 0)
        finally:
            run._finish()
            await run._close()

    async def test_stop_while_calibration_closes_reaches_terminal_state(self):
        run = TpmRun(config(), transport=httpx.MockTransport(Upstream()))
        closing, release = asyncio.Event(), asyncio.Event()
        original = run._close

        async def blocked_close():
            closing.set()
            await release.wait()
            await original()

        run._close = blocked_close
        task = asyncio.create_task(run.calibrate())
        await closing.wait()
        self.assertEqual(run.phase, "awaiting_run")
        run.stop()
        release.set()
        await task
        self.assertEqual((run.phase, run.status), ("finished", "stopped"))

    async def test_p95_reservations_warn_when_budget_may_limit_pressure(self):
        run, _ = await self.prepared()
        run.calibration["p95_seconds"] = 60
        run.ledger.budget = Decimal(".1")
        run.refresh_plan_reserve_warning()
        self.assertEqual(run.plan["estimated_p95_inflight_requests"], 1500)
        self.assertTrue(run.plan["reserve_may_limit_pressure"])
        self.assertTrue(any("在途预留" in warning for warning in run.plan["warnings"]))


class TpmReportTests(unittest.TestCase):
    def row(self, ended, *, stage=4, tokens=50_000, success=True):
        return dict(phase="formal", stage_index=stage, started_at=ended - 1, ended_at=ended,
                    success=success, latency_seconds=1, usage=normalize_usage(usage(tokens - 500, 500)))

    def test_primary_window_excludes_low_stage_and_post_stop_drain(self):
        cfg = validate_config(config(target_tpm=200_000_000, input_tokens=49_500, expected_output_tokens=500, max_output_tokens=1024, context_window_tokens=65_536))
        rows = [self.row(100, tokens=190_000_000), self.row(100, stage=3, tokens=20_000_000),
                self.row(151, tokens=20_000_000)]
        report = build_report(cfg, rows, [dict(index=4, target_tpm=200_000_000, started_at=60, ended_at=150)], [],
                              dispatch_ended_at=150, last_ended_at=151, planned=3)
        self.assertEqual(report["primary_window"]["full_tokens"], 190_000_000)
        self.assertEqual(report["verdict"], "near")
        self.assertEqual(report["drain_seconds"], 1)

    def test_low_stage_is_never_a_target_window(self):
        cfg = validate_config(config(target_tpm=200_000_000, input_tokens=49_500, expected_output_tokens=500, max_output_tokens=1024, context_window_tokens=65_536))
        report = build_report(cfg, [self.row(70, stage=1)],
                              [dict(index=1, target_tpm=50_000_000, started_at=1, ended_at=80)], [],
                              dispatch_ended_at=80, last_ended_at=80)
        self.assertFalse(report["primary_window"]["valid"])
        self.assertEqual(report["verdict"], "insufficient_evidence")

    def test_mid_target_error_growth_cannot_hide_behind_good_final_window(self):
        cfg = validate_config(config(target_tpm=200_000_000, input_tokens=49_500,
                                      expected_output_tokens=500, max_output_tokens=1024, context_window_tokens=65_536))
        rows = [self.row(100, tokens=1_000_000) for _ in range(200)]
        rows += [self.row(170, success=False) for _ in range(4)]
        rows += [self.row(220, tokens=1_000_000) for _ in range(200)]
        report = build_report(cfg, rows, [dict(index=4, target_tpm=200_000_000, started_at=60, ended_at=240)], [],
                              dispatch_ended_at=240, last_ended_at=220, planned=len(rows))
        self.assertGreaterEqual(report['target_success_rate'], .99)
        self.assertEqual(report['primary_window']['success_rate'], 1)
        self.assertTrue(report['errors_worsened'])
        self.assertFalse(report['stable'])
        self.assertEqual(report['verdict'], 'not_reached')

    def test_growth_or_errors_prevent_stable_pass(self):
        cfg = validate_config(config(target_tpm=200_000_000, input_tokens=49_500, expected_output_tokens=500, max_output_tokens=1024, context_window_tokens=65_536))
        rows = [self.row(100 + i, tokens=20_000_000) for i in range(10)]
        series = [dict(at=61 + i, in_flight=i * 20) for i in range(10)]
        report = build_report(cfg, rows, [dict(index=4, target_tpm=200_000_000, started_at=60, ended_at=150)], series,
                              dispatch_ended_at=150, last_ended_at=150, planned=10)
        self.assertTrue(report["sustained_backlog"])
        self.assertEqual(report["verdict"], "not_reached")

    def test_failed_unknown_bill_does_not_erase_measured_success_throughput(self):
        cfg = validate_config(config(target_tpm=200_000_000, input_tokens=49_500,
                                     expected_output_tokens=500, max_output_tokens=1024,
                                     context_window_tokens=65_536))
        rows = [self.row(100, tokens=2_000_000) for _ in range(100)]
        failed = self.row(100, success=False)
        failed["usage"] = normalize_usage(None)
        rows.append(failed)
        report = build_report(cfg, rows,
                              [dict(index=4, target_tpm=200_000_000, started_at=60, ended_at=150)], [],
                              dispatch_ended_at=150, last_ended_at=150, planned=101)
        self.assertEqual(report["verdict"], "reached")
        self.assertLess(report["target_usage_complete_rate"], 1)
        self.assertEqual(report["successful_usage_complete_rate"], 1)
        rows[0]["usage"] = normalize_usage(None)
        missing_success = build_report(cfg, rows,
                                       [dict(index=4, target_tpm=200_000_000, started_at=60, ended_at=150)], [],
                                       dispatch_ended_at=150, last_ended_at=150, planned=101)
        self.assertEqual(missing_success["verdict"], "insufficient_evidence")
