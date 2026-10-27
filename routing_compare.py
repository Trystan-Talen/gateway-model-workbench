#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能路由 vs 固定路由的对照执行器。

两侧跑同一份请求清单、同样的并发与节奏，唯一变量是 URL/密钥背后的路由策略。为了让
「差异来自路由」这句话站得住，这里有三条硬性设计：

1. **交错执行，不是先跑完 A 再跑 B。** 一整轮里的第 k 条请求，两侧在同一时刻竞争
   发出，并且奇偶交替谁先发。先跑完一侧再跑另一侧的话，上游负载变化、连接预热、
   限流窗口漂移都会被算到路由头上，而这些都是与路由无关的时间效应。
2. **配对靠 item_id，不靠顺序。** 同一份清单的同一个 item_id 在两侧各有一条记录，
   报告按它对账；某一侧丢结果时整对剔除，不当成失败。
3. **流式请求一律带 usage。** 缓存命中率的口径是 ``usage.cached_tokens``，不请求
   usage 就永远算不出来。网关不认 ``stream_options.include_usage`` 时自动降级重试，
   并把这个事实记在报告里——不能把兼容性缺口读成缓存退化。
4. **缓存按「运行 × 侧」隔离。** 两侧发的是同一份清单，而两条路由背后常常是同一批
   上游渠道；不隔离的话，先到的一侧会替后到的一侧把前缀缓存预热好，上一次运行也会
   替这一次预热（清单是确定性的）。因此每条请求最前面都加一行本侧专属的隔离标记：
   同一侧、同一段内前缀仍逐字节相同（缓存该命中照样命中），跨侧、跨运行从第一个
   token 起就不同（谁也借不到谁的缓存）。段首请求本应几乎都未命中，报告据此检测
   「实验之外的预热」。

