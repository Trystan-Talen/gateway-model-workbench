"""Durable, isolated model fingerprint collection, independent of the web UI.

Collection sends only fixed, single-turn chat completions. Model discovery is
a separate, explicit read-only operation. Keys live in runtime memory, never in
a run, artifact, reference, log, or exception message.
The synchronous manager API is safe to call from the existing HTTP server.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from fingerprint_protocol import (
    build_artifact,
    compare_artifacts,
    make_plan,
    make_protocol,
    normalize_answer,
    validate_import,
)

TERMINAL = frozenset({"finished", "error", "stopped", "interrupted"})
ACTIVE = frozenset({"waiting", "running", "stopping"})
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RAW_CHARS = 4096
MAX_CONSECUTIVE_REASONING_TRUNCATIONS = 3
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MODEL_DISCOVERY_TIMEOUT_SECONDS = 15
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "reported_cost")
RUN_FIELDS = (
    "id", "name", "mode", "status", "created", "started_at", "finished_at",
    "endpoint", "protocol", "progress", "error", "reference_id", "comparison", "diagnostics",
)
CONFIG_FIELDS = frozenset({
    "name", "mode", "endpoint", "preset", "samples_per_cell", "concurrency",
    "timeout_seconds", "adapter", "reference_id", "trusted_source", "source_note", "risk_acknowledged",
})
FATAL_HTTP = frozenset({400, 401, 403, 404, 405, 413, 422})


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _atomic_json(path, data):
    """Replace metadata atomically and restrict both temporary and final files."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        # Persist the rename as well as the contents (POSIX; best effort elsewhere).
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _identifier(value):
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _stored_date(value, *, optional=False):
    """Reject corrupted timestamps before they can break history sorting/rendering."""
    if optional and value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValueError("记录日期缺失或无效")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("记录日期必须包含时区")
    parsed.astimezone(timezone.utc)


def _text(value, label, maximum, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{label}不能超过 {maximum} 个字符")
    if required and not value:
        raise ValueError(f"请填写{label}")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{label}包含无效控制字符")
    return value


def _endpoint(raw):
    if not isinstance(raw, dict) or set(raw) - {"url", "model", "api_key"}:
        raise ValueError("通道只支持地址、模型和密钥，不支持额外请求头或参数")
    url = _text(raw.get("url"), "通道地址", 2048, required=True)
    model = _text(raw.get("model"), "模型名称", 256, required=True)
    key = _text(raw.get("api_key"), "密钥", 8192)
    if any(c.isspace() for c in url) or any(c in key for c in "\r\n"):
        raise ValueError("通道地址或密钥包含无效空白字符")
    try:
        parts = urlsplit(url)
        port = parts.port  # Validate malformed/out-of-range ports now.
    except ValueError:
        raise ValueError("通道地址或端口无效") from None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("通道地址必须是有效的 HTTP（超文本传输协议）或 HTTPS（加密超文本传输协议）地址")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("通道地址不能包含账号密码、查询参数或片段；密钥请填写在独立字段")
    if "\\" in url:
        raise ValueError("通道地址不能包含反斜杠")
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1/chat/completions"
    elif not path.endswith("/chat/completions"):
        path += "/chat/completions"
    host = parts.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    url = urlunsplit((parts.scheme.lower(), authority, path, "", ""))
    return {"url": url, "model": model}, key


def _model_discovery_client():
    return httpx.AsyncClient(timeout=httpx.Timeout(MODEL_DISCOVERY_TIMEOUT_SECONDS),
                             follow_redirects=False, trust_env=False,
                             limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))


class _ModelDiscoveryError(ValueError):
    """An intentionally authored, credential-free discovery error."""


