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

这个模块只负责执行与汇总，不发通知、不写全局历史；执行结果落盘逻辑与压测任务保持
一致：JSONL（逐行 JSON）逐条追加，权限 0600。
"""
import asyncio
import json
import os
import threading
import time
import uuid

import httpx

import routing_ab
import routing_corpus
from smart_route_load import CHAT_PATH, do_request


def _now():
    return time.time()


def _results_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


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


def _payload_for(item, model, stream, max_tokens=256):
    """用对照清单里的 prompt 构造请求，而不是复用常规压测的模板。

    对照清单的每条请求都带一个很长的共享前缀；换成常规模板会让前缀结构消失，
    缓存命中率这个指标就测不出东西了。
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": item["prompt"]}],
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
        self.status = "draft"
        self.created = _now()
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
        self.history_only = False

    # ---- 历史恢复 ---------------------------------------------------------

    @classmethod
    def from_snapshot(cls, snapshot):
        """从历史索引里的一条摘要重建只读记录，绝不自动继续执行。

        索引里没有密钥（也从没有过），所以恢复出来的记录只能看报告，
        不能重跑；中断过的状态一律降级为 error，避免界面上看起来还在跑。
        """
        run = cls.__new__(cls)
        run.id = str(snapshot.get("id") or uuid.uuid4().hex[:12])
        run.name = str(snapshot.get("name") or "历史路由对照")
        # 索引只存每个侧面的公开字段，执行器要的调度字段（并发、超时…）并没有存；
        # 这里补齐成执行器预期的最小形状，快照才能照常渲染。缺的字段一律 None，
        # 界面显示为「—」，绝不假装成某个默认值。
        concurrency = snapshot.get("concurrency") or {}
        run.config = {"tier": snapshot.get("tier"), "rpm": snapshot.get("rpm"), "name": run.name}
        for side in ("baseline", "candidate"):
            stored = dict((snapshot.get("sides") or {}).get(side) or {})
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
        run.history_only = True
        return run

    # ---- 生命周期 ---------------------------------------------------------

    def stop(self):
        with self.lock:
            if self.status in ("finished", "stopped", "error"):
                return
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
            "schema_version", "ts", "item_id", "mode", "profile", "step_index",
            "status_code", "completed", "error_type", "error_detail", "ttft_ms",
            "total_ms", "reason", "usage_seen", "usage_source", "usage_level",
            "prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens",
            "cache_hit")}
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
                    payload = _payload_for(item, cfg["model"], cfg["stream"])
                    headers = {"Authorization": f"Bearer {cfg['api_key']}",
                               "Accept": "text/event-stream" if cfg["stream"] else "application/json"}
                    timeout_cfg = {"connect": cfg["connect_timeout"], "read": cfg["read_timeout"],
                                   "write": cfg["write_timeout"], "pool": cfg["connect_timeout"]}
                    job = {"id": item["item_id"], "index": item["index"],
                           "mode": "routing", "profile": "routing",
                           "step_index": item.get("burst_index", 0)}
                    include_usage = bool(cfg.get("include_usage", True))
                    result = await do_request(
                        client, job, payload, headers, timeout_cfg,
                        chat_url=cfg["chat_url"], include_usage=include_usage,
                        capture_raw=bool(cfg.get("capture_raw")))
                    # 网关不认 stream_options.include_usage 时通常直接 400；
                    # 降级为不带 usage 重试一次，避免把兼容性差异记成请求失败。
                    if not self._stop_event.is_set() and include_usage \
                            and 400 <= (result.get("status_code") or 0) < 500 \
                            and not result.get("usage_seen"):
                        cfg["include_usage"] = False
                        result = await do_request(
                            client, job, payload, headers, timeout_cfg,
                            chat_url=cfg["chat_url"], include_usage=False,
                            capture_raw=bool(cfg.get("capture_raw")))
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
                    await queues[side].put(item)
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
                self.started_at = _now()
            self._notify_change()
            if not self._stop_event.is_set():
                self._open_outputs()
                asyncio.run(self._execute_async())
        except Exception as exc:  # noqa: BLE001 —— 执行失败必须变成任务状态，不是崩溃
            with self.lock:
                self.status = "error"
                self.error = clean_error(exc)
        finally:
            self._close_outputs()
            with self.lock:
                if self.status == "error":
                    self.finished_at = self.finished_at or _now()
                else:
                    self.status = "stopped" if self._stop_event.is_set() else "finished"
                    self.finished_at = _now()
                try:
                    self._report = routing_ab.build_comparison(
                        self.records["baseline"], self.records["candidate"])
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

    def snap(self):
        with self.lock:
            done = self.progress["done"]
            scheduled = self.progress["scheduled"]
            report = self._report
            return {
                "id": self.id,
                "name": self.name,
                "status": self.status,
                "error": self.error,
                "created": self.created,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": (max(0, (self.finished_at or _now()) - self.started_at)
                            if self.started_at else 0),
                "tier": self.config["tier"],
                "config": {"capture_raw": bool(self.config["baseline"].get("capture_raw"))},
                "corpus": routing_corpus.corpus_manifest(self.config["tier"]),
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
                "limits": self._limits(),
            }

    def _limits(self):
        return [
            "两侧共用同一份请求清单并交错发出，但上游容量、缓存状态和限流窗口仍可能随时间漂移。",
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
    rpm = ui.safe_float(raw.get("rpm", base.get("rpm") or 600), "计划调度 RPM", 0.01, 600000)
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