这个模块只负责执行与汇总，不发通知、不写全局历史；执行结果落盘逻辑与压测任务保持
一致：JSONL（逐行 JSON）逐条追加，权限 0600。
"""
import asyncio
import copy
from app_paths import DATA_DIR
import json
import math
import os
import re
import secrets
import threading
import time
import uuid

import httpx

import routing_ab
import routing_corpus
from run_timing import RunClock, restored_timing, request_evidence
from time_record import normalize_timing_tree
from smart_route_load import CHAT_PATH, USAGE_RETRY_POLICY, attempt_timing, do_request, should_retry_without_usage


def _now():
    return time.time()


def _results_dir():
    return os.path.join(DATA_DIR, "results")


def result_paths(run_id):
    """两侧结果文件的路径由 run.id 唯一决定。

    之所以总是重算而不是只存一份：删除记录和重启恢复都发生在内存之外，
    重算能保证「记录没了但文件还在」这种够不到的文件不会出现。
    """
    base = _results_dir()
    return {side: os.path.join(base, f"routing-{run_id}-{side}.jsonl")
            for side in ("baseline", "candidate")}


def _duration(steps):
    return sum(step["duration"] for step in steps)


_REQUEST_SIDES = ("baseline", "candidate")
_FAILURE_REASONS = {
    "CANCELLED": "客户端已取消请求",
    "TIMEOUT": "请求超时", "NETWORK": "网络连接失败", "PROTOCOL": "响应不完整或协议异常",
    "HTTP_429": "请求被限流", "HTTP_5XX": "上游服务错误", "HTTP_4XX": "请求被拒绝",
    "HTTP_OTHER": "非成功状态码", "UNKNOWN": "失败原因不可得",
}


def _safe_number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    if not finite or value < 0 or (integer and int(value) != value):
        return None
    return int(value) if integer else value


def public_request_record(record):
    """逐条接口的字段和值白名单；正文和异常原文不能进入响应或缓存。"""
    status = record.get("status_code")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        status = None
    completed = record.get("completed") is True
    outcome = {"status_code": status, "completed": completed}
    if isinstance(record.get("ok"), bool):
        outcome["ok"] = record["ok"]
    elif isinstance(record.get("class") or record.get("cls"), str):
        # 仅用类别判断成功，不回显可能混入异常原文的任意类别字符串。
        outcome["ok"] = (record.get("class") or record.get("cls")) == "ok"
    ok = routing_ab._record_ok(outcome)
    error_type = None
    if not ok:
        candidate = record.get("error_type")
        if isinstance(candidate, str) and candidate in _FAILURE_REASONS:
            error_type = candidate
        elif status is not None and 200 <= status < 300:
            error_type = "PROTOCOL"
        elif status == 408:
            error_type = "TIMEOUT"
        elif status == 429:
            error_type = "HTTP_429"
        elif status is not None and status >= 500:
            error_type = "HTTP_5XX"
        elif status is not None and status >= 400:
            error_type = "HTTP_4XX"
        elif status is not None:
            error_type = "HTTP_OTHER"
        else:
            error_type = "UNKNOWN"
    index = routing_ab.request_index(record)
    item_id = record.get("item_id")
    item_id = item_id if isinstance(item_id, str) and re.fullmatch(r"routing-ab-v\d+-\d{6,}", item_id) else None
    result = {
        "index": index, "request_index": index, "sequence": index + 1 if index is not None else None,
        "item_id": item_id, "status_code": status, "completed": completed, "ok": ok,
        "error_type": error_type, "reason": "完整成功响应" if ok else _FAILURE_REASONS[error_type],
        "ttft_ms": _safe_number(record.get("ttft_ms")),
        "total_ms": _safe_number(record.get("total_ms")),
        "timing": request_evidence(record),
        "usage_seen": record.get("usage_seen") is True,
        "usage_source": record.get("usage_source") if record.get("usage_source") in ("stream", "body") else None,
        "usage_level": record.get("usage_level") if record.get("usage_level") in ("NONE", "HIT_ONLY", "FULL") else None,
        "cache_hit": record.get("cache_hit") if isinstance(record.get("cache_hit"), bool) else None,
        "burst_first": record.get("burst_first") if isinstance(record.get("burst_first"), bool) else None,
    }
    attempts = record.get("attempts")
    if isinstance(attempts, list):
        result["attempts"] = [{"attempt_index": _safe_number(a.get("attempt_index"), integer=True),
            "status_code": _safe_number(a.get("status_code"), integer=True),
            "error_type": a.get("error_type") if a.get("error_type") in _FAILURE_REASONS else None,
            "timing": request_evidence(a)} for a in attempts if isinstance(a, dict)]
    for key in ("prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens"):
        result[key] = _safe_number(record.get(key), integer=True)
    return result


def _file_signature(path):
    info = os.stat(path)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def validate_tier(tier):
    """档位校验放在 API 层：只有客户能点在界面上的档位才允许开跑。

    执行器本身只接受正整数——测试要用小档位跑完同一条代码路径，
    但 1000/3000/5000 这个白名单不能因此被放宽，所以分成两道。
    """
    if isinstance(tier, bool) or not isinstance(tier, int):
        raise ValueError("请求数档位必须是整数")
    if tier not in routing_corpus.TIERS:
        raise ValueError(f"请求数档位必须是 {list(routing_corpus.TIERS)} 之一")
    return tier


def build_plan(tier, rpm):
    """把请求数档位折算成一条单步计划：条数与节奏由档位和 RPM 决定。"""
    if isinstance(tier, bool) or not isinstance(tier, int) or tier < 1:
        raise ValueError("请求数档位必须是正整数")
    rpm = float(rpm)
    if not (0.01 <= rpm <= 600000):
        raise ValueError("计划调度 RPM 必须在 0.01 到 600000 之间")
    duration = tier / (rpm / 60)
    return [{"index": 1, "rpm": rpm, "duration": duration, "count": tier}], tier, duration


# 隔离方案版本：标记的格式或位置一旦改变就递增，报告据此判断两次运行是否同口径。
CACHE_SCOPE_SCHEME = "run-side-prefix-v1"
CACHE_SCOPE_DIGITS = 12


def new_cache_scopes():
    """为一次运行的两侧各生成一个互不相同的隔离标记。

    用定长十进制数字而不是十六进制：主流分词器把数字按固定位数切块，两侧标记的
    token 数因此一致，不会让某一侧的提示词系统性地多几个 token。
    """
    baseline = secrets.randbelow(10 ** CACHE_SCOPE_DIGITS)
    candidate = baseline
    while candidate == baseline:
        candidate = secrets.randbelow(10 ** CACHE_SCOPE_DIGITS)
    width = CACHE_SCOPE_DIGITS
    return {"scheme": CACHE_SCOPE_SCHEME,
            "baseline": f"{baseline:0{width}d}", "candidate": f"{candidate:0{width}d}"}


def scoped_prompt(prompt, scope):
    """把隔离标记放在提示词最前面。

    前缀缓存按「从第一个 token 起的最长公共前缀」匹配，标记放在最前面才能让不同侧、
    不同运行的请求从一开始就分叉；放在末尾则完全不起隔离作用。
    """
    if not scope:
        return prompt
    return f"[对照隔离标记 {scope}]\n{prompt}"


def _payload_for(item, model, stream, max_tokens=256, scope=None):
    """用对照清单里的 prompt 构造请求，而不是复用常规压测的模板。

    对照清单的每条请求都带一个很长的共享前缀；换成常规模板会让前缀结构消失，
    缓存命中率这个指标就测不出东西了。``scope`` 是本侧隔离标记，见模块说明第 4 条。
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": scoped_prompt(item["prompt"], scope)}],
        "stream": bool(stream),
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    return payload


