"""有界、固定到达率的 TPM（每分钟 Token 数）流式发压执行器。"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import math
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx

from smart_route_load import _chat_url
from task_status import TERMINAL_STATES, apply_transition
from test_plan import sanitize
from time_record import request_timing, task_timing
from tpm_config import preview, validate_config
from tpm_corpus import Corpus
from tpm_metrics import LiveMetrics
from tpm_records import CompletedRecords, project_request_summary
from tpm_report import build_report, percentile
from tpm_snapshot import clone_snapshot
from tpm_usage import BudgetLedger, normalize_usage, worst_case_cost


class StreamFailure(Exception):
    """Contains only an internal category, never upstream response text."""


class ReleasingResponseStream(httpx.AsyncByteStream):
    """完成底层关闭后释放流引用，不等循环回收才释放长请求体。

    httpx（请求库）的响应流包装器反向引用响应，响应又持有完整请求体。
    仅此执行器拥有的流增加一层透传：成功关闭后断开内层引用；失败或
    取消时保留原流，不能把关闭标志当成底层清理成功。响应、用量和请求
    证据本身不修改，也不依赖请求库的私有字段。
    """

    def __init__(self, stream):
        self._inner = stream

    async def __aiter__(self):
        stream = self._inner
        if stream is None:
            raise httpx.StreamClosed()
        async for chunk in stream:
            yield chunk

    async def aclose(self):
        stream = self._inner
        if stream is not None:
            await stream.aclose()
            self._inner = None


async def bounded_lines(response):
    """Bound unterminated line memory without buffering TTFT until a fixed chunk size."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    async for chunk in response.aiter_bytes():
        if len(chunk) > 2 * 1024 * 1024:
            raise StreamFailure("stream_event_too_large")
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            if len(line) > 2 * 1024 * 1024:
                raise StreamFailure("stream_event_too_large")
            yield line.rstrip("\r")
        if len(pending) > 2 * 1024 * 1024:
            raise StreamFailure("stream_event_too_large")
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending.rstrip("\r")


async def consume_sse(response, record, started_mono, semantics):
    """Consume all SSE events, keeping the last usage snapshot (never summing it)."""
    event_lines = []
    event_size = 0
    saw_done = False
    saw_finish = False
    finish_reason = None
    output_chars = 0

    def event(data):
        nonlocal saw_done, saw_finish, finish_reason, output_chars
        if data == "[DONE]":
            saw_done = True
            return
        if saw_done:
            raise StreamFailure("data_after_done")
        try:
            block = json.loads(data)
        except (ValueError, TypeError):
            raise StreamFailure("invalid_stream_json") from None
        if not isinstance(block, dict):
            raise StreamFailure("invalid_stream_json")
        if isinstance(block.get("usage"), dict) and block["usage"]:
            record["usage"] = normalize_usage(block["usage"], semantics=semantics)
        if block.get("error"):
            raise StreamFailure("stream_error")
        choices = block.get("choices", [])
        if not isinstance(choices, list):
            raise StreamFailure("invalid_stream_choices")
        for choice in choices:
            if not isinstance(choice, dict):
                raise StreamFailure("invalid_stream_choices")
            if choice.get("finish_reason") is not None:
                saw_finish = True
                finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise StreamFailure("invalid_stream_delta")
            text = delta.get("content")
            if isinstance(text, str) and text:
                output_chars += len(text)
                if record["ttft_seconds"] is None:
                    record["ttft_seconds"] = time.monotonic() - started_mono
                    record["first_token_at"] = time.time()

    async for line in bounded_lines(response):
        if len(line) > 2 * 1024 * 1024:
            raise StreamFailure("stream_event_too_large")
        if not line:
            if event_lines:
                event("\n".join(event_lines))
                event_lines, event_size = [], 0
                if saw_done:
                    break
            continue
        if line.startswith("data:"):
            value = line[5:].lstrip(" ")
            event_lines.append(value)
            event_size += len(value)
            if event_size > 2 * 1024 * 1024:
                raise StreamFailure("stream_event_too_large")
    if event_lines:
        event("\n".join(event_lines))
    record.update(stream_done=saw_done, finish_reason=finish_reason,
                  output_limited=finish_reason == "length", output_chars=output_chars)
    if not saw_done or not saw_finish:
        raise StreamFailure("incomplete_stream")
    if finish_reason == "length":
        raise StreamFailure("output_limit")
    if finish_reason not in {"stop", "tool_calls", "function_call"}:
        raise StreamFailure("abnormal_finish")
    if not output_chars:
        raise StreamFailure("empty_output")