async def _discover_models_async(endpoint, key):
    async def receive():
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        async with _model_discovery_client() as client:
            # Independent Request prevents cookies or other session defaults from
            # becoming hidden input. There is exactly one explicit GET request.
            request = httpx.Request("GET", endpoint, headers=headers)
            response = await client.send(request, stream=True)
            try:
                status = response.status_code
                if status in {401, 403}:
                    raise _ModelDiscoveryError("模型列表鉴权失败，请检查密钥；也可手动填写模型名称")
                if status in {404, 405, 501}:
                    raise _ModelDiscoveryError("该通道不支持标准模型列表接口，请手动填写模型名称")
                if 300 <= status < 400:
                    raise _ModelDiscoveryError("模型列表接口发生重定向，未跟随；请填写最终通道地址，或手动填写模型名称")
                if status == 429:
                    raise _ModelDiscoveryError("模型列表接口被限流，请稍后重试，或手动填写模型名称")
                if not 200 <= status < 300:
                    raise _ModelDiscoveryError(f"模型列表接口返回 HTTP（超文本传输协议状态）{status}；可手动填写模型名称")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise _ModelDiscoveryError("模型列表响应超过 1 MiB（兆字节），已停止读取；请手动填写模型名称")
                    content.extend(chunk)
                try:
                    payload = json.loads(bytes(content))
                except (ValueError, UnicodeError, RecursionError):
                    raise _ModelDiscoveryError("模型列表响应不是有效的结构数据，请手动填写模型名称") from None
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise _ModelDiscoveryError("通道未返回标准模型列表，请手动填写模型名称")
                models, seen = [], set()
                for entry in payload["data"]:
                    identifier = entry.get("id") if isinstance(entry, dict) else None
                    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                            or identifier != identifier.strip()
                            or any(ord(char) < 32 or ord(char) == 127 for char in identifier)
                            or (key and key in identifier) or identifier in seen):
                        continue
                    seen.add(identifier)
                    models.append(identifier)
                return {"models": models, "endpoint": endpoint}
            finally:
                await response.aclose()
    try:
        # This also bounds slow trickle responses, rather than only socket waits.
        return await asyncio.wait_for(receive(), timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
        raise _ModelDiscoveryError("获取模型列表超时；未重试，可手动填写模型名称") from None
    except httpx.HTTPError:
        raise _ModelDiscoveryError("获取模型列表时网络连接失败；未重试，可手动填写模型名称") from None


def discover_models(config):
    """Fetch one standard model list, without storage, negotiation, or sampling."""
    if not isinstance(config, dict) or set(config) - {"url", "api_key"}:
        raise ValueError("获取模型列表只支持通道地址与密钥")
    public, key = _endpoint({"url": config.get("url"), "api_key": config.get("api_key"), "model": "model-discovery"})
    if key and key in public["url"]:
        raise ValueError("密钥只能填写在独立密钥字段中")
    endpoint = public["url"][:-len("/chat/completions")] + "/models"
    try:
        return asyncio.run(_discover_models_async(endpoint, key))
    except _ModelDiscoveryError as exc:
        raise ValueError(str(exc)) from None
    except Exception:
        # Never expose a transport exception, response body, or supplied key.
        raise ValueError("获取模型列表失败，请检查通道配置；也可手动填写模型名称") from None


def _redact(value, key):
    """Even a malicious upstream echo must not write the supplied key to disk."""
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, "[密钥已隐藏]")
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    return value