class RouteComparison:
    """一次路由对照实验。两侧请求交错发出，结果按 item_id 配对。"""

    def __init__(self, config, on_change=None):
        self.id = uuid.uuid4().hex[:12]
        self.name = config["name"]
        self.config = config
        self.repeat_corpus = routing_corpus.corpus_manifest(config["tier"])
        self.status = "draft"
        self.created = _now()
        self._clock = RunClock(self.created)
        self._restored_timing = None
        self.started_at = None
        self.finished_at = None
        self.error = None
        self.lock = threading.RLock()
        self._stop_event = threading.Event()
        self._execution_loop = None
        self._async_stop_event = None
        self._on_change = on_change
        self.plan = []
        self.total = 0
        self.planned_duration = 0.0
        # 两侧逐条结果。每条请求要参与配对与缓存统计，所以两侧都必须留全；
        # 5000 档两侧合计一万条，量级在几 MB，可接受。
        self.records = {"baseline": [], "candidate": []}
        self.progress = {"done": 0, "scheduled": 0, "inflight": 0}
        self.out_paths = {"baseline": None, "candidate": None}
        self._files = {}
        self._report = None
        self._live_report = None
        self._live_report_key = None
        self._request_cache = {}
        self._history_derived_report = None
        self._history_derived_key = None
        self.history_only = False
        self.retry_policy = USAGE_RETRY_POLICY
        self.cache_scopes = new_cache_scopes()

    # ---- 历史恢复 ---------------------------------------------------------

    @classmethod
    def from_snapshot(cls, snapshot):
        """从历史索引里的一条摘要重建只读记录，绝不自动继续执行。

        索引不保存密钥，恢复后的原记录保持只读。完整冻结参数配合当前可用
        凭据，可由复测接口创建独立记录；中断状态降为 error，不恢复旧执行。
        """
        snapshot = normalize_timing_tree(snapshot)
        run = cls.__new__(cls)
        run.id = str(snapshot.get("id") or uuid.uuid4().hex[:12])
        run.name = str(snapshot.get("name") or "历史路由对照")
        run.repeat_corpus = copy.deepcopy(snapshot.get("corpus"))
        # 新索引保存完整公开 repeat_config；旧索引只存各侧摘要。旧记录缺少的
        # 调度字段用 None 保持报告可读，界面显示「—」，复测不会猜测默认值。
        concurrency = snapshot.get("concurrency") or {}
        frozen = snapshot.get("repeat_config")
        run.config = copy.deepcopy(frozen) if isinstance(frozen, dict) else {"tier": snapshot.get("tier"), "rpm": snapshot.get("rpm"), "name": run.name}
        for side in ("baseline", "candidate"):
            stored = dict(run.config.get(side) or (snapshot.get("sides") or {}).get(side) or {})
            stored.pop("out_path", None)
            stored.setdefault("concurrency", concurrency.get(side) if isinstance(concurrency, dict) else None)
            stored.setdefault("name", side)
            stored.setdefault("capture_raw", bool((snapshot.get("config") or {}).get("capture_raw")))
            run.config[side] = stored
        run.status = "error" if snapshot.get("status") in ("running", "waiting", "stopping") \
            else snapshot.get("status", "error")
        run.created = float(snapshot.get("created") or _now())
        run.started_at = snapshot.get("started_at")
        run.finished_at = snapshot.get("finished_at")
        run._restored_timing = restored_timing(snapshot, interrupted=snapshot.get("status") in ("running", "waiting", "stopping"))
        run.error = snapshot.get("error") or ("服务在路由对照完成前重启，已保留结果文件，未自动继续执行。"
                                              if snapshot.get("status") in ("running", "waiting", "stopping") else None)
        run.lock = threading.RLock()
        run._stop_event = threading.Event()
        run._execution_loop = None
        run._async_stop_event = None
        run._on_change = None
        run.plan = []
        run.total = int(snapshot.get("tier") or 0)
        run.planned_duration = 0.0
        run.records = {"baseline": [None] * int((snapshot.get("sides") or {}).get("baseline", {}).get("records") or 0),
                       "candidate": [None] * int((snapshot.get("sides") or {}).get("candidate", {}).get("records") or 0)}
        run.progress = {"done": len(run.records["baseline"]) + len(run.records["candidate"]),
                        "scheduled": run.total * 2, "inflight": 0}
        # 结果文件名由 run.id 推导，索引里不存路径也能重建；放在这里是为了让
        # 「删除记录」能连带删掉结果文件，而不是留下一堆界面够不到的文件。
        run.out_paths = result_paths(run.id)
        run._files = {}
        run._report = snapshot.get("report")
        run._live_report = None
        run._live_report_key = None
        run._request_cache = {}
        run._history_derived_report = None
        run._history_derived_key = None
        run.history_only = True
        # 旧记录没有重试协议版本，保持未知，不能冒充新协议执行结果。
        run.retry_policy = snapshot.get("retry_policy")
        # 旧版本记录没有隔离标记：保持 None，界面据此提示「这次运行未做缓存隔离」。
        scopes = snapshot.get("cache_isolation")
        run.cache_scopes = dict(scopes) if isinstance(scopes, dict) else None
        return run

    # ---- 生命周期 ---------------------------------------------------------

    def stop(self):
        with self.lock:
            if self.status in ("finished", "stopped", "error"):
                return
            self._clock.mark("stop_requested_at")
            self._clock.mark("dispatch_ended_at")
            self._stop_event.set()
            self.status = "stopping"
            loop, event = self._execution_loop, self._async_stop_event
        # 唤醒低 RPM 下正在等下一个调度时刻的生产者；在途请求仍正常收尾。
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # 执行循环已关闭，线程事件仍保留停止请求。
        self._notify_change()

    def _notify_change(self):
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                # 索引保存失败不应中断请求或阻止密钥清理，结果仍独立写盘。
                pass

    def _open_outputs(self):
        base = _results_dir()
        os.makedirs(base, exist_ok=True)
        for side in ("baseline", "candidate"):
            path = result_paths(self.id)[side]
            handle = open(path, "a", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self._files[side] = handle
            self.out_paths[side] = path

    def _close_outputs(self):
        for handle in self._files.values():
            try:
                handle.close()
            except OSError:
                pass
        self._files = {}

    def _record(self, side, result):
        def clean_value(value):
            for route in ("baseline", "candidate"):
                value = _redact(value, self.config[route].get("api_key"))
            return value

        clean = {key: clean_value(result.get(key)) for key in (
            "schema_version", "ts", "item_id", "index", "mode", "profile", "step_index",
            "status_code", "completed", "error_type", "error_detail", "ttft_ms",
            "total_ms", "reason", "usage_seen", "usage_source", "usage_level",
            "prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens",
            "cache_hit", "burst_first", "timing", "attempts")}
        clean["index"] = routing_ab.request_index(result)
        clean["route_side"] = side
        stored = dict(clean)
        if self.config[side].get("capture_raw"):
            stored.update({key: clean_value(result.get(key)) for key in ("raw_input", "raw_output")})
        with self.lock:
            # 原始正文只写文件；内存配对统计保留小摘要，避免 1 万条大响应撑满内存。
            self.records[side].append(clean)
            handle = self._files.get(side)
            if handle is not None:
                handle.write(json.dumps(stored, ensure_ascii=False) + "\n")
                handle.flush()

    # ---- 执行 -------------------------------------------------------------

    async def _run_side_worker(self, side, cfg, queue, client, semaphore):
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                if self._stop_event.is_set():
                    continue
                async with semaphore:
                    if self._stop_event.is_set():
                        continue
                    payload = _payload_for(item, cfg["model"], cfg["stream"],
                                           scope=(self.cache_scopes or {}).get(side))
                    headers = {"Authorization": f"Bearer {cfg['api_key']}",
                               "Accept": "text/event-stream" if cfg["stream"] else "application/json"}
                    timeout_cfg = {"connect": cfg["connect_timeout"], "read": cfg["read_timeout"],
                                   "write": cfg["write_timeout"], "pool": cfg["connect_timeout"]}
                    job = {"id": item["item_id"], "index": item["index"],
                           "mode": "routing", "profile": "routing",
                           "step_index": item.get("burst_index", 0),
                           **{k: item[k] for k in ("scheduled_at", "enqueued_at", "_scheduled_mono", "_enqueued_mono") if k in item}}
                    include_usage = bool(cfg.get("include_usage", True))
                    attempts = []
                    try:
                        self._clock.dispatched()
                        result = await do_request(
                            client, job, payload, headers, timeout_cfg,
                            chat_url=cfg["chat_url"], include_usage=include_usage,
                            capture_raw=bool(cfg.get("capture_raw")))
                        attempts.append(attempt_timing(result, 1))
                        if not self._stop_event.is_set() and should_retry_without_usage(result, include_usage, cfg["stream"]):
                            cfg["include_usage"] = False
                            retry_job = {k: v for k, v in job.items() if k not in ("scheduled_at", "enqueued_at", "_scheduled_mono", "_enqueued_mono")}
                            self._clock.dispatched()
                            result = await do_request(
                                client, retry_job, payload, headers, timeout_cfg,
                                chat_url=cfg["chat_url"], include_usage=False,
                                capture_raw=bool(cfg.get("capture_raw")))
                            attempts.append(attempt_timing(result, 2))
                    except asyncio.CancelledError as exc:
                        result = getattr(exc, "request_result", None)
                        if result is not None:
                            attempts.append(attempt_timing(result, len(attempts) + 1))
                            result.update(attempts=attempts, index=item["index"])
                            self._record(side, result)
                            with self.lock:
                                self.progress["done"] += 1
                        raise
                    result["attempts"] = attempts
                    # 段首请求在本侧、本次运行里没有任何先行者，理应未命中；
                    # 报告用它检测「实验之外的缓存预热」。
                    result["index"] = item["index"]
                    result["burst_first"] = item["index"] % max(1, int(item.get("burst_size") or 1)) == 0
                    self._record(side, result)
                    with self.lock:
                        self.progress["done"] += 1
            finally:
                if item is not None:
                    with self.lock:
                        self.progress["inflight"] = max(0, self.progress["inflight"] - 1)
                queue.task_done()

    async def _execute_async(self):
        corpus = routing_corpus.build_corpus(self.config["tier"])
        step = self.plan[0]
        rps = step["rpm"] / 60
        loop = asyncio.get_running_loop()
        stop_signal = asyncio.Event()
        with self.lock:
            self._execution_loop = loop
            self._async_stop_event = stop_signal
            if self._stop_event.is_set():
                stop_signal.set()

        # 交错的核心：第 k 条请求的两侧在同一时刻到期，奇偶交替谁先入队。
        # 顺序固定的话，其中一侧永远先占用连接与上游配额，差异会被系统性放大。
        queues = {"baseline": asyncio.Queue(), "candidate": asyncio.Queue()}
        semaphores = {side: asyncio.Semaphore(self.config[side]["concurrency"])
                      for side in ("baseline", "candidate")}

        async def producer():
            base = loop.time()
            wall_base, mono_base = time.time(), time.monotonic()
            for index, item in enumerate(corpus):
                if self._stop_event.is_set():
                    break
                due = base + index / rps
                wait = due - loop.time()
                if wait > 0:
                    try:
                        await asyncio.wait_for(stop_signal.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
                if self._stop_event.is_set():
                    break
                order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                for side in order:
                    queued = {**item, "scheduled_at": wall_base + index / rps,
                              "_scheduled_mono": mono_base + index / rps,
                              "enqueued_at": time.time(), "_enqueued_mono": time.monotonic()}
                    await queues[side].put(queued)
                    with self.lock:
                        self.progress["scheduled"] += 1
                        self.progress["inflight"] += 1
            for side in queues:
                for _ in range(self.config[side]["concurrency"]):
                    await queues[side].put(None)

        try:
            async with httpx.AsyncClient(http2=self.config["baseline"]["http2"]) as base_client, \
                       httpx.AsyncClient(http2=self.config["candidate"]["http2"]) as cand_client:
                clients = {"baseline": base_client, "candidate": cand_client}
                workers = [
                    asyncio.create_task(self._run_side_worker(side, self.config[side], queues[side],
                                                              clients[side], semaphores[side]))
                    for side in ("baseline", "candidate")
                    for _ in range(self.config[side]["concurrency"])
                ]
                pending = [asyncio.create_task(producer()), *workers]
                try:
                    await asyncio.gather(*pending)
                except BaseException:
                    # 一个协程失败时，先取消并收拢全部协程，再关闭客户端/结果文件。
                    # 否则故障被吞掉会显示为完成，其他 worker 还可能继续发请求。
                    for task in pending:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    raise
        finally:
            with self.lock:
                self._execution_loop = None
                self._async_stop_event = None
                self.progress["inflight"] = 0

    def execute(self):
        """同步执行入口；异常一律收敛为 ``error`` 状态，不向调用线程抛。"""
        secrets = [self.config[side].get("api_key") for side in ("baseline", "candidate")]

        def clean_error(exc):
            detail = str(exc)
            for secret in secrets:
                detail = _redact(detail, secret)
            return detail

        try:
            with self.lock:
                self.status = "stopping" if self._stop_event.is_set() else "running"
                if not self._stop_event.is_set():
                    self.started_at = self._clock.mark("started_at")
            self._notify_change()
            if not self._stop_event.is_set():
                self._open_outputs()
                asyncio.run(self._execute_async())
        except asyncio.CancelledError:
            self._clock.mark("stop_requested_at")
            self._clock.mark("dispatch_ended_at")
            self._stop_event.set()
            self.status = "stopping"
        except Exception as exc:  # noqa: BLE001 —— 执行失败必须变成任务状态，不是崩溃
            with self.lock:
                self.status = "error"
                self.error = clean_error(exc)
        finally:
            self._close_outputs()
            self._clock.finish_dispatch()
            self.finished_at = self._clock.mark("ended_at")
            with self.lock:
                if self.status == "error":
                    self.finished_at = self.finished_at or _now()
                else:
                    self.status = "stopped" if self._stop_event.is_set() else "finished"
                    self.finished_at = self._clock.mark("ended_at")
                try:
                    self._report = routing_ab.build_comparison(
                        self.records["baseline"], self.records["candidate"])
                    self._report.setdefault("method", {})["usage_retry_policy"] = self.retry_policy
                except Exception as exc:
                    self.status = "error"
                    self.error = self.error or clean_error(exc)
                finally:
                    # 报告构建失败也必须清理两侧密钥。
                    for side in ("baseline", "candidate"):
                        self.config[side].pop("api_key", None)
            # 最终报告与终态立即写索引，不依赖用户再次创建/删除其他任务。
            self._notify_change()

    # ---- 对外快照 ---------------------------------------------------------

    def _request_rows(self, side):
        """缓存小摘要，历史文件变化时重新读取；不缓存正文或异常原文。"""
        with self.lock:
            if not self.history_only:
                key = ("memory", len(self.records[side]))
                cached = self._request_cache.get(side)
                if cached is not None and cached["key"] == key:
                    return cached
                rows = [public_request_record(record) for record in self.records[side]
                        if isinstance(record, dict)]
                cached = {"key": key, "rows": routing_ab.ordered_records(rows),
                          "available": True, "reason": None, "invalid_lines": 0}
                self._request_cache[side] = cached
                return cached
            path = result_paths(self.id)[side]
            try:
                key = ("file", *_file_signature(path))
            except OSError:
                cached = {"key": ("unavailable",), "rows": [], "available": False,
                          "reason": "结果文件已删除或不可读，无法查看逐条请求。", "invalid_lines": 0}
                self._request_cache[side] = cached
                return cached
            cached = self._request_cache.get(side)
            if cached is not None and cached["key"] == key:
                return cached
            rows, invalid = [], 0
            try:
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                            if not isinstance(record, dict) or not isinstance(record.get("completed"), bool):
                                raise ValueError("记录必须是对象")
                            rows.append(public_request_record(record))
                        except (ValueError, TypeError, OverflowError, RecursionError):
                            invalid += 1
                changed = key != ("file", *_file_signature(path))
            except (OSError, UnicodeError):
                cached = {"key": ("unreadable",), "rows": [], "available": False,
                          "reason": "结果文件不可读，无法查看逐条请求。", "invalid_lines": 0}
                self._request_cache[side] = cached
                return cached
            reason = f"已跳过 {invalid} 条损坏记录，列表只展示可读取的结果。" if invalid else None
            if changed:
                reason = "结果文件在读取时发生变化，下次刷新将重新读取。"
            cached = {"key": None if changed else key, "rows": routing_ab.ordered_records(rows),
                      "available": True, "reason": reason, "invalid_lines": invalid}
            self._request_cache[side] = cached
            return cached

    def request_page(self, side="baseline", offset=0, limit=50):
        """按清单顺序分页查看全部已返回结果，不调用全量报告快照。"""
        if side not in _REQUEST_SIDES:
            raise ValueError("路由侧必须是 baseline（固定路由）或 candidate（智能路由）")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("请求列表起始位置必须是非负整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("请求列表每页条数必须在 1 到 100 之间")
        with self.lock:
            cached = self._request_rows(side)
            total = len(cached["rows"])
            return {"run_id": self.id, "side": side, "offset": offset, "limit": limit,
                    "total": total, "recorded_total": len(self.records[side]),
                    "items": copy.deepcopy(cached["rows"][offset:offset + limit]),
                    "has_more": offset + limit < total, "status": self.status,
                    "available": cached["available"], "reason": cached["reason"],
                    "invalid_lines": cached["invalid_lines"], "order": "request_index"}

    def _derived_history_report(self):
        """旧历史只增加独立的新统计，保留原报告及其当时的计算事实。"""
        if not self.history_only or isinstance((self._report or {}).get("stability"), dict):
            return None
        cached = {side: self._request_rows(side) for side in _REQUEST_SIDES}
        key = (self.status, self.total,
               tuple((side, cached[side]["key"], len(self.records[side])) for side in _REQUEST_SIDES))
        if self._history_derived_key != key or any(cached[side]["key"] is None for side in _REQUEST_SIDES):
            stability = {"protocol": routing_ab.stability_protocol(), "source": "result_files"}
            for side in _REQUEST_SIDES:
                rows = cached[side]["rows"]
                partial = (self.status != "finished" or not cached[side]["available"]
                           or bool(cached[side]["invalid_lines"]) or cached[side]["key"] is None
                           or len(rows) != len(self.records[side])
                           or (self.total > 0 and len(rows) != self.total))
                reason = cached[side]["reason"]
                if not reason and partial:
                    reason = "历史运行未取得全部计划结果，新增统计仅覆盖可读取的结果。"
                stability[side] = {**routing_ab.sequence_stability(rows),
                                   "available": cached[side]["available"], "partial": partial,
                                   "reason": reason}
            self._history_derived_report = {"stability": stability}
            self._history_derived_key = key
        return copy.deepcopy(self._history_derived_report)

    def snap(self):
        with self.lock:
            done = self.progress["done"]
            scheduled = self.progress["scheduled"]
            report = self._report
            live_report = None
            if not self.history_only:
                partial = not (self.status == "finished" and self.total > 0
                               and all(len(self.records[side]) == self.total
                                       for side in ("baseline", "candidate")))
                live_key = (len(self.records["baseline"]), len(self.records["candidate"]), partial)
                if self._live_report_key != live_key:
                    self._live_report = routing_ab.build_live_comparison(
                        self.records["baseline"], self.records["candidate"])
                    self._live_report.update({"partial": partial, "observed_at": _now()})
                    self._live_report_key = live_key
                # 小型聚合快照独立返回，调用方修改不会污染后续缓存。
                live_report = copy.deepcopy(self._live_report)
            return {
                "id": self.id,
                "name": self.name,
                "status": self.status,
                "error": self.error,
                "created": self.created,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "timing": self._restored_timing if self.history_only else self._clock.snapshot(),
                "elapsed": ((self._restored_timing or {}).get("elapsed_ms") or 0) / 1000 if self.history_only else self._clock.elapsed(),
                "tier": self.config["tier"],
                "config": {"capture_raw": bool(self.config["baseline"].get("capture_raw"))},
                "repeat_config": {"name": self.name, "tier": self.config["tier"], "rpm": self.config.get("rpm"),
                                  **{side: _public_side(self.config[side]) for side in ("baseline", "candidate")}},
                "corpus": copy.deepcopy(self.repeat_corpus) if getattr(self, "repeat_corpus", None) else routing_corpus.corpus_manifest(self.config["tier"]),
                "rpm": self.plan[0]["rpm"] if self.plan else None,
                "concurrency": {side: self.config[side]["concurrency"]
                                for side in ("baseline", "candidate")},
                "sides": {
                    side: {"name": self.config[side]["name"],
                           "url": self.config[side]["url"],
                           "model": self.config[side]["model"],
                           "records": len(self.records[side]),
                           "out_path": self.out_paths[side]}
                    for side in ("baseline", "candidate")
                },
                "progress": {"done": done, "scheduled": scheduled, "total": self.total,
                             "pct": (done / (self.total * 2)) if self.total else 0},
                "report": report,
                "derived_report": self._derived_history_report(),
                "live_report": live_report,
                "retry_policy": self.retry_policy,
                "cache_isolation": dict(self.cache_scopes) if self.cache_scopes else None,
                "limits": self._limits(),
            }

    def _limits(self):
        return [
            "两侧共用同一份请求清单并交错发出，但上游容量与限流窗口仍可能随时间漂移。",
            "缓存按运行和侧隔离：每侧请求最前面带本侧专属标记，只能命中本侧本次运行自己预热的前缀；"
            "若网关改写或剥离了提示词开头，隔离会失效，报告以段首命中数提示这种情况。",
            "缓存命中率只统计能取到 usage 的请求；命中只证明本次命中，不能证明缓存还能存活多久。",
            "本对照是黑盒观测，无法看到网关内部的模型替换、渠道切换或重试，只能看到最终结果。",
        ]


def _redact(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(str(secret), "***")
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


# ---- 配置校验与生命周期管理 -------------------------------------------------

# 两侧共用的请求参数：除了 URL / 密钥 / 名称，其余必须逐项相同，否则不是对照实验。
_SIDE_SHARED_KEYS = (
    "model", "concurrency", "rpm", "stream", "profile", "capture_raw",
    "connect_timeout", "read_timeout", "write_timeout", "include_usage", "http2", "insecure",
)


def validate_config(raw):
    """校验一份路由对照配置，返回两侧完全对齐的规范化配置。

    这里做的每一件事都是为了「唯一变量」这句话站得住：
    1. 模型、并发、节奏、超时等**必须逐项相同**，不同就直接拒绝，而不是悄悄取一边的值。
    2. 两侧各自保留自己的 URL 与密钥——这是本次实验唯一允许不同的地方。
    3. 名称、档位、RPM 之外的字段一律丢弃，避免界面草稿里的未知字段流进执行器。
    """
    if not isinstance(raw, dict):
        raise ValueError("路由对照请求体必须是 JSON 对象")
    baseline = raw.get("baseline")
    candidate = raw.get("candidate")
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("路由对照必须提供 baseline（固定路由）与 candidate（智能路由）两侧配置")

    import route_test_ui as ui  # 延迟导入：route_test_ui 是入口模块，直接导入会形成环。

    sides = {}
    for key, side, label in (("baseline", baseline, "固定路由"), ("candidate", candidate, "智能路由")):
        cfg = dict(side)
        if not str(cfg.get("url") or "").strip():
            raise ValueError(f"{label}必须填写网关地址")
        if not str(cfg.get("api_key") or "").strip():
            raise ValueError(f"{label}必须填写 API Key")
        # 借用压测任务的校验器归一化超时、开关等字段：两条路径对同一字段的口径必须一致，
        # 否则「两侧参数完全相同」只是界面上的巧合，不是校验出来的事实。
        normalized = ui.validate_config({**cfg, "mode": "smoke", "iterations": 1}, require_secret=True)
        # 请求地址在这里一次性算好；执行器不再重复解析 URL，避免两侧各自算出不同结果。
        normalized["chat_url"] = ui.strip_url(normalized["url"])
        normalized["name"] = str(cfg.get("name") or label).strip()[:40] or label
        sides[key] = normalized

    base, cand = sides["baseline"], sides["candidate"]
    for key in _SIDE_SHARED_KEYS:
        if base.get(key) != cand.get(key):
            raise ValueError(f"对照实验两侧的 {key} 必须相同，当前为 {base.get(key)!r} / {cand.get(key)!r}")

    tier = validate_tier(raw.get("tier"))
    rpm = ui.safe_float(raw.get("rpm", base.get("rpm") or 100), "计划调度 RPM", 0.01, 600000)
    return {
        "name": str(raw.get("name") or "路由对照").strip()[:80] or "路由对照",
        "tier": tier,
        "rpm": rpm,
        "baseline": base,
        "candidate": cand,
    }


def public_config(cfg):
    """对外摘要：不含密钥，供历史索引与界面状态展示。"""
    return {
        "name": cfg["name"],
        "tier": cfg["tier"],
        "rpm": cfg["rpm"],
        "retry_policy": USAGE_RETRY_POLICY,
        "baseline": _public_side(cfg["baseline"]),
        "candidate": _public_side(cfg["candidate"]),
        "corpus": routing_corpus.corpus_manifest(cfg["tier"]),
        "tiers": routing_corpus.all_tier_summaries(),
    }


def _public_side(cfg):
    return {key: value for key, value in cfg.items() if key != "api_key"}


class RoutingCompareManager:
    """对照实验的创建、查询、停止与删除。

    与 ComparisonManager 的分工不同：那边是「多个实验组串行、每组一条压测任务」，
    这边是「同一个清单交错打在两条路由上」，因此这里不派生子任务，一条记录对应
    两侧各一个结果文件。
    """

    def __init__(self, on_change=None):
        self.runs = {}
        self.lock = threading.RLock()
        self._on_change = on_change

    def create(self, raw):
        cfg = validate_config(raw)
        run = RouteComparison(cfg, on_change=self._on_change)
        run.status = "waiting"
        run.plan, run.total, run.planned_duration = build_plan(cfg["tier"], cfg["rpm"])
        with self.lock:
            self.runs[run.id] = run
        run._notify_change()
        threading.Thread(target=run.execute, daemon=True).start()
        return run

    def get(self, run_id):
        with self.lock:
            return self.runs.get(run_id)

    def stop(self, run_id):
        with self.lock:
            run = self.runs.get(run_id)
            if not run:
                return "路由对照不存在"
        run.stop()
        return None

    def delete(self, run_id):
        """删除记录时必须一并删除结果文件。

        结果文件是以 run.id 命名的，两条路由各一份；只删记录会把文件永久留在
        磁盘上，而记录已经不存在，用户再也无法通过界面清理它们。
        """
        with self.lock:
            run = self.runs.get(run_id)
            if not run:
                return "路由对照不存在"
            if run.status in ("running", "waiting", "stopping"):
                return "请等待路由对照结束后再删除"
            del self.runs[run_id]
            # 路径由 id 重算而不是读 run.out_paths：恢复出来的历史记录可能没有
            # 结果文件（换机器、手工清理过），重算才能把真实存在的文件删干净。
            paths = list(result_paths(run_id).values())
        failed = []
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except OSError:
                failed.append(path)
        if failed:
            return "记录已删除，但部分结果文件删除失败"
        return None

    def ordered(self):
        with self.lock:
            return [run.snap() for run in sorted(self.runs.values(), key=lambda item: -item.created)]