class TpmRun:
    """A run spans two explicit confirmations, retaining one ledger across both."""

    def __init__(self, config, *, task_id=None, output_path=None, on_change=None, transport=None):
        self.config = validate_config(config)
        self.id = task_id or uuid.uuid4().hex
        self.created = time.time()
        self._created_mono = time.monotonic()
        self._started_mono = self._dispatch_ended_mono = self._ended_mono = None
        self.stop_requested_at = self._timing_ended_at = None
        self._formal_origin = None
        self.started_at = self.finished_at = self.stopped_at = None
        self.status = "draft"
        self.phase = "awaiting_calibration"
        self.stop_reason = None
        self.dispatch_ended_at = None
        self.formal_started_at = None
        self.calibration = None
        self.plan = preview(self.config)
        self.plan_id = self._plan_hash()
        self.corpus = None
        self.corpus_revision = 0
        self.ledger = BudgetLedger(self.config["budget_usd"], self.config["prices"])
        self.requests = CompletedRecords()
        self._live_metrics = LiveMetrics()
        self.series = []
        self.stages = []
        self.report = None
        self.output_path = str(output_path) if output_path else None
        self.on_change = on_change
        self._transport = transport
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._loop = None
        self._wake = None
        self._client = None
        self._pool = None
        self._active = set()
        self._inflight = {}
        self._sequence = 0
        self._planned = 0
        self._missed = 0
        self._missed_reasons = Counter()
        self._missed_by_stage = {}
        self._writer = None
        self._sink = None
        self._last_sample = 0
        self._busy = False
        self._task_errors = []
        self._evidence_encoding_error = False
        self._max_reserve = self.plan["request_reserve_usd"]

    def _timing(self):
        return task_timing(created_at=self.created, started_at=self.started_at,
                           stop_requested_at=self.stop_requested_at, dispatch_ended_at=self.dispatch_ended_at,
                           ended_at=self._timing_ended_at,
                           queue_ms=(self._started_mono - self._created_mono) * 1000 if self._started_mono is not None else None,
                           elapsed_ms=((self._ended_mono if self._ended_mono is not None else time.monotonic()) - self._started_mono) * 1000 if self._started_mono is not None else None,
                           drain_ms=(self._ended_mono - self._dispatch_ended_mono) * 1000 if self._ended_mono is not None and self._dispatch_ended_mono is not None else None)

    def _end_dispatch(self):
        with self._lock:
            if self.dispatch_ended_at is None:
                self.dispatch_ended_at = time.time()
                self._dispatch_ended_mono = time.monotonic()

    def complete_timing(self):
        """Called only after local execution and its incremental writer have settled."""
        with self._lock:
            if self._ended_mono is None:
                self._timing_ended_at = time.time()
                self._ended_mono = time.monotonic()
            if self.report is not None:
                self.report["timing"] = self._timing()

    def _plan_hash(self):
        return hashlib.sha256(json.dumps(self.plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]

    def refresh_plan_reserve_warning(self):
        """Duration revisions must rerun this before hashing and user confirmation."""
        p95 = (self.calibration or {}).get("p95_seconds")
        if p95 is None:
            return
        estimated_inflight = math.ceil(self.plan["target_rps"] * p95)
        reserve = estimated_inflight * self.plan["request_reserve_usd"]
        fees = self.ledger.snapshot()
        headroom = (fees["remaining_usd"] - reserve - self.plan["formal_estimated_cost_usd"])
        self.plan.update(estimated_p95_inflight_requests=estimated_inflight,
                         estimated_p95_inflight_reserve_usd=reserve,
                         estimated_budget_headroom_usd=headroom,
                         reserve_may_limit_pressure=headroom < 0)
        prefix = "按校准第 95 百分位估计的在途预留"
        self.plan["warnings"] = [warning for warning in self.plan["warnings"] if not warning.startswith(prefix)]
        if headroom < 0:
            self.plan["warnings"].append(
                f"{prefix}约为 {reserve:.4f} 美元（约 {estimated_inflight} 个请求）；加上预计正式费用后超过当前预算余额。"
                "可能因保守在途预留提前停发，不能将其误判为上游容量不足。该在途估计不是容量或费用保证，预算不会自动增加。")

    def _changed(self):
        if self.on_change:
            self.on_change(self)

    def stop(self, reason="manual_stop"):
        with self._lock:
            if self.status in TERMINAL_STATES:
                return False
            self.stop_reason = self.stop_reason or reason
            self.stop_requested_at = self.stop_requested_at or time.time()
            self._end_dispatch()
            self._stop.set()
            if self.phase in {"awaiting_calibration", "awaiting_run"} and not self._busy:
                self.phase = "finished"
                apply_transition(self, "stopped")
                self._build_report()
                self.complete_timing()
            else:
                self.phase = "draining"
                apply_transition(self, "stopping")
            if self._loop and self._wake:
                self._loop.call_soon_threadsafe(self._wake.set)
            self._changed()
            return True

    def _claim(self, expected):
        with self._lock:
            if self._busy or self.phase != expected or self._stop.is_set():
                raise ValueError("任务阶段不匹配或已执行，禁止重复启动收费请求。")
            self._busy = True

    async def _open(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._pool = asyncio.BoundedSemaphore(self.config["pool_connections"])
        limits = httpx.Limits(max_connections=self.config["pool_connections"],
                             max_keepalive_connections=self.config["pool_connections"])
        self._client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(self.config["timeout_seconds"]),
                                         transport=self._transport, follow_redirects=False, trust_env=False)
        if self.output_path:
            Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
            self._sink = open(self.output_path, "a", encoding="utf-8", buffering=64 * 1024)
            self._writer = asyncio.Queue(maxsize=max(128, self.config["concurrency"] * 2))
            self._writer_task = asyncio.create_task(self._write_records())

    async def _write_records(self):
        """Disk work is batched off the scheduling loop and bounded by queue capacity."""
        while True:
            row = await self._writer.get()
            if row is None:
                self._writer.task_done()
                break
            batch = [row]
            while len(batch) < 128 and not self._writer.empty():
                candidate = self._writer.get_nowait()
                if candidate is None:
                    raise RuntimeError("unexpected writer sentinel")
                batch.append(candidate)
            # Completed evidence is encoded once before publication; the cold
            # archive and incremental file share exactly these bytes of text.
            payload = "".join(value + "\n" for value in batch)
            await asyncio.to_thread(self._flush_records, payload)
            for _ in batch:
                self._writer.task_done()

    def _flush_records(self, payload):
        """Keep each batch's write/flush ordered in one worker invocation."""
        self._sink.write(payload)
        self._sink.flush()

    async def _close(self):
        try:
            if self._client:
                await self._client.aclose()
        finally:
            try:
                await self._close_writer()
            finally:
                self._busy = False
                if self.status in TERMINAL_STATES:
                    self._end_dispatch()
                    self.complete_timing()
                self._changed()

    async def _close_writer(self):
        if self._writer:
            try:
                joining = asyncio.create_task(self._writer.join())
                done, _ = await asyncio.wait((joining, self._writer_task), return_when=asyncio.FIRST_COMPLETED)
                if self._writer_task in done:
                    joining.cancel()
                    await asyncio.gather(joining, return_exceptions=True)
                    self._writer_task.result()
                else:
                    await self._writer.put(None)
                    await self._writer_task
            finally:
                await asyncio.to_thread(self._sink.close)
                self._writer = self._sink = None

    async def _sleep(self, seconds):
        if seconds <= 0 or self._stop.is_set():
            return
        try:
            # Avoid a child task and cancellation handshake at every arrival slot.
            # The same wake event still interrupts the wait immediately on stop.
            async with asyncio.timeout(seconds):
                await self._wake.wait()
        except asyncio.TimeoutError:
            pass

    async def _wait_for_slot(self, due, stage_end, *, final_slot=False):
        # A final slot can have less than 2 ms before its immutable stage end.
        # Wake once before that narrow window, then still wait until the exact
        # due time. This adds at most one timer per stage, never an early send.
        remaining = due - time.monotonic()
        if final_slot and 0 < stage_end - due < .002 and remaining > .002:
            await self._sleep(remaining - .002)
        while not self._stop.is_set():
            await self._sleep(due - time.monotonic())
            if time.monotonic() >= due:
                return

    def _reject_admission(self, phase, stage_index, deadline):
        reason = ("admission_stopped" if self._stop.is_set() else
                  "stage_elapsed" if deadline is not None and time.monotonic() >= deadline else None)
        if reason is None:
            return False
        if phase == "formal":
            self._record_missed(1, reason, stage_index)
        return True

    async def _request(self, phase, stage_index=None, due=None, group=0, *, deadline=None):
        enqueued_at = time.time()
        pool_begin = time.monotonic()
        async with self._pool:
            pool_wait = time.monotonic() - pool_begin
            # Admission is the sole reservation and actual-start boundary. There is no request queue.
            if self._reject_admission(phase, stage_index, deadline):
                return None
            self._sequence += 1
            request_id = f"{self.id}-{self._sequence}"
            encoded = self.corpus.request_bytes(request_id, group=group)
            if self._reject_admission(phase, stage_index, deadline):
                return None
            # Declared context capacity is not a billing guarantee. UTF-8 payload bytes
            # are a conservative bound for byte/subword tokenizers, including protocol overhead.
            byte_bound = worst_case_cost(len(encoded) + self.config["protocol_overhead_tokens"],
                                         self.config["max_output_tokens"], self.config["prices"])
            reserve = max(self.plan["request_reserve_usd"], byte_bound)
            self._max_reserve = max(self._max_reserve, reserve)
            if not self.ledger.reserve(request_id, reserve):
                self.stop("budget_stop")
                if phase == "formal":
                    self._record_missed(1, "budget_denied", stage_index)
                return None
            record = {"id": request_id, "phase": phase, "stage_index": stage_index,
                      "group": group, "corpus_revision": self.corpus_revision,
                      "scheduled_at": self.formal_started_at + due - self._formal_origin if due is not None and self._formal_origin is not None else None,
                      "enqueued_at": enqueued_at, "first_token_at": None,
                      "started_at": None, "ended_at": None, "success": False,
                      "http_status": None, "error": None, "ttft_seconds": None,
                      "latency_seconds": None,
                      "pool_wait_seconds": pool_wait, "body_bytes": len(encoded),
                      "output_limited": False, "stream_done": False, "finish_reason": None,
                      "usage": normalize_usage(None, semantics=self.config["usage_semantics"])}
            # Reservation or local request preparation can cross the stage end
            # after the scheduler admitted its task. Nothing has been sent yet;
            # release only this unsent reservation, without inventing a failure
            # response or an unknown upstream bill. No await separates this
            # final check from entering the client stream.
            with self._lock:
                started_mono = time.monotonic()
                started = time.time()
                signed_delay = started_mono - due if due is not None else None
                record.update(started_at=started,
                              schedule_delay_seconds=max(0, signed_delay) if signed_delay is not None else 0,
                              signed_schedule_delay_seconds=signed_delay,
                              stage_deadline_margin_seconds=deadline - started_mono if deadline is not None else None)
                record["timing"] = request_timing(
                    scheduled_at=record["scheduled_at"], enqueued_at=enqueued_at, started_at=started,
                    queue_ms=(started_mono - pool_begin) * 1000,
                    schedule_lag_ms=record["schedule_delay_seconds"] * 1000 if due is not None else None)
                if self._reject_admission(phase, stage_index, deadline):
                    self.ledger.release_unsent(request_id)
                    return None
                self._inflight[request_id] = record
            try:
                # One monotonic deadline from actual admission includes local
                # timing preparation, connect, write, heartbeats and full read.
                async with asyncio.timeout_at(started_mono + self.config["timeout_seconds"]):
                    async with self._client.stream("POST", _chat_url(self.config["url"]),
                                                   content=encoded,
                                                   headers={"Authorization": "Bearer " + self.config["api_key"],
                                                            "Content-Type": "application/json"}) as response:
                        response.stream = ReleasingResponseStream(response.stream)
                        record["http_status"] = response.status_code
                        if response.status_code != 200:
                            category = "http_429" if response.status_code == 429 else "http_5xx" if response.status_code >= 500 else "http_error"
                            raise StreamFailure(category)
                        await consume_sse(response, record, started_mono, self.config["usage_semantics"])
                # Closing the stream and leaving the original total deadline
                # must both succeed before this is a successful completion.
                record["success"] = True
            except (asyncio.TimeoutError, httpx.TimeoutException):
                record["error"] = "timeout"
            except StreamFailure as exc:
                record["error"] = str(exc)
            except httpx.HTTPError:
                record["error"] = "network_error"
            except asyncio.CancelledError:
                record["error"] = "cancelled_unknown_bill"
                self.stop_reason = self.stop_reason or "interrupted"
                self._stop.set()
                raise
            except Exception:
                record["error"] = "client_error"
            finally:
                record["ended_at"] = time.time()
                record["latency_seconds"] = time.monotonic() - started_mono
                record["timing"] = request_timing(**{**record["timing"],
                    "first_token_at": record["first_token_at"], "ended_at": record["ended_at"],
                    "ttft_ms": record["ttft_seconds"] * 1000 if record["ttft_seconds"] is not None else None,
                    "total_ms": record["latency_seconds"] * 1000, "source": "recorded"})
                if not record["stream_done"] and record["usage"].get("billing_complete"):
                    # Earlier cumulative snapshots cannot settle an interrupted final bill.
                    record["usage"]["billing_complete"] = False
                    record["usage"]["issues"].append("流式响应未完整结束，累计用量不是可靠最终账单用量")
                before_cost = self.ledger.snapshot()["known_cost_usd"]
                record["settlement"] = self.ledger.settle(request_id, record["usage"])
                record["known_cost_usd"] = record["settlement"]["known_cost_usd"] - before_cost
                if record["settlement"].get("budget_exceeded"):
                    self.stop("budget_exceeded")
                if phase == "formal" and (record["usage"].get("output_tokens") or 0) > self.config["max_output_tokens"]:
                    self.stop("output_limit_ignored")
                # 每条失败请求都保留可核查的结构化上下文；error 仍保留稳定的
                # 字符串分类，供现有统计与界面兼容。不要把上游响应正文写入
                # 记录，避免把提示词、模型输出或凭据带入错误日志。
                if record.get("error"):
                    record["error_detail"] = {
                        "code": record["error"],
                        "http_status": record.get("http_status"),
                        "finish_reason": record.get("finish_reason"),
                        "stream_done": bool(record.get("stream_done")),
                        "usage_completeness": record.get("usage", {}).get("completeness"),
                        "ended_at": record["ended_at"],
                    }
                with self._lock:
                    # Publish a completed row and remove its active entry atomically.
                    # Published rows are immutable; live statistics consume each once.
                    try:
                        encoded_record = self.requests.append(record)
                    except Exception:
                        # Settlement already happened. Preserve this completed
                        # evidence in memory even if it cannot be encoded, and
                        # stop admissions before another ready task can send.
                        # Do not leave it in flight or claim it reached disk.
                        self._evidence_encoding_error = True
                        self.requests.retain_unencoded(record)
                        self._inflight.pop(request_id, None)
                        # Stop synchronously: another ready request task may
                        # execute before this task's done callback.
                        self.stop("persistence_error")
                        raise
                    self._inflight.pop(request_id, None)
                if self._writer:
                    if self._writer_task.done():
                        self._writer_task.result()
                    # Full disk queue is a local bottleneck; stop new admissions, then flush.
                    if self._writer.full():
                        self.stop("persistence_backpressure")
                        pending_put = asyncio.create_task(self._writer.put(encoded_record))
                        done, _ = await asyncio.wait((pending_put, self._writer_task), return_when=asyncio.FIRST_COMPLETED)
                        if self._writer_task in done:
                            pending_put.cancel()
                            await asyncio.gather(pending_put, return_exceptions=True)
                            self._writer_task.result()
                    else:
                        self._writer.put_nowait(encoded_record)
                self._sample()
            return record

    def _record_missed(self, count, reason, stage_index=None):
        """Record scheduler dispositions, without guessing which subsystem delayed it."""
        if count <= 0:
            return
        with self._lock:
            self._missed += count
            self._missed_reasons[reason] += count
            stage = self._missed_by_stage.setdefault(str(stage_index) if stage_index is not None else "unassigned", Counter())
            stage[reason] += count

    def _reconcile_missed(self):
        # Unvisited stages / cancelled tasks have no finer admission evidence.
        # Call after all admitted requests settle, including exceptional exits.
        completed = sum(row["phase"] == "formal" for row in self.requests)
        self._record_missed(self._planned - completed - self._missed, "not_admitted")

    def _sample(self, force=False):
        now = time.time()
        if not force and now - self._last_sample < 1:
            return
        self._last_sample = now
        value = self._metrics(now)
        self.series.append({"at": now, "phase": self.phase, **value, **self.ledger.snapshot()})
        self._changed()

    def _metrics(self, now):
        with self._lock:
            active = list(self._inflight.values())
            self._live_metrics.update(self.requests)
            result = self._live_metrics.snapshot(now=now, formal_started_at=self.formal_started_at,
                                                 planned=self._planned, missed=self._missed,
                                                 in_flight=sum(row["phase"] == "formal" for row in active))
        elapsed = max(.001, min(10, now - self.formal_started_at)) if self.formal_started_at else 10
        result["actual_start_rps"] += sum(row["phase"] == "formal" and now - 10 < row["started_at"] <= now for row in active) / elapsed
        result["all_in_flight"] = len(active)
        return result

    def _task_done(self, task):
        self._active.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self._task_errors.append(type(error).__name__)
            self.stop("persistence_error")

    async def calibrate(self):
        self._claim("awaiting_calibration")
        self.phase = "calibrating"
        self._started_mono = time.monotonic()
        apply_transition(self, "running")
        self._changed()
        try:
            await self._open()
            self.corpus = await asyncio.to_thread(Corpus, self.config)
            # Usage-backed resizing is charged and retained; its changing size is excluded from final means.
            probe_count = 0
            for _ in range(min(4, self.config["calibration_requests"] - 1)):
                row = await self._request("calibration")
                probe_count += 1
                if not row or not row["success"] or not row["usage"].get("complete"):
                    break
                observed = row["usage"]["full_input_tokens"]
                changed = await asyncio.to_thread(self.corpus.resize_from_usage, observed)
                if not changed:
                    break
                self.corpus_revision += 1
            if not self._stop.is_set() and self.config["cache_mode"] == "prefer_cache":
                for group in range(self.config["template_groups"]):
                    for _ in range(self.config["warmup_requests"]):
                        row = await self._request("warmup", group=group)
                        if not row or not row["success"]:
                            break
                    if self._stop.is_set():
                        break
            for index in range(probe_count, self.config["calibration_requests"]):
                if self._stop.is_set():
                    break
                await self._request("calibration", group=index % self.config["template_groups"])
            selected = [row for row in self.requests if row["phase"] == "calibration" and row["corpus_revision"] == self.corpus_revision]
            reliable = [row for row in selected if row["success"] and row["usage"].get("complete")]
            issues = []
            if len(reliable) != len(selected) or len(reliable) < 2:
                issues.append("校准存在失败或缺失完整用量，不能确认正式计量方案。")
            avg_input = sum(row["usage"]["full_input_tokens"] for row in reliable) / len(reliable) if reliable else None
            avg_output = sum(row["usage"]["output_tokens"] for row in reliable) / len(reliable) if reliable else None
            if avg_input is not None and abs(avg_input - self.config["input_tokens"]) > self.config["input_tokens"] * .01:
                issues.append("实际输入偏离目标超过 1%，需要重新校准。")
            if not self.corpus.metadata()["input_verified"]:
                issues.append("最终长语料尚未经实测验证，需要增加校准样本或重新校准。")
            warmups = [row for row in self.requests if row["phase"] == "warmup" and row["corpus_revision"] == self.corpus_revision]
            if self.config["cache_mode"] == "prefer_cache" and self.config["warmup_requests"]:
                groups_ready = all(sum(row["group"] == group for row in warmups) == self.config["warmup_requests"]
                                   for group in range(self.config["template_groups"]))
                warmups_reliable = all(
                    row["success"] and row["usage"].get("complete") and row["usage"].get("billing_complete")
                    and row["usage"]["output_tokens"] <= self.config["max_output_tokens"]
                    and row["usage"]["full_input_tokens"] + self.config["max_output_tokens"]
                    + self.config["protocol_overhead_tokens"] <= self.config["context_window_tokens"]
                    for row in warmups)
                if not groups_ready or not warmups_reliable:
                    issues.append("最终语料至少一个模板组未完成全部可靠预热，或预热用量、输出硬上限、上下文核验失败；禁止正式使用未完成预热的模板组。")
            if avg_input is not None and avg_input + self.config["max_output_tokens"] + self.config["protocol_overhead_tokens"] > self.config["context_window_tokens"]:
                issues.append("实测输入与输出硬上限及协议开销超过声明的模型上下文。")
            if any(not row["usage"].get("billing_complete") for row in selected):
                issues.append("缓存或费用用量口径不完整，请确认协议语义后新建校准。")
            if any((row["usage"].get("output_tokens") or 0) > self.config["max_output_tokens"] for row in selected):
                issues.append("接口返回的输出用量超过硬上限，费用兜底未被接口遵守。")
            cache_rows = [row for row in reliable if row["usage"].get("cache_read_tokens") is not None]
            cache_input = sum(row["usage"]["full_input_tokens"] for row in cache_rows)
            def mean_category(key):
                values = [row["usage"].get(key) for row in reliable]
                return sum(values) / len(values) if values and all(value is not None for value in values) else None
            self.calibration = {"passed": not issues and not self._stop.is_set(), "issues": issues,
                                "requests": len(selected), "charged_requests": len(self.requests),
                                "mean_input_tokens": avg_input, "mean_output_tokens": avg_output,
                                "mean_normal_input_tokens": mean_category("input_tokens"),
                                "mean_cache_read_tokens": mean_category("cache_read_tokens"),
                                "mean_cache_write_tokens": mean_category("cache_write_tokens"),
                                "p95_seconds": percentile([row["latency_seconds"] for row in reliable], .95),
                                "usage_complete_rate": len(reliable) / len(selected) if selected else 0,
                                "cache_hit_rate": sum(row["usage"]["cache_read_tokens"] for row in cache_rows) / cache_input if cache_input else None,
                                "max_body_bytes": max((row["body_bytes"] for row in selected), default=0),
                                "request_body_bytes": max((row["body_bytes"] for row in selected), default=0),
                                "corpus": self.corpus.metadata()}
            if reliable:
                self.plan = preview(self.config, self.calibration)
                # The calibration client is closed below. Formal execution opens a new
                # pool using these frozen, calibrated capacities after user consent.
                self.config.update(concurrency=self.plan["concurrency"],
                                   pool_connections=self.plan["pool_connections"])
                largest_body = max(len(self.corpus.request_bytes(self.id + "-999999999", group=group))
                                   for group in range(self.config["template_groups"]))
                frozen_byte_reserve = worst_case_cost(largest_body + self.config["protocol_overhead_tokens"],
                                                       self.config["max_output_tokens"], self.config["prices"])
                self.plan["request_reserve_usd"] = max(self.plan["request_reserve_usd"], self._max_reserve, frozen_byte_reserve)
                self._max_reserve = self.plan["request_reserve_usd"]
                self.plan["worst_case_cost_usd"] = self._max_reserve * (
                    self.plan["planned_requests"] + self.config["calibration_requests"] +
                    (self.config["template_groups"] * self.config["warmup_requests"] if self.config["cache_mode"] == "prefer_cache" else 0))
                self.plan["reserve_basis"] = "真实请求体 UTF-8（统一字符编码）字节上界与协议开销；不能把声明上下文容量当作上游计费保证。"
                self.refresh_plan_reserve_warning()
                self.plan_id = self._plan_hash()
            if self._stop.is_set():
                self._finish()
            elif issues:
                self.stop_reason = "calibration_failed"
                self.phase = "finished"
                apply_transition(self, "error")
                self._build_report()
            else:
                self.phase = "awaiting_run"
        except asyncio.CancelledError:
            self.stop_reason = self.stop_reason or "interrupted"
            self.phase = "finished"
            apply_transition(self, "error")
            raise
        except Exception:
            self.stop_reason = "calibration_error"
            self.phase = "finished"
            apply_transition(self, "error")
            raise
        finally:
            await self._close()
            if self._stop.is_set() and self.status not in TERMINAL_STATES:
                self._finish()
        return self.snap()

    async def run(self):
        self._claim("awaiting_run")
        if not self.calibration or not self.calibration["passed"]:
            self._busy = False
            raise ValueError("必须先完成有效校准并确认正式计划。")
        self.phase = "running"
        self._planned = sum(stage["planned_requests"] for stage in self.plan["stages"])
        self._changed()
        try:
            await self._open()
            self.formal_started_at = time.time()
            origin = time.monotonic()
            self._formal_origin = origin
            self._sample(force=True)
            offset = 0
            for definition in self.plan["stages"]:
                if self._stop.is_set():
                    break
                stage = dict(definition, started_at=self.formal_started_at + offset)
                self.stages.append(stage)
                begin = origin + offset
                end = begin + stage["duration_seconds"]
                interval = 1 / stage["rps"]
                slot = 0
                count = stage["planned_requests"]
                while slot < count and not self._stop.is_set():
                    due = begin + slot * interval
                    if due >= end:
                        break
                    await self._wait_for_slot(due, end, final_slot=slot == count - 1)
                    now = time.monotonic()
                    if self._stop.is_set() or now >= end:
                        break
                    # Discard elapsed slots; never burst to catch up after event-loop delay.
                    skipped = min(count - slot, int(max(0, now - due) / interval))
                    if skipped:
                        slot += skipped
                        self._record_missed(skipped, "scheduler_late", stage["index"])
                        due = begin + slot * interval
                    if slot >= count or due >= end:
                        break
                    if len(self._active) >= min(self.config["concurrency"], self.config["pool_connections"]):
                        self._record_missed(1, "concurrency_limit", stage["index"])
                    else:
                        task = asyncio.create_task(self._request("formal", stage["index"], due,
                                                                slot % self.config["template_groups"], deadline=end))
                        self._active.add(task)
                        task.add_done_callback(self._task_done)
                    slot += 1
                    self._sample()
                    self._cache_protection()
                self._record_missed(max(0, count - slot),
                                    "dispatch_stopped" if self._stop.is_set() else "stage_elapsed", stage["index"])
                if not self._stop.is_set():
                    await self._sleep(end - time.monotonic())
                stage["ended_at"] = min(self.formal_started_at + offset + stage["duration_seconds"], time.time())
                offset += stage["duration_seconds"]
            self._end_dispatch()
            self.phase = "draining"
            # Pending coroutine starts observe the stop flag, while already admitted streams drain.
            self._stop.set()
            if self.status == "running":
                apply_transition(self, "stopping")
            self._sample(force=True)
            if self._active:
                results = await asyncio.gather(*list(self._active), return_exceptions=True)
                if any(isinstance(value, BaseException) for value in results):
                    self.stop_reason = self.stop_reason or "client_error"
            if self._task_errors:
                self.stop_reason = self.stop_reason or "client_error"
            self._reconcile_missed()
            self._finish()
        except asyncio.CancelledError:
            self.stop_reason = self.stop_reason or "interrupted"
            self._end_dispatch()
            self._stop.set()
            for task in list(self._active):
                task.cancel()
            await asyncio.gather(*list(self._active), return_exceptions=True)
            self._reconcile_missed()
            self.phase = "finished"
            apply_transition(self, "error")
            self._build_report()
            raise
        except Exception:
            self.stop_reason = "client_error"
            self._end_dispatch()
            self._stop.set()
            self.phase = "draining"
            await asyncio.gather(*list(self._active), return_exceptions=True)
            self._reconcile_missed()
            self.phase = "finished"
            apply_transition(self, "error")
            self._build_report()
            raise
        finally:
            await self._close()
        return self.snap()

    def _cache_protection(self):
        if self.config["cache_mode"] != "prefer_cache":
            return
        rows = [row for row in self.requests[-100:] if row["phase"] == "formal" and row["usage"].get("cache_read_tokens") is not None]
        if len(rows) < 20:
            return
        inputs = sum(row["usage"].get("full_input_tokens") or 0 for row in rows)
        if inputs and sum(row["usage"]["cache_read_tokens"] for row in rows) / inputs < self.config.get("cache_hit_stop_threshold", .5):
            self.stop("cache_hit_low")

    def _finish(self):
        self.phase = "finished"
        # Decode the complete evidence only after admissions and streams stop.
        # Reuse this one materialization throughout the terminal report.
        records = list(self.requests)
        if self.stop_reason:
            target = "stopped" if self.stop_reason in {"manual_stop", "budget_stop", "cache_hit_low"} else "error"
        elif any(not row["success"] for row in records) or self._missed:
            target = "finished_with_errors"
        else:
            target = "finished"
        apply_transition(self, target)
        self._sample(force=True)
        self._build_report(records)
        if not self._busy:
            self._end_dispatch()
            self.complete_timing()

    def _build_report(self, records=None):
        if records is None:
            records = list(self.requests)
        self.report = build_report(self.config, records, self.stages, self.series,
                                   dispatch_ended_at=self.dispatch_ended_at,
                                   last_ended_at=max((row["ended_at"] for row in records), default=None),
                                   stop_reason=self.stop_reason, missed=self._missed, planned=self._planned,
                                   require_clock_evidence=True)
        self.report["timing"] = self._timing()
        self.report["fees"] = self.ledger.snapshot()
        self.report["unknown_bill_risk"] = self.report["fees"].get("reserved_cost_usd", 0) > 0
        if self._evidence_encoding_error:
            # A late persistence failure can occur after a complete target
            # window exists. Keep the arithmetic for audit, but never present
            # unpersisted evidence as a verified throughput result.
            self.report.update(verdict="insufficient_evidence", label="证据不足/发压端受限",
                               summary="证据不足/发压端受限", stable=False, evidence_incomplete=True)
            self.report["primary_window"].update(valid=False, measurement_status="unverified")
            self.report["limitations"].append("完成请求编码失败，部分证据仅保留在内存、未完整落盘；停止发压，不能据此确认达到目标。")

    def snap(self, include_requests=False, *, summary=False, display=False):
        now = self.finished_at if self.status in TERMINAL_STATES and self.finished_at else time.time()
        result = {"id": self.id, "name": self.config["name"], "model": self.config["model"], "created": self.created, "started_at": self.started_at,
                  "finished_at": self.finished_at, "stopped_at": self.stopped_at, "timing": self._timing(),
                  "phase": self.phase, "status": self.status, "stop_reason": self.stop_reason,
                  "metrics": self._metrics(now), "fees": self.ledger.snapshot()}
        result["fees"]["max_request_reserve_usd"] = self._max_reserve
        if summary:
            return result
        with self._lock:
            scheduling = {"missed_by_reason": dict(self._missed_reasons),
                          "missed_by_stage": {key: dict(value) for key, value in self._missed_by_stage.items()}}
        result.update({"config": sanitize(self.config), "calibration": self.calibration,
                  "plan": self.plan, "plan_id": self.plan_id,
                  "series": self.series, "stages": self.stages,
                  "report": self.report, "output_path": self.output_path,
                  "recent_requests": self.requests[-100:],
                  "formal_started_at": self.formal_started_at, "dispatch_ended_at": self.dispatch_ended_at,
                  "corpus": self.corpus.metadata() if self.corpus else None,
                  "client_retries": 0, "upstream_retries": "unknown", "scheduling": scheduling})
        if include_requests:
            result["requests"] = list(self.requests)
        elif display:
            # Only the explicit live view omits duplicated, undisplayed request
            # evidence. Full snapshots, history and exports retain every field.
            result["recent_requests"] = [project_request_summary(row) for row in result["recent_requests"]]
            result["recent_requests_view"] = "display-v1"
        return clone_snapshot(result)

    def export(self):
        return self.snap(include_requests=True)