def _number(value, *, integer=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0 or (integer and int(value) != value):
            return None
    except (ValueError, OverflowError):
        return None
    return int(value) if integer else value


def _usage(payload):
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return dict.fromkeys(USAGE_KEYS)
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    return {
        "prompt_tokens": _number(usage.get("prompt_tokens")),
        "completion_tokens": _number(usage.get("completion_tokens")),
        "reasoning_tokens": _number(completion_details.get("reasoning_tokens", usage.get("reasoning_tokens"))),
        "cached_tokens": _number(prompt_details.get("cached_tokens", usage.get("cached_tokens"))),
        "reported_cost": _number(usage.get("cost", usage.get("reported_cost")), integer=False),
    }


def _content(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part if isinstance(part, str) else part.get("text", "")
                       for part in content
                       if isinstance(part, str) or (isinstance(part, dict) and isinstance(part.get("text"), str)))
    return ""


class _Runtime:
    def __init__(self, key):
        self.key = key
        self.stop = threading.Event()
        self.loop = None
        self.tasks = set()
        self.fatal_error = None
        self.last_persist = time.monotonic()
        self.reasoning_truncation_streak = 0


class FingerprintManager:
    def __init__(self, base_dir, *, limiter=None):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "fingerprint_data"
        self.runs_dir = self.data_dir / "runs"
        self.references_dir = self.data_dir / "references"
        for directory in (self.data_dir, self.runs_dir, self.references_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        self.limiter = limiter
        self.lock = threading.RLock()
        self._condition = threading.Condition(self.lock)
        self._runs = {}
        self._references = {}
        self._runtimes = {}
        self._queue = deque()
        self._thread = None
        self._closed = False
        self._restored = False
        self.storage_warnings = []

    def _run_path(self, run_id):
        if not _identifier(run_id):
            raise ValueError("采集记录编号无效")
        return self.runs_dir / f"{run_id}.json"

    def _samples_path(self, run_id):
        if not _identifier(run_id):
            raise ValueError("采集记录编号无效")
        return self.runs_dir / f"{run_id}.samples.jsonl"

    def _reference_path(self, reference_id):
        if not _identifier(reference_id):
            raise ValueError("参考编号无效")
        return self.references_dir / f"{reference_id}.json"

    def _save_run(self, run):
        _atomic_json(self._run_path(run["id"]), run)

    def _quarantine(self, path):
        destination = path.with_suffix(path.suffix + f".corrupt-{uuid.uuid4().hex[:8]}")
        try:
            path.rename(destination)
            os.chmod(destination, 0o600)
            self.storage_warnings.append(f"已隔离损坏记录：{path.name}")
        except OSError:
            self.storage_warnings.append(f"无法读取记录：{path.name}；其他记录仍可使用")

    def restore(self):
        """Load durable records, never resume a previous process's requests."""
        with self.lock:
            if self._restored:
                return
            for path in sorted(self.references_dir.glob("*.json")):
                try:
                    reference = json.loads(path.read_text(encoding="utf-8"))
                    if (not isinstance(reference, dict) or not _identifier(reference.get("id"))
                            or path.stem != reference["id"] or not isinstance(reference.get("artifact"), dict)
                            or not isinstance(reference.get("name"), str)
                            or not isinstance(reference.get("trusted_source"), bool)
                            or not isinstance(reference.get("created"), str)):
                        raise ValueError("invalid reference")
                    _stored_date(reference["created"])
                    reference["artifact"] = validate_import(reference["artifact"])
                    self._references[reference["id"]] = reference
                    os.chmod(path, 0o600)
                except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
                    self._references.pop(path.stem, None)
                    self._quarantine(path)
            for path in sorted(self.runs_dir.glob("*.json")):
                try:
                    run = json.loads(path.read_text(encoding="utf-8"))
                    if (not isinstance(run, dict) or not _identifier(run.get("id"))
                            or path.stem != run["id"] or not set(RUN_FIELDS).issubset(run)
                            or run["status"] not in ACTIVE | TERMINAL
                            or not isinstance(run.get("protocol"), dict)
                            or not isinstance(run.get("endpoint"), dict)
                            or set(run["endpoint"]) != {"url", "model"}
                            or not isinstance(run.get("seed"), int)):
                        raise ValueError("invalid run")
                    _stored_date(run["created"])
                    _stored_date(run.get("started_at"), optional=True)
                    _stored_date(run.get("finished_at"), optional=True)
                    if run.get("artifact") is not None:
                        run["artifact"] = validate_import(run["artifact"])
                        run["diagnostics"] = copy.deepcopy(run["artifact"].get("diagnostics", {}))
                    if run["status"] in ACTIVE:
                        run["status"] = "interrupted"
                        run["finished_at"] = _now()
                        run["error"] = "服务进程已重启，采集已中断；不会自动补发请求"
                        self._runs[run["id"]] = run
                        self._finalize_artifact(run, complete=False)
                        self._save_run(run)
                    self._runs[run["id"]] = run
                    os.chmod(path, 0o600)
                except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
                    self._runs.pop(path.stem, None)
                    self._quarantine(path)
            self._restored = True

    def create(self, config):
        self.restore()
        if not isinstance(config, dict):
            raise ValueError("采集配置必须是对象")
        if set(config) - CONFIG_FIELDS:
            raise ValueError("采集配置含不支持的字段，不能注入请求体、消息、工具或请求头")
        if config.get("risk_acknowledged") is not True:
            raise ValueError("请先确认请求数量及潜在费用")
        mode = config.get("mode", "selfcheck")
        if not isinstance(mode, str) or mode not in {"selfcheck", "reference", "verify"}:
            raise ValueError("采集模式无效")
        name = _text(config.get("name"), "任务名称", 120) or {"selfcheck": "通道自检", "reference": "参考采集", "verify": "模型指纹对照"}[mode]
        source_note = _text(config.get("source_note"), "来源说明", 1000)
        trusted = config.get("trusted_source", False)
        if not isinstance(trusted, bool):
            raise ValueError("可信来源声明必须为布尔值")
        if mode == "reference" and (trusted is not True or not source_note):
            raise ValueError("采集参考必须声明可信来源并填写来源说明")
        endpoint, key = _endpoint(config.get("endpoint"))
        if key and any(key in value for value in (endpoint["url"], endpoint["model"], name, source_note)):
            raise ValueError("密钥只能填写在独立密钥字段中")
        protocol = make_protocol({field: config[field] for field in
                                  ("preset", "samples_per_cell", "concurrency", "timeout_seconds", "adapter")
                                  if field in config})
        # Validate before freezing/queueing; never silently amend an existing protocol.
        if urlsplit(endpoint["url"]).hostname == "api.deepseek.com" and protocol["adapter"] != "deepseek":
            raise ValueError("DeepSeek（深度求索）官方接口默认启用思考，请选择‘DeepSeek（深度求索）：关闭思考’适配；对照任务需使用相同适配重新采集参考")
        seed = secrets.randbits(63)
        plan = make_plan(protocol, seed)
        if not plan:
            raise ValueError("冻结协议没有可执行探针")
        reference_id = config.get("reference_id")
        if reference_id not in (None, "") and not _identifier(reference_id):
            raise ValueError("参考编号无效")
        reference_id = reference_id or None
        with self._condition:
            if self._closed:
                raise ValueError("采集服务正在关闭")
            if mode == "verify" and reference_id not in self._references:
                raise ValueError("请选择存在的参考指纹")
            if mode != "verify" and reference_id:
                raise ValueError("只有对照采集模式可以指定参考")
            run_id = uuid.uuid4().hex
            run = {
                "id": run_id, "name": name, "mode": mode, "status": "waiting",
                "created": _now(), "started_at": None, "finished_at": None,
                "endpoint": endpoint, "protocol": protocol, "seed": seed,
                "progress": {"done": 0, "total": len(plan), "valid": 0, "errors": 0},
                "error": None, "reference_id": reference_id, "comparison": None,
                "diagnostics": {}, "artifact": None, "source_note": source_note,
                "trusted_source": trusted,
            }
            sample_path = self._samples_path(run_id)
            try:
                fd = os.open(sample_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                self._save_run(run)
            except OSError:
                sample_path.unlink(missing_ok=True)
                raise ValueError("无法保存采集配置，请检查存储空间及目录权限") from None
            self._runs[run_id] = run
            self._runtimes[run_id] = _Runtime(key)
            self._queue.append(run_id)
            snapshot = self._snapshot(run)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._scheduler, name="fingerprint-scheduler", daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return snapshot

    def _summary(self, run):
        result = {key: copy.deepcopy(run.get(key)) for key in RUN_FIELDS}
        if run.get("storage_warning"):
            result["storage_warning"] = run["storage_warning"]
        return result

    def _snapshot(self, run):
        snapshot = self._summary(run)
        snapshot["artifact"] = copy.deepcopy(run.get("artifact"))
        snapshot["source_note"] = run.get("source_note", "")
        if self.storage_warnings:
            snapshot["storage_warning"] = "; ".join(filter(None, [snapshot.get("storage_warning"), *self.storage_warnings]))
        return snapshot

    def list_runs(self):
        self.restore()
        with self.lock:
            return [self._summary(run) for run in sorted(self._runs.values(), key=lambda r: r["created"], reverse=True)]

    def get(self, run_id):
        self.restore()
        with self.lock:
            run = self._runs.get(run_id)
            return self._snapshot(run) if run else None

    def _require_run(self, run_id):
        run = self._runs.get(run_id)
        if run is None:
            raise ValueError("采集记录不存在")
        return run

    def stop(self, run_id):
        self.restore()
        with self._condition:
            run = self._require_run(run_id)
            if run["status"] in TERMINAL:
                return self._snapshot(run)
            runtime = self._runtimes.get(run_id)
            if runtime:
                runtime.stop.set()
            if run["status"] == "waiting":
                try:
                    self._queue.remove(run_id)
                except ValueError:
                    pass
                run["status"] = "stopped"
                run["finished_at"] = _now()
                self._finalize_artifact(run, complete=False)
                self._runtimes.pop(run_id, None)
                if runtime:
                    runtime.key = ""
            else:
                run["status"] = "stopping"
                if runtime and runtime.loop:
                    try:
                        runtime.loop.call_soon_threadsafe(self._cancel_tasks, runtime)
                    except RuntimeError:
                        pass  # The worker is already closing its loop.
            self._save_run(run)
            self._condition.notify_all()
            return self._snapshot(run)

    @staticmethod
    def _cancel_tasks(runtime):
        for task in tuple(runtime.tasks):
            if not task.done():
                task.cancel()

    def delete(self, run_id):
        self.restore()
        with self.lock:
            run = self._require_run(run_id)
            if run["status"] not in TERMINAL:
                raise ValueError("请先停止采集，等待结算完成后再删除")
            # Metadata deletion is the commit point; a leftover sample file is harmless.
            self._run_path(run_id).unlink(missing_ok=True)
            self._runs.pop(run_id)
            self._samples_path(run_id).unlink(missing_ok=True)

    def _read_samples(self, run):
        path = self._samples_path(run["id"])
        samples = []
        damaged = False
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        sample = json.loads(line)
                        if not isinstance(sample, dict) or not isinstance(sample.get("index"), int) or "cell_id" not in sample:
                            raise ValueError("invalid sample")
                        samples.append(sample)
                    except (ValueError, TypeError):
                        damaged = True
        except (OSError, UnicodeError):
            damaged = True
        if damaged:
            run["storage_warning"] = "部分原始样本损坏或缺失；仅保留可读取样本，不能据此给出严格结论"
        return samples, damaged

    def samples(self, run_id, offset=0, limit=100):
        self.restore()
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("样本偏移必须是非负整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("每页样本数量必须在 1 至 500 之间")
        with self.lock:
            run = self._require_run(run_id)
            samples, _ = self._read_samples(run)
            samples.sort(key=lambda item: item["index"])
            return {"samples": samples[offset:offset + limit], "total": len(samples), "offset": offset, "limit": limit}

    def export(self, run_id):
        self.restore()
        with self.lock:
            run = self._require_run(run_id)
            if run["status"] not in TERMINAL or not run.get("artifact"):
                raise ValueError("采集尚未结算，完成或停止后可导出证据")
            return copy.deepcopy(run["artifact"])

    def _reference_summary(self, reference):
        artifact = reference["artifact"]
        return {**{key: copy.deepcopy(reference.get(key)) for key in
                   ("id", "name", "source_note", "trusted_source", "imported", "created")},
                **{key: copy.deepcopy(artifact.get(key)) for key in
                   ("protocol", "endpoint", "collected_at", "complete", "diagnostics")}}

    def list_references(self):
        self.restore()
        with self.lock:
            return [self._reference_summary(ref) for ref in sorted(self._references.values(), key=lambda r: r["created"], reverse=True)]

    def get_reference(self, reference_id):
        self.restore()
        with self.lock:
            reference = self._references.get(reference_id)
            return copy.deepcopy(reference) if reference else None

    def _save_reference(self, artifact, *, name, source_note, trusted_source, imported, reference_id=None):
        reference_id = reference_id or uuid.uuid4().hex
        artifact = copy.deepcopy(artifact)
        artifact.setdefault("source", {}).update({"trusted_source": trusted_source, "source_note": source_note})
        reference = {"id": reference_id, "name": name, "source_note": source_note,
                     "trusted_source": trusted_source, "imported": imported,
                     "artifact": artifact, "created": _now()}
        _atomic_json(self._reference_path(reference_id), reference)
        self._references[reference_id] = reference
        return reference

    def import_reference(self, data, name, source_note, trusted_source):
        self.restore()
        name = _text(name, "参考名称", 120, required=True)
        source_note = _text(source_note, "来源说明", 1000)
        if not isinstance(trusted_source, bool):
            raise ValueError("可信来源声明必须为布尔值")
        if trusted_source and not source_note:
            raise ValueError("声明可信来源时请填写来源说明")
        try:
            size = len(_json_bytes(data))
        except (ValueError, TypeError, OverflowError):
            raise ValueError("参考文件必须是有效的有限结构数据") from None
        if size > MAX_IMPORT_BYTES:
            raise ValueError("参考文件不能超过 2 MiB（兆字节）")
        artifact = validate_import(data)
        with self.lock:
            reference = self._save_reference(artifact, name=name, source_note=source_note,
                                             trusted_source=trusted_source, imported=True)
            return copy.deepcopy(reference)

    def delete_reference(self, reference_id):
        self.restore()
        with self.lock:
            if reference_id not in self._references:
                raise ValueError("参考指纹不存在")
            if any(run["reference_id"] == reference_id and run["status"] in ACTIVE for run in self._runs.values()):
                raise ValueError("该参考正在被采集任务使用，请先停止任务")
            self._reference_path(reference_id).unlink(missing_ok=True)
            self._references.pop(reference_id)

    def _compare(self, artifact, reference):
        comparison = compare_artifacts(artifact, reference["artifact"])
        if not reference.get("trusted_source"):
            reason = "参考来源未声明可信，仅能观察行为差异，不能据此认定与指定模型一致或存在明显差异"
            comparison.setdefault("warnings", []).append(reason)
            if comparison.get("verdict") in {"match", "mismatch"}:
                comparison.update({"verdict": "uncertain", "label": "待复测"})
                comparison.setdefault("reasons", []).append(reason)
        comparison.update({"reference_id": reference["id"], "reference_name": reference["name"], "compared_at": _now()})
        return comparison

    def compare(self, run_id, reference_id):
        self.restore()
        if not _identifier(reference_id):
            raise ValueError("参考编号无效，请选择有效的参考指纹")
        with self.lock:
            run = self._require_run(run_id)
            if run["status"] not in TERMINAL or not run.get("artifact"):
                raise ValueError("请等待采集结算后进行离线对照")
            reference = self._references.get(reference_id)
            if reference is None:
                raise ValueError("参考指纹不存在")
            comparison = self._compare(run["artifact"], reference)
            run["comparison"] = comparison
            self._save_run(run)
            return copy.deepcopy(comparison)

    def _scheduler(self):
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                run_id = self._queue[0]
                run = self._runs.get(run_id)
                runtime = self._runtimes.get(run_id)
                if not run or not runtime or run["status"] != "waiting":
                    self._queue.popleft()
                    continue
                try:
                    acquired = self.limiter is None or self.limiter.acquire(run_id)
                except Exception:
                    acquired = False
                    run["storage_warning"] = "全局并发闸门暂不可用，任务保持等待"
                if not acquired:
                    self._condition.wait(timeout=0.25)
                    continue
                self._queue.popleft()
                run["status"] = "running"
                run["started_at"] = _now()
            try:
                with self.lock:
                    self._save_run(run)
                asyncio.run(self._execute(run, runtime))
            except Exception:
                # Never stringify transport/execution exceptions: they can contain credentials.
                runtime.fatal_error = runtime.fatal_error or "采集执行失败，已停止后续请求；请检查通道配置与存储状态"
            finally:
                with self._condition:
                    try:
                        if runtime.fatal_error:
                            run["status"] = "error"
                            run["error"] = runtime.fatal_error
                        elif runtime.stop.is_set():
                            run["status"] = "stopped"
                        else:
                            run["status"] = "finished"
                        run["finished_at"] = _now()
                        self._finalize_artifact(run, complete=run["status"] == "finished")
                        if run["status"] == "finished" and run["mode"] == "reference":
                            # Stable id makes a recovered reference unambiguous if metadata persistence fails.
                            reference = self._save_reference(run["artifact"], name=run["name"], source_note=run["source_note"],
                                                             trusted_source=True, imported=False, reference_id=run_id)
                            run["reference_id"] = reference["id"]
                        if run["mode"] == "verify" and run.get("reference_id") in self._references:
                            run["comparison"] = self._compare(run["artifact"], self._references[run["reference_id"]])
                        self._save_run(run)
                    except Exception:
                        run["status"] = "error"
                        run["error"] = "采集已停止，但结果结算或保存失败；请检查存储空间与权限"
                        run["storage_warning"] = "内存结果可能未落盘；已逐条保存的样本会在重启后恢复"
                        try:
                            self._save_run(run)
                        except Exception:
                            pass
                    finally:
                        runtime.key = ""
                        runtime.loop = None
                        runtime.tasks.clear()
                        self._runtimes.pop(run_id, None)
                        if self.limiter is not None:
                            try:
                                self.limiter.release(run_id)
                            except Exception:
                                self.storage_warnings.append("释放全局并发名额失败，请检查任务调度状态")
                        self._condition.notify_all()

    def _new_client(self, protocol):
        # trust_env=False avoids ambient proxy/auth injection; redirects never receive credentials.
        return httpx.AsyncClient(timeout=httpx.Timeout(protocol["timeout_seconds"]),
                                 follow_redirects=False, trust_env=False,
                                 limits=httpx.Limits(max_connections=protocol["concurrency"],
                                                    max_keepalive_connections=protocol["concurrency"]))

    async def _execute(self, run, runtime):
        runtime.loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        for probe in make_plan(run["protocol"], run["seed"]):
            queue.put_nowait(probe)
        async with self._new_client(run["protocol"]) as client:
            async def worker():
                while not runtime.stop.is_set() and not runtime.fatal_error:
                    try:
                        probe = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    sample = await self._collect(client, run, runtime, probe)
                    try:
                        self._append_sample(run, runtime, sample)
                    except Exception:
                        runtime.fatal_error = "无法逐条保存样本，已停止所有后续请求；请检查存储空间与目录权限"
                    finally:
                        queue.task_done()
                    reasoning_exhausted = (sample["category"] == "invalid" and sample["truncated"]
                                           and not sample["raw"].strip() and sample["reasoning_observed"])
                    runtime.reasoning_truncation_streak = runtime.reasoning_truncation_streak + 1 if reasoning_exhausted else 0
                    if runtime.reasoning_truncation_streak >= MAX_CONSECUTIVE_REASONING_TRUNCATIONS:
                        runtime.fatal_error = ("连续 3 次回答为空且在推理阶段耗尽输出预算，已停止后续请求；"
                                               "请确认接口支持关闭思考，选择对应适配后新建任务。本次未重试、未放宽令牌上限")
                    if sample["status_code"] in FATAL_HTTP:
                        runtime.fatal_error = f"通道返回 HTTP（超文本传输协议状态）{sample['status_code']}，已停止；请修正鉴权、地址或固定采样参数后新建任务"
                    if runtime.fatal_error:
                        current = asyncio.current_task()
                        for task in tuple(runtime.tasks):
                            if task is not current and not task.done():
                                task.cancel()
                        return
                    if runtime.stop.is_set():
                        return
            if runtime.stop.is_set():
                return
            runtime.tasks = {asyncio.create_task(worker()) for _ in range(run["protocol"]["concurrency"])}
            outcomes = await asyncio.gather(*runtime.tasks, return_exceptions=True)
            if any(isinstance(outcome, Exception) for outcome in outcomes):
                runtime.fatal_error = runtime.fatal_error or "采集工作器意外退出，已停止后续请求"

    async def _collect(self, client, run, runtime, probe):
        started = time.monotonic()
        sample = {"index": probe["index"], "cell_id": probe["cell_id"],
                  "paraphrase_index": probe["paraphrase_index"],
                  "repetition_index": probe.get("repetition_index"),
                  "started_at": _now(), "finished_at": None, "latency_ms": None,
                  "status_code": None, "category": "error", "normalized": None,
                  "raw": "", "extra_text": False, "finish_reason": None, "truncated": False,
                  "returned_model": None, "response_id": None, "usage": dict.fromkeys(USAGE_KEYS), "error": None,
                  "reasoning_observed": False}
        protocol = run["protocol"]
        request_body = {"model": run["endpoint"]["model"], "stream": False,
                        "temperature": protocol["temperature"], "top_p": protocol["top_p"],
                        protocol["token_field"]: protocol["max_tokens"],
                        "messages": [{"role": "system", "content": probe["system_prompt"]},
                                     {"role": "user", "content": probe["user_prompt"]}],
                        **copy.deepcopy(protocol["extra_body"])}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if runtime.key:
            headers["Authorization"] = "Bearer " + runtime.key
        async def receive():
            # Build an independent Request: a shared client's cookie jar must never
            # become hidden cross-sample state after an upstream Set-Cookie response.
            request = httpx.Request("POST", run["endpoint"]["url"], json=request_body, headers=headers)
            response = await client.send(request, stream=True)
            try:
                sample["status_code"] = response.status_code
                if not 200 <= response.status_code < 300:
                    sample["error"] = f"HTTP（超文本传输协议状态）{response.status_code}；未重试、未调整参数"
                else:
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                            sample["error"] = "响应超过 1 MiB（兆字节）上限，已关闭响应"
                            break
                        chunks.extend(chunk)
                    if sample["error"] is None:
                        self._decode_response(sample, json.loads(bytes(chunks)))
            finally:
                await response.aclose()
        try:
            # A total timeout bounds trickle responses, including on Python 3.9.
            await asyncio.wait_for(receive(), timeout=protocol["timeout_seconds"])
        except asyncio.CancelledError:
            sample["error"] = "请求在停止或错误收尾时取消；未自动补发"
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
            sample["error"] = "请求超时；未重试"
        except httpx.HTTPError:
            sample["error"] = "网络请求失败；未重试"
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
            sample["error"] = "响应不是有效的聊天补全结构；未重试"
        except Exception:
            sample["error"] = "响应处理失败；未重试"
        finally:
            sample["finished_at"] = _now()
            sample["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        if runtime.key and runtime.key in sample["raw"]:
            sample.update({"category": "invalid", "normalized": None, "extra_text": True})
        sample = _redact(sample, runtime.key)
        sample["raw"] = sample["raw"][:MAX_RAW_CHARS]
        return sample

    @staticmethod
    def _decode_response(sample, payload):
        if not isinstance(payload, dict):
            raise ValueError("response must be an object")
        sample["usage"] = _usage(payload)
        for name, field in (("model", "returned_model"), ("id", "response_id")):
            if isinstance(payload.get(name), str):
                sample[field] = payload[name][:512]
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("missing choices")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("missing assistant message")
        content = _content(message)
        refusal = message.get("refusal")
        sample["finish_reason"] = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
        sample["truncated"] = sample["finish_reason"] in {"length", "max_tokens", "max_output_tokens"} or len(content) > MAX_RAW_CHARS
        sample["reasoning_observed"] = bool(message.get("reasoning_content") or message.get("reasoning") or
                                             (sample["usage"]["reasoning_tokens"] or 0) > 0)
        sample["raw"] = content[:MAX_RAW_CHARS]
        if isinstance(refusal, str) and refusal.strip():
            sample["raw"] = (content or refusal)[:MAX_RAW_CHARS]
            sample["category"] = "invalid" if sample["truncated"] else "refusal"
            return
        normalized = normalize_answer(content[:MAX_RAW_CHARS], sample["cell_id"])
        sample.update({field: normalized.get(field) for field in ("normalized", "category", "extra_text")})
        if message.get("tool_calls") or message.get("function_call"):
            sample.update({"category": "invalid", "normalized": None, "extra_text": True})
        if sample["truncated"]:
            sample.update({"category": "invalid", "normalized": None})
            if not content.strip() and sample["reasoning_observed"]:
                sample["error"] = "正文为空：输出预算在推理阶段耗尽；请关闭思考后新建采集"

    def _append_sample(self, run, runtime, sample):
        with self.lock:
            fd = os.open(self._samples_path(run["id"]), os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "ab") as stream:
                stream.write(_json_bytes(sample) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            run["progress"]["done"] += 1
            run["progress"]["valid"] += sample["category"] == "valid"
            run["progress"]["errors"] += sample["category"] == "error"
            now = time.monotonic()
            if run["progress"]["done"] % 5 == 0 or now - runtime.last_persist >= 1:
                self._save_run(run)
                runtime.last_persist = now

    def _finalize_artifact(self, run, *, complete):
        samples, damaged = self._read_samples(run)
        run["progress"]["done"] = len(samples)
        run["progress"]["valid"] = sum(sample.get("category") == "valid" for sample in samples)
        run["progress"]["errors"] = sum(sample.get("category") == "error" for sample in samples)
        complete = bool(complete and not damaged and len(samples) == run["progress"]["total"])
        artifact = build_artifact(samples, run["protocol"], run["endpoint"],
                                  collected_at=run["started_at"] or run["created"], seed=run["seed"], complete=complete)
        artifact.setdefault("source", {}).update({"trusted_source": run.get("trusted_source", False),
                                                  "source_note": run.get("source_note", "")})
        if damaged:
            artifact.setdefault("diagnostics", {}).setdefault("warnings", []).append(run["storage_warning"])
        run["artifact"] = artifact
        run["diagnostics"] = copy.deepcopy(artifact.get("diagnostics", {}))

    def close(self, timeout=5):
        """Cancel queued/running work and release runtime secrets on server shutdown."""
        with self._condition:
            self._closed = True
            for run_id, run in list(self._runs.items()):
                if run["status"] in ACTIVE:
                    self.stop(run_id)
            self._condition.notify_all()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
