#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关黑盒压测工作台。启动后访问 http://127.0.0.1:8666。"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from quality_methodology import methodology_snapshot
import copy
import json
import math
import os
import traceback
import hashlib
import hmac
import secrets
from http.cookies import SimpleCookie
import threading
import time
import urllib.parse
import uuid
from collections import deque
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from app_paths import CODE_DIR, DATA_DIR
from app_version import APP_VERSION
from session_store import SessionStore
from history_store import HistoryStore, quality_index
import credential_store
from smart_route_load import CHAT_PATH, build_payload, do_request, fetch_models, fetch_models_detailed
from quality_test import QualityRun, QualityCase, load_suite, suite_manifest, run_quality_probe
from quality_report import build_quality_report, render_quality_markdown
import storage
from task_status import (
    STATES as TASK_STATES,
    TERMINAL_STATES as TASK_TERMINAL_STATES,
    apply_transition,
    normalize_terminal_state,
    status_label,
    transition_allowed,
    STATUS_LABELS as TASK_STATUS_LABELS,
)
from preflight import estimate_run, estimate_quality_run, estimate_routing_run, risk_level
import ops
import test_plan
import routing_ab
import routing_compare
import routing_corpus
import fingerprint_protocol
from fingerprint_service import FingerprintManager, discover_models as discover_fingerprint_models


def preflight_payload(cfg, *, kind="run"):
    """对一份「还没提交」的配置做预检，供创建抽屉在提交前展示风险卡片。

    和 GET /preflight 的区别：这里吃的是表单里的当前草稿，而不是上一次保存的配置，
    所以用户改了目标地址能立刻看到「目标疑似生产环境」的提示。
    """
    if kind == "quality":
        resources = [r for r in (cfg.get("resources") or []) if isinstance(r, dict)]
        cases = cfg.get("case_count")
        if cases is None:
            try:
                cases = len(load_suite(cfg.get("suite_path")))
            except Exception:
                cases = 0
        return estimate_quality_run(cfg, case_count=cases)
    if kind == "routing":
        return estimate_routing_run(cfg)
    steps = build_plan(cfg)[0] if cfg.get("mode") else None
    return estimate_run(cfg, steps)


def preflight_or_degrade(cfg, *, kind="run"):
    """预检失败时降级成可读原因，绝不因为它把创建流程卡死。

    校验也放在这里：草稿通常是不完整的（抽屉刚打开、还没填 URL），
    这类「还不够格预检」应该渲染成一条提示，而不是一个 400。
    """
    try:
        if kind == "routing":
            cfg = routing_compare.validate_config(cfg)
        elif kind != "quality":
            cfg = validate_config(cfg, require_secret=False)
        return preflight_payload(cfg, kind=kind)
    except (KeyError, TypeError, ValueError) as exc:
        return {"level": "ok", "findings": [], "requires_confirmation": False,
                "unavailable": f"配置尚不足以预检：{exc}"}


def require_risk_confirmation(cfg, body, *, kind="run"):
    """生产类目标必须由提交方显式确认，否则拒绝创建。

    界面上的确认弹窗只是体验层；这里才是真正的闸门——任何绕过页面的调用
    （脚本、curl、重放）都会在服务端被拦下。
    """
    payload = preflight_or_degrade(cfg, kind=kind)
    if not payload.get("requires_confirmation"):
        return None
    if body.get("risk_acknowledged") is True:
        return None
    target = payload.get("target") or {}
    return (f"目标 {target.get('host') or '未知主机'} 疑似生产环境（{target.get('reason') or '无法确认是否为测试环境'}）；"
            "确认要在该目标上执行后，请勾选风险确认再提交。")


PORT = int(os.environ.get("ROUTE_TEST_PORT", "8666"))
BASE_DIR = str(DATA_DIR)
TASKS_FILE = os.path.join(BASE_DIR, "tasks.json")
HISTORY_FILE = os.path.join(BASE_DIR, "task_index.json")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
LOOP = None
AUTH_LOCK = threading.RLock()
SESSIONS = SessionStore(os.path.join(BASE_DIR, "sessions.sqlite3"))
SESSION_COOKIE = "route_session"

# 运维基础件：并发闸门、事件日志、审计日志、方案模板。
# 这些状态在进程内共享，跨线程访问各自带锁，不依赖 Web 层。
EVENTS = ops.EventLog(os.path.join(BASE_DIR, "events.jsonl"))
AUDIT = ops.AuditLog(os.path.join(BASE_DIR, "audit.db"))
LIMITER = ops.ConcurrencyLimiter(1)
TEMPLATES = test_plan.TemplateStore(os.path.join(BASE_DIR, "test_templates.json"))

# Authentication/HTTP hardening is intentionally conservative for the local HTTP UI.
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 60.0
LOGIN_FAILURES = {}
LOGIN_RATE_LOCK = threading.RLock()

_KEYCHAIN_SERVICE = "route-load-workbench"


_ACCOUNTS_CACHE = {"signature": None, "accounts": []}


def _read_accounts():
    with AUTH_LOCK:
        try:
            stat = os.stat(ACCOUNTS_FILE)
            signature = (ACCOUNTS_FILE, stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if signature != _ACCOUNTS_CACHE["signature"]:
                with open(ACCOUNTS_FILE, encoding="utf-8") as source:
                    data = json.load(source)
                _ACCOUNTS_CACHE.update(signature=signature, accounts=data if isinstance(data, list) else [])
            return copy.deepcopy(_ACCOUNTS_CACHE["accounts"])
        except (OSError, ValueError, TypeError):
            _ACCOUNTS_CACHE.update(signature=None, accounts=[])
            return []


def _write_accounts(accounts):
    temp = f"{ACCOUNTS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with AUTH_LOCK:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, ACCOUNTS_FILE)
    try:
        os.chmod(ACCOUNTS_FILE, 0o600)
    except OSError:
        pass


def _password_hash(password, salt=None):
    password = str(password or "")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return salt.hex(), digest.hex()


def _check_password(password, account):
    try:
        salt = bytes.fromhex(str(account.get("salt")))
        expected = str(account.get("password_hash"))
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, 240_000).hex()
    return hmac.compare_digest(actual, expected)


def _public_account(account):
    return {"username": account.get("username"), "role": account.get("role", "user"), "created_at": account.get("created_at")}


def _current_user(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except Exception:
        return None
    token = cookie.get(SESSION_COOKIE)
    if not token:
        return None
    with AUTH_LOCK:
        username = SESSIONS.get(token.value)
        accounts = _read_accounts()
        account = next((x for x in accounts if x.get("username") == username and x.get("enabled", True)), None)
    return _public_account(account) if account else None


def _new_session(username):
    token = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        SESSIONS[token] = username
    return token


def _session_headers(token=None, clear=False):
    # Secure is opt-in because the bundled server normally runs on localhost over HTTP.
    # Deployments behind HTTPS can set ROUTE_TEST_SECURE_COOKIE=1.
    secure = "; Secure" if os.environ.get("ROUTE_TEST_SECURE_COOKIE", "").lower() in {"1", "true", "yes", "on"} else ""
    if clear:
        return {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"}
    return {"Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; Max-Age=604800; HttpOnly; SameSite=Strict{secure}"}


def _login_rate_key(handler):
    return str(getattr(handler, "client_address", ("unknown",))[0])


def _client_ip(handler):
    """审计记录里的来源地址：只记 IP，不记端口，避免把临时端口当线索。"""
    try:
        return str(handler.client_address[0])
    except (AttributeError, IndexError, TypeError):
        return "unknown"


def _login_rate_status(handler, now=None):
    now = time.monotonic() if now is None else now
    key = _login_rate_key(handler)
    with LOGIN_RATE_LOCK:
        failures = [stamp for stamp in LOGIN_FAILURES.get(key, []) if now - stamp < LOGIN_RATE_WINDOW_SECONDS]
        if failures:
            LOGIN_FAILURES[key] = failures
        else:
            LOGIN_FAILURES.pop(key, None)
        if len(failures) < LOGIN_RATE_LIMIT:
            return None
        retry_after = max(1, int(LOGIN_RATE_WINDOW_SECONDS - (now - failures[0]) + 0.999))
        return retry_after


def _record_login_failure(handler, now=None):
    now = time.monotonic() if now is None else now
    key = _login_rate_key(handler)
    with LOGIN_RATE_LOCK:
        failures = [stamp for stamp in LOGIN_FAILURES.get(key, []) if now - stamp < LOGIN_RATE_WINDOW_SECONDS]
        failures.append(now)
        LOGIN_FAILURES[key] = failures[-LOGIN_RATE_LIMIT:]


def _clear_login_failures(handler):
    with LOGIN_RATE_LOCK:
        LOGIN_FAILURES.pop(_login_rate_key(handler), None)


def _auth_setup_required():
    return len(_read_accounts()) == 0

# Compatibility names used by existing creation workflows.
_keychain_account = credential_store.account
_keychain_available = credential_store.available
_load_api_key = credential_store.load
_save_api_key = credential_store.save
_delete_api_key = credential_store.delete

def _api_key_hint(key):
    key = str(key or "")
    return f"{key[:4]}••••{key[-4:]}" if len(key) >= 8 else ("已保存密钥" if key else "")

COUNTER_KEYS = ("ok", "r429", "r5xx", "r4xx", "timeout", "network", "protocol")


_SECRET_FIELD_NAMES = {
    "api_key", "apikey", "authorization", "password", "passwd", "secret",
    "token", "access_token", "refresh_token", "credential", "credentials", "x_api_key",
}


def _is_secret_field(name):
    """判断字段名是否可能承载凭据，避免未知字段绕过脱敏。"""
    normalized = str(name).strip().lower().replace("-", "_")
    return normalized in _SECRET_FIELD_NAMES or normalized.endswith("_token")


def _public_task_config(cfg):
    """只暴露当前 UI（用户界面）支持的任务配置，未知字段不进入持久化/接口摘要。"""
    allowed = {
        "name", "url", "model", "mode", "rpm", "ramps", "iterations", "duration",
        "concurrency", "connect_timeout", "read_timeout", "write_timeout", "profile",
        "stream", "capture_raw", "include_usage", "http2", "insecure",
    }
    return {key: _without_secret_fields(value) for key, value in cfg.items() if key in allowed}


def _without_secret_fields(value):
    """递归移除可能承载凭据的字段，避免嵌套字段绕过脱敏。"""
    if isinstance(value, dict):
        return {
            key: _without_secret_fields(item)
            for key, item in value.items()
            if not _is_secret_field(key)
        }
    if isinstance(value, list):
        return [_without_secret_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_secret_fields(item) for item in value)
    return value


def _redact_secret(value, secret):
    """在原始输入/输出中替换已知密钥；不改变其他数据结构。"""
    secret = str(secret or "")
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[已隐藏]")
    if isinstance(value, dict):
        return {key: _redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret(item, secret) for item in value)
    return value


def _secret_values(value):
    """递归收集配置中的密钥，用于异常文本和原始数据的统一替换。"""
    if isinstance(value, dict):
        values = []
        for key, item in value.items():
            if _is_secret_field(key) and item:
                values.append(str(item))
            else:
                values.extend(_secret_values(item))
        return values
    if isinstance(value, (list, tuple)):
        values = []
        for item in value:
            values.extend(_secret_values(item))
        return values
    return []


def _redact_secrets(value, secrets):
    """按长度从长到短替换多个密钥，避免短密钥先命中造成残留。"""
    result = value
    for secret in sorted({str(item) for item in secrets if str(item)}, key=len, reverse=True):
        result = _redact_secret(result, secret)
    return result


def _clear_secret_fields(value):
    """递归清空内存对象中的凭据，保留结构便于状态展示。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_field(key):
                value[key] = ""
            else:
                _clear_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _clear_secret_fields(item)
    return value


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    return values[max(0, min(len(values) - 1, int(p * len(values)) - 1))]


def strip_url(url):
    if not url:
        return None
    base = str(url).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        base = base[:-3]
    return base + CHAT_PATH


def safe_float(value, field, minimum=None, maximum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是数字")
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数字")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field}不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} 不能大于 {maximum}")
    return result


def safe_int(value, field, minimum=None, maximum=None):
    result = safe_float(value, field, minimum, maximum)
    if not result.is_integer():
        raise ValueError(f"{field} 必须是整数")
    return int(result)


def parse_bool(value, field, default=False):
    """把 HTTP（超文本传输协议）输入中的布尔值按统一规则解析。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off", ""):
        return False
    raise ValueError(f"{field} 必须是布尔值")


def validate_config(raw, require_secret=True):
    if not isinstance(raw, dict):
        raise ValueError("请求体必须是 JSON 对象")
    cfg = dict(raw)
    cfg["url"] = str(cfg.get("url") or "").strip()
    cfg["api_key"] = str(cfg.get("api_key") or "").strip()
    if not cfg["api_key"]:
        cfg["api_key"] = _load_api_key(cfg.get("url"))
    cfg["model"] = str(cfg.get("model") or "").strip()
    cfg["name"] = str(cfg.get("name") or "").strip()[:80]
    cfg["mode"] = str(cfg.get("mode") or "smoke")
    if not cfg["url"]:
        raise ValueError("请填写网关 URL")
    parsed = urllib.parse.urlparse(cfg["url"])
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("网关 URL 必须是有效的 http 或 https 地址")
    if require_secret and not cfg["api_key"]:
        raise ValueError("请填写 API Key")
    if not cfg["model"]:
        raise ValueError("请填写或选择模型")
    if cfg["mode"] not in ("smoke", "ramp", "soak"):
        raise ValueError("测试模式无效")
    cfg["rpm"] = safe_float(cfg.get("rpm", 600), "计划调度 RPM", 0.01, 600000)
    cfg["concurrency"] = safe_int(cfg.get("concurrency", 20), "并发上限", 1, 2000)
    cfg["iterations"] = safe_int(cfg.get("iterations", 50), "请求条数", 1, 10000000)
    cfg["duration"] = safe_int(cfg.get("duration", 60), "持续时间", 1, 86400)
    cfg["connect_timeout"] = safe_float(cfg.get("connect_timeout", 10), "连接超时", 0.1, 3600)
    cfg["read_timeout"] = safe_float(cfg.get("read_timeout", 60), "读取超时", 0.1, 3600)
    cfg["write_timeout"] = safe_float(cfg.get("write_timeout", 30), "写入超时", 0.1, 3600)
    cfg["profile"] = str(cfg.get("profile") or "smoke")
    if cfg["profile"] not in ("smoke", "short", "long"):
        raise ValueError("请求内容类型无效")
    cfg["stream"] = parse_bool(cfg.get("stream", True), "stream", True)
    cfg["capture_raw"] = parse_bool(cfg.get("capture_raw", False), "记录原始数据开关", False)
    # 默认开启：缓存命中率的口径就是 usage.cached_tokens，不请求 usage 就永远算不出来。
    # 个别网关不认 stream_options.include_usage 会直接 400，执行器收到 4xx 会自动降级重试，
    # 兼容性差异不会被记成请求失败。
    cfg["include_usage"] = parse_bool(cfg.get("include_usage", True), "请求用量统计开关", True)
    cfg["http2"] = parse_bool(cfg.get("http2", False), "HTTP/2 开关", False)
    cfg["insecure"] = parse_bool(cfg.get("insecure", False), "跳过 TLS 证书校验开关", False)
    ramps = []
    if cfg["mode"] == "ramp":
        raw_ramps = cfg.get("ramps", "")
        try:
            ramps = [float(x.strip()) for x in str(raw_ramps).split(",") if x.strip()]
        except ValueError:
            raise ValueError("阶梯 RPM 必须是逗号分隔的正数")
        if not ramps or any(not math.isfinite(x) or x <= 0 or x > 600000 for x in ramps):
            raise ValueError("阶梯 RPM 必须是 0 到 600000 之间的正数")
        if len(ramps) > 50:
            raise ValueError("阶梯数量不能超过 50 档")
    cfg["ramps"] = ",".join(f"{x:g}" for x in ramps) if ramps else str(cfg.get("ramps") or "")
    allowed = {
        "name", "url", "api_key", "model", "mode", "rpm", "ramps", "iterations", "duration",
        "concurrency", "connect_timeout", "read_timeout", "write_timeout", "profile",
        "stream", "capture_raw", "include_usage", "http2", "insecure",
    }
    return {key: cfg[key] for key in allowed if key in cfg}


def build_plan(cfg):
    steps = []
    if cfg["mode"] == "ramp":
        rpms = [float(x) for x in cfg["ramps"].split(",")]
        for index, rpm in enumerate(rpms, 1):
            count = int(round(rpm / 60 * cfg["duration"]))
            steps.append({"index": index, "rpm": rpm, "duration": cfg["duration"], "count": count})
    elif cfg["mode"] == "soak":
        count = int(round(cfg["rpm"] / 60 * cfg["duration"]))
        steps.append({"index": 1, "rpm": cfg["rpm"], "duration": cfg["duration"], "count": count})
    else:
        duration = cfg["iterations"] / (cfg["rpm"] / 60)
        steps.append({"index": 1, "rpm": cfg["rpm"], "duration": duration, "count": cfg["iterations"]})
    total = sum(step["count"] for step in steps)
    if total <= 0:
        raise ValueError("计划请求数必须至少为 1，请提高计划调度速率或持续时间")
    if total > 10000000:
        raise ValueError("计划请求数超过 1000 万，请降低 RPM 或持续时间")
    return steps, total


def public_config(cfg):
    return _public_task_config(cfg)


class ArtifactMixin:
    """结果产物的容量与校验值追踪。

    为什么放在任务对象上而不是集中式数据库：
    当前持久化仍是文件，任务对象是唯一知道“哪个文件属于我”的地方。
    把 artifact 元信息挂在任务上，删除任务时就能可靠地一并删除结果文件，
    也避免为清理逻辑再引入一套映射关系。
    """

    artifact_path = None
    artifact_bytes = None
    artifact_sha256 = None
    artifact_checked_at = None
    artifact_state = "unknown"

    def refresh_artifact(self, verify=False):
        """刷新结果文件的字节数与校验值。

        `verify=False` 只更新大小（执行结束后即可调用，成本低）。
        `verify=True` 重新计算 sha256（恢复历史或用户主动核对时调用）。
        返回 artifact 摘要字典。
        """
        path = self.artifact_path or getattr(self, "out_path", None)
        if not path or not os.path.exists(path):
            self.artifact_state = "missing" if path else "none"
            self.artifact_bytes = None
            if verify:
                self.artifact_sha256 = None
            return self.artifact_snapshot()
        try:
            size = os.path.getsize(path)
        except OSError:
            self.artifact_state = "missing"
            return self.artifact_snapshot()
        self.artifact_bytes = size
        if verify or self.artifact_sha256 is None:
            _, digest = storage.file_fingerprint(path)
            self.artifact_sha256 = digest
        self.artifact_checked_at = time.time()
        if self.artifact_state in ("unknown", "missing"):
            self.artifact_state = "present"
        return self.artifact_snapshot()

    def mark_artifact_expired(self, reason="结果文件缺失或校验不一致"):
        self.artifact_state = "expired"
        self.artifact_expired_reason = reason

    def artifact_snapshot(self):
        return {
            "name": os.path.basename(self.artifact_path) if self.artifact_path else None,
            "bytes": self.artifact_bytes,
            "sha256": self.artifact_sha256,
            "state": self.artifact_state,
            "checked_at": self.artifact_checked_at,
            "expired_reason": getattr(self, "artifact_expired_reason", None),
        }

    def delete_artifact(self):
        """删除本任务的结果文件。返回是否已不再存在。"""
        path = self.artifact_path or getattr(self, "out_path", None)
        if not path:
            return True
        if storage.unlink_artifact(path):
            self.artifact_state = "deleted"
            self.artifact_bytes = 0
            return True
        return False


class Task(ArtifactMixin):

    def __init__(self, config, steps, total):
        self.id = uuid.uuid4().hex[:12]
        self.name = config.get("name") or f"测试-{time.strftime('%m%d-%H%M%S')}-{self.id[-4:]}"
        self.config = dict(config)
        self.public_config = public_config(config)
        self.steps = steps
        self.status = "waiting"
        self.error = None
        self.created = time.time()
        self.started_at = None
        self.finished_at = None
        self.stopped_at = None
        self.total = total
        self.scheduled = 0
        self.started = 0
        self.done = 0
        self.dropped = 0
        self.queue_depth = 0
        self.schedule_lag_ms = deque(maxlen=10000)
        self.counters = {key: 0 for key in COUNTER_KEYS}
        self.ttft = deque(maxlen=200000)
        self.total_ms = deque(maxlen=200000)
        # 缓存命中统计：分母只算「上游确实返回了 cached_tokens」的请求。
        # 无 usage 的请求单独计数，避免把「网关不回 usage」误读成未命中。
        self.cache_stat = {"eligible": 0, "hits": 0, "no_usage": 0}
        self.recent = deque(maxlen=100)
        self.timeline = deque(maxlen=900)
        self.current_step = 1
        self.current_target_rpm = steps[0]["rpm"] if steps else 0
        self.schedule_step = 1
        self.schedule_target_rpm = self.current_target_rpm
        self.out_path = None
        self.write_error = None
        self.lock = threading.RLock()
        self._recent_times = deque(maxlen=200000)
        self._last_result_at = None
        self._prev = None
        self._stopped = threading.Event()
        self._stop_event = None
        self._sampler = None
        self._out = None
        # 从历史索引或旧结果文件恢复的任务只读，不再尝试恢复执行。
        self.history_only = False
        self._history_snapshot = None
        # 对照实验的内部组任务不作为顶层记录展示，但仍可通过组详情打开。
        self.parent_comparison_id = None
        self.parent_group_id = None

    def total_requests_failed(self):
        """已收到结果中，非成功的请求数（用于终态判定，不等同于「未完成」）。"""
        return sum(value for key, value in self.counters.items() if key != "ok")

    def _mode_label(self):
        return {"smoke": "冒烟验证", "ramp": "阶梯加压", "soak": "稳定性长跑"}.get(self.config["mode"], self.config["mode"])

    def _elapsed(self):
        if not self.started_at:
            return 0
        return max(0, (self.finished_at or time.time()) - self.started_at)

    def _live_rpm(self):
        if self.status != "running":
            return 0.0
        now = time.monotonic()
        while self._recent_times and now - self._recent_times[0] > 10:
            self._recent_times.popleft()
        return len(self._recent_times) * 6.0 if self._last_result_at is not None else 0.0

    def _phase(self):
        if self.status == "finished":
            return f"已完成全部 {self.total} 条请求"
        if self.status == "stopped":
            return f"已停止 · 完成 {self.done} / {self.total} 条"
        if self.status == "error":
            return f"执行失败 · 完成 {self.done} / {self.total} 条"
        if self.status == "waiting":
            return "等待前序任务结束"
        if self.config["mode"] == "ramp":
            return f"第 {self.current_step}/{len(self.steps)} 档 · 计划调度速率 {self.current_target_rpm:.0f} RPM"
        if self.config["mode"] == "soak":
            return f"计划调度时长 {self.config['duration']} 秒 · 当前仍在执行"
        return f"已完成 {self.done} / {self.total} 条"

    def snap(self):
        if self.history_only and self._history_snapshot is not None:
            # 历史任务只读展示：不重新计算运行时指标，也不会被误认为仍在执行。
            snapshot = dict(self._history_snapshot)
            snapshot["config"] = self.public_config
            snapshot["out_path"] = self.out_path
            snapshot["history_only"] = True
            return snapshot
        with self.lock:
            elapsed = self._elapsed()
            success = self.counters["ok"] * 100 / self.done if self.done else None
            lag_values = list(self.schedule_lag_ms)
            total_values = list(self.total_ms)
            average_total_ms = sum(total_values) / len(total_values) if total_values else None
            planned_duration = sum(step["duration"] for step in self.steps)
            capacity_rpm = (self.config["concurrency"] * 60000 / average_total_ms) if average_total_ms else None
            cache = self.cache_stat
            cache_rate = (cache["hits"] * 100 / cache["eligible"]) if cache["eligible"] else None
            return {
                "id": self.id, "name": self.name, "status": self.status, "error": self.error,
                "mode": self._mode_label(), "model": self.public_config.get("model"), "concurrency": self.config.get("concurrency"),
                "target_url": self.public_config.get("url"), "config": self.public_config,
                "total": self.total, "scheduled": self.scheduled, "started": self.started,
                "done": self.done, "progress": min(1, self.done / self.total) if self.total else 0,
                "remaining": max(0, self.total - self.done),
                "pending": max(0, self.scheduled - self.started),
                "inflight": max(0, self.started - self.done),
                "dropped": self.dropped, "queue_depth": self.queue_depth,
                "cur_step": self.current_step, "target_rpm": self.current_target_rpm,
                "schedule_step": self.schedule_step, "schedule_target_rpm": self.schedule_target_rpm,
                "counters": dict(self.counters), "success_pct": success,
                "ttft": {"n": len(self.ttft), "p50": percentile(list(self.ttft), .5), "p95": percentile(list(self.ttft), .95)},
                "total_ms": {"n": len(total_values), "p50": percentile(total_values, .5), "p95": percentile(total_values, .95), "avg": average_total_ms},
                "rpm": {"live": self._live_rpm(), "avg": self.done * 60 / elapsed if elapsed else None,
                        "capacity_estimate": capacity_rpm},
                "schedule_lag": {"p50": percentile(lag_values, .5), "p95": percentile(lag_values, .95)},
                "cache": {"eligible": cache["eligible"], "hits": cache["hits"],
                          "no_usage": cache["no_usage"], "rate_pct": cache_rate,
                          "basis": "usage.cached_tokens>0"},
                "phase": self._phase(), "recent": list(reversed(self.recent)),
                "timeline": list(self.timeline), "out_path": self.out_path,
                "write_error": self.write_error, "created": self.created,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "stopped_at": self.stopped_at, "elapsed": elapsed,
                "planned_duration": planned_duration, "planned_count": self.total,
                "history_only": self.history_only,
                "parent_comparison_id": self.parent_comparison_id,
                "parent_group_id": self.parent_group_id,
                "artifact": self.artifact_snapshot(),
                "status_label": status_label(self.status),
            }

    def open_output(self):
        self.out_path = os.path.join(BASE_DIR, f"run_{int(time.time())}_{self.id}.jsonl")
        # 结果文件即是本任务的 artifact：路径与容量/校验值一并在任务上登记，
        # 删除任务时才能可靠地连带删除。
        self.artifact_path = self.out_path
        self.artifact_state = "present"
        # 原始记录可能包含用户输入/模型输出，只允许当前用户读写。
        fd = os.open(self.out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(self.out_path, 0o600)
            self._out = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        except Exception:
            os.close(fd)
            raise

    def close_output(self):
        if self._out:
            self._out.close()
            self._out = None
        # 关闭后立刻登记最终容量；校验值等用户主动核对时再算，避免长跑收尾卡顿。
        self.refresh_artifact(verify=False)

    def record(self, result):
        """先落盘，再更新内存统计，避免页面统计领先于结果文件。"""
        status = result.get("status_code") or 0
        error = result.get("error_type")
        completed = bool(result.get("completed"))
        if 200 <= status < 300 and completed:
            category, label, css = "ok", "成功", "ok"
        elif error == "TIMEOUT" or status == 408:
            category, label, css = "timeout", "超时", "timeout"
        elif error == "NETWORK" or (status == 0 and error != "TIMEOUT"):
            category, label, css = "network", "网络错误", "network"
        elif error == "PROTOCOL" or (200 <= status < 300 and not completed):
            category, label, css = "protocol", "响应不完整", "protocol"
        elif status == 429 or error == "HTTP_429":
            category, label, css = "r429", "限流", "r429"
        elif 500 <= status < 600 or error == "HTTP_5XX":
            category, label, css = "r5xx", "服务端错误", "r5xx"
        else:
            category, label, css = "r4xx", "客户端错误", "r4xx"
        secret = self.config.get("api_key")
        clean = {k: _redact_secret(result.get(k), secret) for k in (
            "schema_version", "ts", "item_id", "mode", "profile", "step_index", "grade",
            "status_code", "completed", "error_type", "error_detail", "ttft_ms", "total_ms",
            "schedule_lag_ms", "reason",
            "usage_seen", "usage_source", "usage_level", "prompt_tokens", "cached_tokens",
            "completion_tokens", "total_tokens", "cache_hit")}
        if self.config.get("capture_raw"):
            clean["raw_input"] = _redact_secret(result.get("raw_input"), secret)
            clean["raw_output"] = _redact_secret(result.get("raw_output"), secret)
        line = json.dumps(clean, ensure_ascii=False) + "\n"
        try:
            with self.lock:
                if not self._out:
                    raise OSError("结果文件尚未打开")
                self._out.write(line)
                self._out.flush()
                self.done += 1
                self.counters[category] += 1
                now = time.monotonic()
                self._recent_times.append(now)
                self._last_result_at = now
                if result.get("ttft_ms") is not None:
                    self.ttft.append(result["ttft_ms"])
                if result.get("total_ms") is not None:
                    self.total_ms.append(result["total_ms"])
                cached = result.get("cached_tokens")
                if cached is None:
                    self.cache_stat["no_usage"] += 1
                else:
                    self.cache_stat["eligible"] += 1
                    if cached > 0:
                        self.cache_stat["hits"] += 1
                self.recent.append({
                    "id": result.get("item_id"), "step": result.get("step_index"),
                    "status": status, "cat": category, "cls": css, "label": label,
                    "ttft": result.get("ttft_ms"), "total": result.get("total_ms"),
                    "reason": result.get("reason"),
                })
        except Exception as exc:
            self.write_error = f"结果写入失败：{exc}"
            self.error = self.write_error
            self._stopped.set()
            raise

    async def sample(self):
        with self.lock:
            self._prev = dict(self.counters)
        while not self._stopped.is_set() and self.status in ("running", "stopping"):
            await asyncio.sleep(1)
            with self.lock:
                current = dict(self.counters)
                delta = {k: current[k] - self._prev[k] for k in COUNTER_KEYS}
                self._prev = current
                self.timeline.append({
                    "t": int(time.time() * 1000), **delta,
                    "rpm": sum(delta.values()) * 60,
                    "target_rpm": self.current_target_rpm,
                    "step": self.current_step,
                    "queue": self.queue_depth,
                })



def validate_comparison_config(raw):
    """校验一个对照实验；公共配置只保存非密钥字段，组配置不含 API Key。"""
    if not isinstance(raw, dict):
        raise ValueError("对照实验请求体必须是 JSON 对象")
    shared_value = raw.get("shared_config") or {}
    if not isinstance(shared_value, dict):
        raise ValueError("对照实验 shared_config 必须是 JSON 对象")
    shared = dict(shared_value)
    shared["name"] = str(raw.get("name") or shared.get("name") or "对照实验").strip()[:80]
    base = validate_config({**shared, "mode": "smoke", "iterations": 1, "rpm": 600}, require_secret=True)
    groups = raw.get("groups") or []
    if not isinstance(groups, list) or not 2 <= len(groups) <= 4:
        raise ValueError("对照实验必须包含 2 到 4 个实验组")
    normalized = []
    group_ids = set()
    baseline_count = 0
    for index, item in enumerate(groups, 1):
        if not isinstance(item, dict):
            raise ValueError("实验组配置无效")
        concurrency = safe_int(item.get("concurrency", 20), f"实验组 {index} 并发上限", 1, 2000)
        rpm = safe_float(item.get("rpm", 600), f"实验组 {index} 计划调度 RPM", 0.01, 600000)
        iterations = safe_int(item.get("iterations", 20), f"实验组 {index} 请求条数", 1, 10000000)
        group_id = str(item.get("id") or f"group-{index}").strip()
        if group_id in group_ids:
            raise ValueError(f"实验组 id 重复：{group_id}")
        group_ids.add(group_id)
        role = str(item.get("role") or ("baseline" if index == 1 else "comparison")).strip().lower()
        if role not in ("baseline", "comparison"):
            raise ValueError(f"实验组 {index} 角色必须是 baseline（基准组）或 comparison（对比组）")
        if role == "baseline":
            baseline_count += 1
        normalized.append({
            "id": group_id,
            "name": str(item.get("name") or ("基准组" if role == "baseline" else f"对比组 {index - 1}")).strip()[:60],
            "role": role,
            "concurrency": concurrency,
            "rpm": rpm,
            "iterations": iterations,
        })
    if baseline_count != 1:
        raise ValueError("对照实验必须且只能有 1 个基准组")
    # 执行顺序固定为基准组优先，保证每个对比组都有同一个比较基线。
    normalized.sort(key=lambda item: 0 if item["role"] == "baseline" else 1)
    execution_policy = str(raw.get("execution_policy") or "serial").strip().lower()
    if execution_policy != "serial":
        raise ValueError("对照实验当前仅支持串行执行")
    if "seed" in raw:
        raise ValueError("对照实验暂不支持随机种子；请求样本按固定索引轮换")
    return {
        "name": base["name"],
        "shared_config": base,
        "groups": normalized,
        "execution_policy": execution_policy,
    }


class ComparisonRun:
    def __init__(self, config):
        self.id = uuid.uuid4().hex[:12]
        self.name = config["name"]
        self.config = config
        self.status = "draft"
        self.created = time.time()
        self.started_at = None
        self.finished_at = None
        self.group_results = []
        self.lock = threading.RLock()
        self.history_only = False
        self._history_snapshot = None
        self.current_group_id = None
        self.current_group_name = None

    def snap(self):
        if self.history_only and self._history_snapshot is not None:
            return dict(self._history_snapshot)
        with self.lock:
            groups = list(self.group_results)
            status = self.status
            started_at = self.started_at
            finished_at = self.finished_at
            config = dict(self.config)

        def metric(summary, *path):
            value = summary
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            return value

        def rounded(value, digits=2):
            return round(value, digits) if isinstance(value, (int, float)) else None

        configured_groups = config.get("groups") or []
        configured_by_id = {str(item.get("id")): item for item in configured_groups if isinstance(item, dict)}
        # 组配置先于执行结果存在。报告和详情标签必须从创建时就显示所有实验组，
        # 否则执行到一半时用户会误以为尚未创建对比组，或者把内部任务看成多个顶层任务。
        result_by_id = {str(item.get("id")): item for item in groups if isinstance(item, dict)}
        merged_groups = []
        for cfg in configured_groups:
            if not isinstance(cfg, dict):
                continue
            result = result_by_id.get(str(cfg.get("id")), {})
            merged = dict(cfg)
            merged.update(result)
            merged["id"] = cfg.get("id")
            merged.setdefault("name", cfg.get("name"))
            merged.setdefault("role", cfg.get("role"))
            merged.setdefault("concurrency", cfg.get("concurrency"))
            merged.setdefault("rpm", cfg.get("rpm"))
            merged.setdefault("iterations", cfg.get("iterations"))
            merged.setdefault("status", "waiting")
            merged_groups.append(merged)
        # 兼容异常或旧历史数据：配置中没有的结果仍保留，不能静默丢失证据。
        configured_ids = {str(item.get("id")) for item in configured_groups if isinstance(item, dict)}
        merged_groups.extend(item for item in groups if str(item.get("id")) not in configured_ids)
        groups = merged_groups
        baseline_cfg = next((item for item in configured_groups if item.get("role") == "baseline"), configured_groups[0] if configured_groups else {})
        baseline_id = baseline_cfg.get("id")
        baseline_result = next((item for item in groups if item.get("id") == baseline_id), None)
        base = (baseline_result or {}).get("summary") or {}
        base_success = metric(base, "success_pct")
        base_completed_rpm = metric(base, "rpm", "avg")
        base_p95 = metric(base, "total_ms", "p95")

        # 可比性必须先于结论：如果一个实验组同时改变并发与计划调度速率，
        # 观察到的差异无法单独归因于某一个压力变量。请求条数只影响证据量，
        # 不把它当成服务压力维度，但会在报告中单独提示样本量不同。
        baseline_pressure = {
            "concurrency": baseline_cfg.get("concurrency"),
            "rpm": baseline_cfg.get("rpm"),
        }
        pressure_labels = {"concurrency": "并发上限", "rpm": "计划调度速率"}
        changed_dimensions = set()
        sample_size_changed = False
        for item in configured_groups:
            if item.get("id") == baseline_id:
                continue
            for key in ("concurrency", "rpm"):
                if item.get(key) != baseline_pressure.get(key):
                    changed_dimensions.add(key)
            if item.get("iterations") != baseline_cfg.get("iterations"):
                sample_size_changed = True
        if len(changed_dimensions) == 0:
            comparability_level, comparability_label = "not_attributable", "不可归因"
            comparability_note = "对比组没有改变并发或计划调度速率，当前结果只能作为重复观测，不能解释压力变量影响。"
        elif len(changed_dimensions) == 1 and not sample_size_changed:
            comparability_level, comparability_label = "comparable", "可比"
            comparability_note = f"对比组仅改变{pressure_labels[next(iter(changed_dimensions))]}；差异可做该变量的方向性比较。"
        elif len(changed_dimensions) == 1:
            comparability_level, comparability_label = "partial", "部分可比"
            comparability_note = f"压力变量仅改变{pressure_labels[next(iter(changed_dimensions))]}，但请求条数也不同；方向可参考，强度不宜直接归因。"
        else:
            comparability_level, comparability_label = "not_attributable", "不可单独归因"
            comparability_note = "多个核心压力变量同时变化，当前差异不能单独归因于并发或计划调度速率。"
        if sample_size_changed and len(changed_dimensions) != 1:
            comparability_note += " 部分组请求条数不同，成功率与 p95 的证据量也不完全一致。"
        comparability = {
            "level": comparability_level,
            "label": comparability_label,
            "changed_dimensions": [pressure_labels[key] for key in ("concurrency", "rpm") if key in changed_dimensions],
            "sample_size_changed": sample_size_changed,
            "note": comparability_note,
        }

        rows = []
        for item in groups:
            summary = item.get("summary") or {}
            cfg = configured_by_id.get(str(item.get("id")), item)
            counters = summary.get("counters") or {}
            success = metric(summary, "success_pct")
            completed_rpm = metric(summary, "rpm", "avg")
            p50 = metric(summary, "total_ms", "p50")
            p95 = metric(summary, "total_ms", "p95")
            lag_p95 = metric(summary, "schedule_lag", "p95")
            failures = sum(int(counters.get(key) or 0) for key in ("timeout", "network", "protocol", "r429", "r5xx", "r4xx"))
            planned_iterations = cfg.get("iterations")
            planned_rpm = cfg.get("rpm")
            done = summary.get("done")
            completion_ratio = (done * 100 / planned_iterations) if isinstance(done, (int, float)) and planned_iterations else None
            dispatch_fulfilment = (completed_rpm * 100 / planned_rpm) if isinstance(completed_rpm, (int, float)) and planned_rpm else None
            row = {
                "id": item.get("id"),
                "name": item.get("name") or cfg.get("name"),
                "role": item.get("role") or cfg.get("role") or ("baseline" if item.get("id") == baseline_id else "comparison"),
                "status": item.get("status") or (summary.get("status") if isinstance(summary, dict) else None),
                "concurrency": item.get("concurrency", cfg.get("concurrency")),
                "planned_rpm": item.get("rpm", planned_rpm),
                "planned_iterations": planned_iterations,
                "success_pct": rounded(success, 2),
                "completed_rpm": rounded(completed_rpm, 2),
                "avg_completed_rpm": rounded(completed_rpm, 2),
                "completion_ratio_pct": rounded(completion_ratio, 2),
                "dispatch_fulfilment_pct": rounded(dispatch_fulfilment, 2),
                "total_p50_ms": rounded(p50, 1),
                "total_p95_ms": rounded(p95, 1),
                "schedule_lag_p95_ms": rounded(lag_p95, 1),
                "completed_requests": done,
                "failure_count": failures,
                "delta_success_pct": rounded(success - base_success, 2) if success is not None and base_success is not None and item.get("id") != baseline_id else None,
                "delta_completed_rpm": rounded(completed_rpm - base_completed_rpm, 2) if completed_rpm is not None and base_completed_rpm is not None and item.get("id") != baseline_id else None,
                "delta_avg_completed_rpm": rounded(completed_rpm - base_completed_rpm, 2) if completed_rpm is not None and base_completed_rpm is not None and item.get("id") != baseline_id else None,
                "delta_total_p95_ms": rounded(p95 - base_p95, 1) if p95 is not None and base_p95 is not None and item.get("id") != baseline_id else None,
            }
            if success is None or completed_rpm is None:
                row["capacity_signal"] = "insufficient"
            elif (dispatch_fulfilment is not None and dispatch_fulfilment < 80) or (success is not None and success < 99) or (lag_p95 is not None and lag_p95 > 1000):
                row["capacity_signal"] = "degraded"
            else:
                row["capacity_signal"] = "stable"
            rows.append(row)

        complete_rows = [row for row in rows if row.get("completed_requests") is not None and row.get("status") in ("finished", "finished_with_errors")]
        ordered = sorted((row for row in complete_rows if row.get("planned_rpm") is not None), key=lambda row: float(row["planned_rpm"]))
        capacity_inflection = None
        for previous, current in zip(ordered, ordered[1:]):
            if float(current["planned_rpm"]) <= float(previous["planned_rpm"]):
                continue
            throughput_flat = current.get("completed_rpm") is not None and previous.get("completed_rpm") is not None and current["completed_rpm"] <= previous["completed_rpm"] * .92
            success_drop = current.get("success_pct") is not None and previous.get("success_pct") is not None and current["success_pct"] <= previous["success_pct"] - 1
            p95_jump = current.get("total_p95_ms") is not None and previous.get("total_p95_ms") is not None and current["total_p95_ms"] >= max(100, previous["total_p95_ms"] * 1.2)
            if throughput_flat or success_drop or p95_jump:
                reasons = []
                if throughput_flat:
                    reasons.append("实际完成吞吐没有随计划压力增长")
                if success_drop:
                    reasons.append("成功率下降")
                if p95_jump:
                    reasons.append("总耗时 p95 上升")
                capacity_inflection = {"group_id": current.get("id"), "group_name": current.get("name"), "planned_rpm": current.get("planned_rpm"), "reasons": reasons, "previous_group_id": previous.get("id"), "previous_group_name": previous.get("name")}
                break

        signals = []
        for row in rows:
            if row.get("id") == baseline_id:
                continue
            if row["delta_success_pct"] is not None and row["delta_success_pct"] <= -1:
                signals.append({"level": "risk", "title": f"{row['name']} 成功率下降 {abs(row['delta_success_pct']):g} 个百分点", "detail": "压力提升后，完整响应占比低于基准组；应结合错误分布判断是否为限流、超时或服务端错误。"})
            if row["delta_total_p95_ms"] is not None and base_p95 and row["delta_total_p95_ms"] >= max(100, base_p95 * .2):
                signals.append({"level": "risk", "title": f"{row['name']} 总耗时 p95 明显上升", "detail": "长尾延迟相对基准组恶化，继续加压可能扩大排队和超时。"})
            if row["delta_completed_rpm"] is not None and base_completed_rpm and row["delta_completed_rpm"] <= max(-1, -base_completed_rpm * .08):
                signals.append({"level": "risk", "title": f"{row['name']} 完成吞吐未随压力增长", "detail": "计划调度速率提高不等于实际完成能力提高，当前可能接近容量上限。"})
            if row["failure_count"]:
                signals.append({"level": "caution", "title": f"{row['name']} 出现 {row['failure_count']} 条失败或异常", "detail": "请进入该实验组标签，查看限流、服务端错误、超时和协议错误的具体分布。"})

        all_configured_done = len(rows) >= len(configured_groups) and len(complete_rows) == len(configured_groups)
        if not rows:
            level, title, summary_text = "neutral", "等待实验开始", "实验组尚未产生可分析结果。"
        elif not all_configured_done:
            level, title, summary_text = "neutral", "实验进行中，暂不下最终结论", f"已完成 {len(complete_rows)} / {len(configured_groups)} 个实验组；所有组完成后才会生成最终对照判断。"
        elif any(signal["level"] == "risk" for signal in signals):
            level, title, summary_text = "risk", "对比组相对基准组出现明显退化", "至少一个压力组在成功率、长尾延迟或实际完成吞吐上相对基准组恶化；不要用计划 RPM 代替真实完成能力。"
        elif capacity_inflection or any(signal["level"] == "caution" for signal in signals):
            level, title, summary_text = "caution", "观察到压力窗口关注信号", "当前窗口出现失败/异常或压力升高后指标停滞的信号；这不是网关容量承诺，需补充样本并复测。"
        else:
            level, title, summary_text = "good", "当前窗口未观察到明显退化", "在当前题量、串行执行和客户端黑盒观测条件下，未观察到明显退化，但尚不足以支持正式放量。"

        evidence_count = sum(int(row.get("completed_requests") or 0) for row in rows)
        evidence_grade = "低" if evidence_count < 30 else "有限" if evidence_count < 100 else "中" if evidence_count < 500 else "高"
        completed_sample_counts = [int(row.get("completed_requests") or 0) for row in complete_rows]
        min_group_sample = min(completed_sample_counts) if completed_sample_counts else 0
        if not all_configured_done:
            decision_scope = "进行中，不能用于最终决策"
            recommendation = "等待基准组和全部对比组完成后再比较；不要用当前中间结果判断容量。"
        elif evidence_count < max(30, len(rows) * 10) or min_group_sample < 10:
            decision_scope = "可做方向性比较，证据不足以承诺容量"
            recommendation = "增加每组题量，并至少重复 2 次；当前结果只说明本次黑盒样本的相对差异。"
        elif level == "risk":
            decision_scope = "可以定位本次配置下的退化信号，不支持扩大流量"
            recommendation = "暂停扩大流量；先按失败类型、排队和长尾延迟定位，再用相同题集复测。"
        elif level == "caution":
            decision_scope = "可以作为压力窗口关注信号，不能单独作为容量结论"
            recommendation = "补充中间压力档位并重复运行，确认信号是否稳定复现；完成复测后再评估灰度放量。"
        else:
            decision_scope = "仅支持当前题量、串行顺序和客户端观测窗口内的方向性比较"
            recommendation = "保留为当前窗口基线；完成重复、长跑和并行混压验证后，再评估是否进入受控灰度。"
        stable_rows = [row for row in complete_rows if row.get("capacity_signal") == "stable" and row.get("completed_rpm") is not None]
        stable_max = max((float(row["completed_rpm"]) for row in stable_rows), default=None)
        stable_max_row = max(stable_rows, key=lambda row: float(row.get("completed_rpm") or 0), default=None)
        if not all_configured_done:
            decision_status, decision_label = "pending", "等待全部实验组完成"
        elif level == "risk":
            decision_status, decision_label = "hold", "暂停扩大流量，先定位并复测"
        elif level == "caution":
            decision_status, decision_label = "review", "完成复测后再评估灰度放量"
        else:
            decision_status, decision_label = "observe", "未观察到明显退化，但尚不足以支持正式放量"
        next_steps = []
        if not all_configured_done:
            next_steps.append("等待基准组和全部对比组完成，再形成最终判断")
        elif level == "risk":
            next_steps.extend(["暂停扩大流量，定位失败类型、调度排队和 p95 长尾", "用相同题集重复运行，确认退化是否稳定复现", "风险解除前不要把当前结果当作放量依据"])
        elif level == "caution":
            next_steps.extend(["增加中间压力档位，定位压力窗口信号", "至少重复 2 次，排除偶发网络抖动", "完成复测后再评估受控灰度，不直接承诺容量"])
        else:
            next_steps.extend(["保留当前结果作为窗口基线", "补做长跑、重复运行与并行混压", "证据充足前不要把未观察到退化表述成无限容量"])
        planned_requests = sum(int(item.get("iterations") or 0) for item in configured_groups if isinstance(item, dict))
        completed_requests = sum(int(item.get("completed_requests") or 0) for item in rows)
        if status in ("finished", "finished_with_errors"):
            progress = 1.0
        elif status == "error" and not planned_requests:
            progress = 0.0
        else:
            progress = min(1.0, completed_requests / planned_requests) if planned_requests else 0.0
        completed_groups = sum(1 for item in rows if item.get("status") in ("finished", "finished_with_errors", "stopped", "error"))
        current_group = next((item for item in rows if item.get("id") == self.current_group_id), None)
        if current_group is None:
            current_group = next((item for item in rows if item.get("status") == "running"), None)
        if current_group is None:
            current_group = next((item for item in rows if item.get("status") == "waiting"), None)
        report = {
            "baseline_group": baseline_id,
            "groups": rows,
            "capacity_inflection": capacity_inflection,
            # 保留旧字段兼容历史消费者；新前端统一展示“压力窗口信号”，避免把启发式结果误称为容量。
            "pressure_window_signal": capacity_inflection,
            "capacity": {"observed_max_stable_completed_rpm": round(stable_max, 2) if stable_max is not None else None, "observed_max_stable_group": stable_max_row.get("name") if stable_max_row else None, "is_capacity_limit": bool(capacity_inflection)},
            "evidence": {"completed_requests": evidence_count, "grade": evidence_grade, "min_group_sample": min_group_sample, "note": "证据量级不是统计置信区间；它只表示本次已完成样本的多少。"},
            "comparability": comparability,
            "decision": {"status": decision_status, "label": decision_label, "scope": decision_scope, "recommendation": recommendation, "next_steps": next_steps, "min_group_sample": min_group_sample, "formal_release_allowed": False},
            "method": {"execution_policy": config.get("execution_policy", "serial"), "comparison_basis": "每个对比组与明确指定的基准组比较", "metrics": ["成功率", "实际完成吞吐", "计划调度 RPM", "总耗时 p50/p95", "调度延迟 p95", "失败与异常"], "order_effect": "基准组优先、对比组随后串行执行；连接预热、缓存、动态限流和服务状态随时间变化可能影响差异。"},
            "conclusion": {
                "level": level,
                "title": title,
                "summary": summary_text,
                "signals": signals,
                "thresholds": {"success_drop_pct": 1, "p95_increase_ratio": .2, "throughput_change_ratio": .08, "capacity_flatten_ratio": .92},
                "limitations": ["实验组当前按串行执行，不能代表真实混压场景。", "基准组优先的固定顺序可能引入连接预热、缓存和服务状态顺序效应。", "小样本只能说明本次黑盒观测，不等同于长期稳定性证明。", "计划调度 RPM、实际开始吞吐、实际完成吞吐是不同指标；当前报告不会把计划值冒充完成能力。", "压力窗口信号是启发式提示，需要用更多档位、重复运行和并行混压确认。"],
            },
        }
        return {
            "id": self.id, "name": self.name, "status": status,
            "status_label": status_label(status),
            "created": self.created, "started_at": started_at,
            "finished_at": finished_at,
            "elapsed": max(0, (finished_at or time.time()) - started_at) if started_at else 0,
            "execution_policy": config.get("execution_policy", "serial"),
            "config": public_config(config["shared_config"]),
            "groups": groups, "report": report,
            "progress": progress, "completed_groups": completed_groups, "total_groups": len(configured_groups),
            "completed_requests": completed_requests, "planned_requests": planned_requests,
            "current_group_id": current_group.get("id") if current_group else None,
            "current_group_name": current_group.get("name") if current_group else None,
        }


class ComparisonManager:
    def __init__(self):
        self.runs = {}
        self.lock = threading.RLock()

    def create(self, raw):
        cfg = validate_comparison_config(raw)
        run = ComparisonRun(cfg)
        with self.lock:
            self.runs[run.id] = run
        # 首版先落地“创建实验 + 组配置”能力；执行沿用任务队列，避免并行争用。
        threading.Thread(target=self._execute, args=(run,), daemon=True).start()
        persist_history()
        return run

    def _execute(self, run):
        try:
            with run.lock:
                run.status = "running"
                run.started_at = time.time()
            last_persist_at = 0.0
            for group in run.config["groups"]:
                shared = dict(run.config["shared_config"])
                shared.update({"name": f"{run.name} · {group['name']}", "concurrency": group["concurrency"],
                               "rpm": group["rpm"], "iterations": group["iterations"], "mode": "smoke"})
                try:
                    task = MANAGER.add(shared, parent_comparison_id=run.id, parent_group_id=group["id"])
                    with run.lock:
                        run.current_group_id = group["id"]
                        run.current_group_name = group["name"]
                        run.group_results.append({"id": group["id"], "name": group["name"], "task_id": task.id,
                                                  "role": group.get("role"), "concurrency": group["concurrency"], "rpm": group["rpm"], "iterations": group["iterations"],
                                                  "status": task.status, "progress": 0, "done": 0, "total": group["iterations"]})
                    while task.status in ("waiting", "running"):
                        snapshot = task.snap()
                        with run.lock:
                            current = next((item for item in run.group_results if item.get("id") == group["id"]), None)
                            if current is not None:
                                current.update({"status": snapshot.get("status"), "progress": snapshot.get("progress", 0),
                                                 "done": snapshot.get("done", 0), "total": snapshot.get("total", group["iterations"]),
                                                 "started": snapshot.get("started", 0), "summary": snapshot})
                        now = time.monotonic()
                        if now - last_persist_at >= 1.0:
                            persist_history()
                            last_persist_at = now
                        time.sleep(.2)
                    snapshot = task.snap()
                    with run.lock:
                        current = next((item for item in run.group_results if item.get("id") == group["id"]), None)
                        if current is not None:
                            current.update({"status": task.status, "progress": snapshot.get("progress", 0), "done": snapshot.get("done", 0),
                                             "total": snapshot.get("total", group["iterations"]), "summary": snapshot})
                        if task.status != "finished":
                            run.status = "finished_with_errors"
                except Exception as exc:
                    with run.lock:
                        run.group_results.append({
                            "id": group["id"], "name": group["name"], "role": group.get("role"),
                            "concurrency": group["concurrency"], "rpm": group["rpm"],
                            "iterations": group["iterations"], "status": "error",
                            "progress": 0, "done": 0, "total": group["iterations"],
                            "summary": {},
                            "error": _redact_secrets(str(exc), _secret_values(run.config)),
                        })
                        run.status = "error"
                    break
            with run.lock:
                run.finished_at = time.time()
                run.current_group_id = None
                run.current_group_name = None
                if run.status not in ("error", "finished_with_errors"):
                    run.status = "finished"
        finally:
            # 公共配置只在执行期间使用密钥；任何异常路径都必须清理。
            with run.lock:
                _clear_secret_fields(run.config)
            # 对照实验本身是一条持久化记录，所有实验组完成后必须保存最终报告。
            persist_history()

    def delete(self, run_id):
        with self.lock:
            run = self.runs.get(run_id)
            if not run:
                return "对照实验不存在"
            if run.status in ("running", "waiting", "stopping"):
                return "请等待对照实验结束后再删除"
            del self.runs[run_id]
            child_ids = [item.get("task_id") for item in run.group_results if item.get("task_id")]
        # 实验组是这条对照实验的内部子任务，不会出现在顶层列表里；
        # 只删记录不删组结果文件，等于把它们永久遗留在磁盘上无法再清理。
        leftover = []
        for task_id in child_ids:
            child = MANAGER.get(task_id)
            if child is None:
                continue
            if MANAGER.delete(task_id):
                leftover.append(task_id)
        persist_history()
        if leftover:
            return "对照实验记录已删除，但部分组结果文件删除失败"
        return None

    def get(self, run_id):
        with self.lock:
            return self.runs.get(run_id)

    def ordered(self):
        with self.lock:
            return [run.snap() for run in sorted(self.runs.values(), key=lambda x: -x.created)]


COMPARISONS = ComparisonManager()

# 智能路由 vs 固定路由的对照实验：与 ComparisonManager 并列的第三条记录线，
# 但它的一条记录对应「同一个清单交错打在两条路由上」，不是多个实验组串行。
ROUTING_COMPARISONS = routing_compare.RoutingCompareManager(on_change=lambda: persist_history())


def _quality_public_config(cfg):
    """返回质量测试摘要，未知配置字段和凭据均不暴露。"""
    allowed = {"name", "capture_raw", "suite_path", "suite_id", "suite_version", "case_count", "suite_manifest", "reference_reuse_id"}
    public = {key: _without_secret_fields(value) for key, value in cfg.items() if key in allowed}
    resources = []
    for item in cfg.get("resources", []):
        if not isinstance(item, dict):
            continue
        resource_allowed = {"name", "url", "model", "connect_timeout", "read_timeout", "write_timeout", "http2", "insecure", "is_reference"}
        resources.append({key: _without_secret_fields(value) for key, value in item.items() if key in resource_allowed})
    public["resources"] = resources
    return public


def validate_quality_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("质量测试请求体必须是 JSON 对象")
    name = str(raw.get("name") or "质量评测").strip()[:80]
    suite_path = raw.get("suite_path")
    if suite_path is not None and not isinstance(suite_path, str):
        raise ValueError("suite_path 必须是字符串路径")
    cases = load_suite(suite_path) if suite_path else load_suite()
    manifest = suite_manifest(suite_path, cases)
    resources = raw.get("resources") or []
    if not isinstance(resources, list) or not 1 <= len(resources) <= 2:
        raise ValueError("质量测试需要 1 到 2 个资源实例")
    normalized = []
    for index, item in enumerate(resources, 1):
        if not isinstance(item, dict):
            raise ValueError(f"资源实例 {index} 配置无效")
        value = dict(item)
        unknown_fields = set(value) - {
            "name", "url", "api_key", "model", "connect_timeout", "read_timeout", "write_timeout", "http2", "insecure", "is_reference",
        }
        if unknown_fields:
            raise ValueError(f"资源实例 {index} 含不支持的字段：{sorted(unknown_fields)[0]}")
        value["name"] = str(value.get("name") or f"资源 {index}").strip()[:60]
        value["url"] = str(value.get("url") or "").strip()
        value["api_key"] = str(value.get("api_key") or "").strip()
        value["model"] = str(value.get("model") or "").strip()
        if not value["url"]:
            raise ValueError(f"资源实例 {index} 缺少 URL")
        parsed = urllib.parse.urlparse(value["url"])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"资源实例 {index} URL 必须是有效的 http 或 https 地址")
        if not value["api_key"]:
            value["api_key"] = _load_api_key(value["url"])
        if not value["api_key"]:
            raise ValueError(f"资源实例 {index} 缺少 API Key；也可以留空复用该 URL 已保存的密钥")
        if not value["model"]:
            raise ValueError(f"资源实例 {index} 缺少模型名称")
        value["connect_timeout"] = safe_float(value.get("connect_timeout", 10), f"资源实例 {index} 连接超时", 0.1, 3600)
        value["read_timeout"] = safe_float(value.get("read_timeout", 60), f"资源实例 {index} 读取超时", 0.1, 3600)
        value["write_timeout"] = safe_float(value.get("write_timeout", 30), f"资源实例 {index} 写入超时", 0.1, 3600)
        value["http2"] = parse_bool(value.get("http2", False), f"资源实例 {index} HTTP/2 开关", False)
        value["insecure"] = parse_bool(value.get("insecure", False), f"资源实例 {index} 跳过 TLS 证书校验开关", False)
        resource_allowed = {
            "name", "url", "api_key", "model", "connect_timeout", "read_timeout", "write_timeout", "http2", "insecure", "is_reference",
        }
        value["is_reference"] = parse_bool(value.get("is_reference", False), f"资源实例 {index} 参考标记", False)
        normalized.append({key: value[key] for key in resource_allowed if key in value})
    capture_raw = parse_bool(raw.get("capture_raw", False), "记录原始数据开关", False)
    manifest["execution"] = methodology_snapshot()["protocol"] if len(normalized) == 2 else {
        "id": "single_sequential_v1", "label": "单资源按题序执行", "repeats": 1,
        "description": "每题仅请求一次，不重试；按实际响应评分并保存后更新进度。",
        "limitation": "单轮观测，不代表跨时间稳定性。"}
    reference_reuse_id = str(raw.get("reference_reuse_id") or "").strip() or None
    reference_indexes = [i for i, item in enumerate(normalized) if item.get("is_reference")]
    if reference_indexes and len(normalized) != 2:
        raise ValueError("官方参考资源只能在双资源对照中配置")
    if len(reference_indexes) > 1:
        raise ValueError("只能标记一个官方参考资源")
    manifest["reference_index"] = reference_indexes[0] if reference_indexes else None
    manifest["reference_source"] = "admin_configured_reference" if reference_indexes else None
    manifest["same_claimed_model"] = bool(reference_indexes and normalized[0].get("model") == normalized[1].get("model"))
    return {
        "name": name,
        "resources": normalized,
        "capture_raw": capture_raw,
        "reference_reuse_id": reference_reuse_id,
        "suite_path": str(suite_path) if suite_path else None,
        "suite_id": manifest["id"], "suite_version": manifest["version"],
        "suite_manifest": manifest, "_suite_cases": [c.to_dict() for c in cases],
        "case_count": len(cases),
    }


def _quality_percentile(values, ratio):
    values = sorted(float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)))
    if not values:
        return None
    index = min(len(values) - 1, max(0, math.ceil(len(values) * ratio) - 1))
    return round(values[index], 2)


def _quality_progress(task):
    """只把已评分、已保存的逐题结果计入进度；失败响应也有明确的评测结果。"""
    resources = task.results
    case_count = int(task.config.get("case_count") or 0)
    total_resources = len(task.config.get("resources") or resources)
    planned = case_count * total_resources
    completed = sum(sum("score" in item and "passed" in item
                        for item in resource.get("case_results", [])) for resource in resources)
    successful = sum(sum(bool(item.get("completed")) for item in resource.get("case_results", []))
                     for resource in resources)
    errors = sum(len(resource.get("request_errors") or []) for resource in resources)
    return {
        "planned_cases": planned,
        "completed_cases": completed,
        "processed_cases": sum(len(r.get("case_results", [])) for r in resources),
        "successful_responses": successful,
        "failed_cases": errors,
        "completed_resources": sum(bool(r.get("summary")) and
                                   len(r.get("case_results", [])) >= case_count > 0 for r in resources),
        "total_resources": total_resources,
        "progress": min(1.0, completed / planned) if planned else 0.0,
        "current_resource": task.current_resource,
        "current_case": task.current_case,
        "phase": task.phase,
    }


def _quality_report(status, resources, case_count, manifest=None):
    return build_quality_report(status, resources, case_count, manifest)


def _quality_resource_status(resource_result, case_count):
    """单个资源（对照实验的其中一侧）的执行结论。

    与任务级终态同源：题目没跑完算「已停止」，跑完但有请求错误算「完成但有错误」。
    """
    if len(resource_result.get("case_results") or []) < case_count:
        return "stopped"
    return "finished_with_errors" if resource_result.get("request_errors") else "finished"


class QualityTask(ArtifactMixin):
    def __init__(self, config):
        self.id = uuid.uuid4().hex[:12]
        self.name = config["name"]
        self.config = copy.deepcopy(config)
        self.public_config = _quality_public_config(config)
        self.status = "waiting"
        self.created = time.time()
        self.started_at = None
        self.finished_at = None
        self.error = None
        self.stop_event = threading.Event()
        # 从创建时就展示全部资源，避免对照任务执行到一半变成“单资源测试”。
        historical = config.get("_historical_reference_results") or {}
        self.results = []
        for r in config.get("resources", []):
            if r.get("historical_reference"):
                result = copy.deepcopy(historical)
                result.update({"name": r["name"], "url": r.get("url", result.get("url", "")), "model": r["model"], "status": "finished", "is_reference": True, "historical_reference": True, "reference_task_id": r.get("reference_task_id")})
                self.results.append(result)
            else:
                self.results.append({"name": r["name"], "url": r["url"], "model": r["model"],
                         "status": "waiting", "planned_cases": config.get("case_count", 0),
                         "is_reference": bool(r.get("is_reference")), "historical_reference": False, "reference_task_id": None,
                         "case_results": [], "summary": None, "request_errors": []})
        self.lock = threading.RLock()
        self.history_only = False
        self.current_resource = None
        self.current_case = None
        self.phase = "waiting"
        self.out_path = os.path.join(BASE_DIR, f"quality_run_{self.id}.json")
        # 评测证据文件就是本任务的 artifact：路径、容量、校验值登记在任务上，
        # 删除记录时才能连带删除；历史恢复时才能发现文件已被外部改动或删掉。
        self.artifact_path = self.out_path
        self.artifact_state = "unknown"

    def snap(self, include_details=True):
        with self.lock:
            return {
                "id": self.id, "name": self.name, "status": self.status,
                "status_label": status_label(self.status),
                "created": self.created, "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": max(0, (self.finished_at or time.time()) - self.started_at) if self.started_at else 0,
                "error": self.error, "config": copy.deepcopy(self.public_config),
                "case_count": self.config.get("case_count", 0),
                "resources": copy.deepcopy(self.results) if include_details else [{k: v for k, v in item.items() if k not in {"case_results", "request_errors"}} for item in self.results],
                "out_path": self.out_path, "history_only": self.history_only,
                "artifact": self.artifact_snapshot(),
                **_quality_progress(self),
                "report": _quality_report(self.status, self.results, self.config.get("case_count", 0), self.public_config.get("suite_manifest")) if include_details else None,
            }

    def persist(self):
        """原子保存完整评测证据；仅在显式开启时包含原始输入输出。"""
        snapshot = self.snap()
        temp = f"{self.out_path}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(snapshot, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.out_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        # 证据文件刚写完，顺手刷新容量信息（不算 sha256，避免每题都做一次全量哈希）。
        self.refresh_artifact(verify=False)


class QualityManager:
    def __init__(self):
        self.tasks = {}
        self.lock = threading.RLock()

    def create(self, raw):
        cfg = validate_quality_config(raw)
        reuse_id = cfg.get("reference_reuse_id")
        if reuse_id:
            with self.lock:
                previous = self.tasks.get(reuse_id)
            if not previous or previous.status not in ("finished", "finished_with_errors"):
                raise ValueError("选择复用的参考评测不存在或尚未完成")
            previous_snapshot = previous.snap()
            prev_manifest = (previous_snapshot.get("config") or {}).get("suite_manifest") or {}
            current_manifest = cfg.get("suite_manifest") or {}
            if prev_manifest.get("fingerprint") != current_manifest.get("fingerprint"):
                raise ValueError("历史参考评测的题集版本或指纹不同，不能复用")
            if len(cfg.get("resources") or []) != 1:
                raise ValueError("复用历史参考结果时，只能填写一个被测资源")
            tested = cfg["resources"][0]
            prev_resources = previous_snapshot.get("resources") or []
            if len(prev_resources) != 1:
                raise ValueError("只能复用单资源质量评测作为参考基准")
            ref = dict(prev_resources[0])
            ref.update({"is_reference": True, "historical_reference": True, "reference_task_id": reuse_id, "api_key": "", "url": ref.get("url", "")})
            cfg["resources"] = [tested, ref]
            cfg["_historical_reference_results"] = copy.deepcopy(prev_resources[0])
            cfg["suite_manifest"]["reference_index"] = 1
            cfg["suite_manifest"]["reference_source"] = "reused_historical_result"
            cfg["suite_manifest"]["same_claimed_model"] = bool(tested.get("model") == ref.get("model"))
        # 质量评测与网关压测共用同一套 macOS Keychain（钥匙串）凭据策略。
        # 只保存到钥匙串，不进入任务摘要、浏览器本地存储或历史索引。
        for resource in cfg.get("resources", []):
            if not resource.get("historical_reference"):
                _save_api_key(resource.get("url"), resource.get("api_key"))
        task = QualityTask(cfg)
        with self.lock:
            self.tasks[task.id] = task
        threading.Thread(target=self._launch, args=(task,), daemon=True).start()
        persist_history()
        return task

    def _launch(self, task):
        with task.lock:
            if task.status != "stopped":
                apply_transition(task, "running", force=True)
                task.phase = "running"
            task.started_at = task.started_at or time.time()
        coroutine = run_quality_task(task)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, LOOP)
        except Exception as exc:
            coroutine.close()
            with task.lock:
                task.error = task.error or _redact_secrets(str(exc), _secret_values(task.config))
                apply_transition(task, "error", force=True)
                task.finished_at = time.time()
                task.phase = "error"
                _clear_secret_fields(task.config)
            persist_history()
            return

        def done(fut):
            try:
                fut.result()
            except Exception as exc:
                with task.lock:
                    task.error = task.error or _redact_secrets(str(exc), _secret_values(task.config))
                    apply_transition(task, "error", force=True)
                    task.finished_at = time.time()
            finally:
                # 无论题集加载、网络请求还是评分阶段如何失败，都不能把密钥留在内存。
                with task.lock:
                    _clear_secret_fields(task.config)
                persist_history()
        future.add_done_callback(done)

    def delete(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return "质量评测不存在"
            if task.status in ("running", "waiting", "stopping"):
                return "请先停止或等待质量评测结束"
            artifact_deleted = task.delete_artifact()
            del self.tasks[task_id]
        persist_history()
        if not artifact_deleted:
            return "质量评测记录已删除，但证据文件删除失败（可能正在被占用）"
        return None

    def get(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)

    def stop(self, task_id):
        """请求停止后续题目；已在执行的那一题会完成并落盘。

        与压测任务同一套语义：停止不是立即中断，而是「不再发新请求」。
        状态先进入 stopping，等 run_quality_task 收尾时再归一化成终态，
        避免界面在收尾期间仍显示「运行中」。
        """
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return "质量评测不存在"
            if task.status in ("running", "stopping"):
                task.stop_event.set()
                apply_transition(task, "stopping", force=True)
                task.phase = "stopping"
                task.persist()
                return None
            if task.status == "waiting":
                # 还没开始执行：直接判定为已停止，不需要发停止信号。
                task.stop_event.set()
                apply_transition(task, "stopped", force=True)
                task.phase = "stopped"
                task.persist()
                return None
            return "质量评测已经结束"

    def ordered(self, summary=False):
        with self.lock:
            values = list(self.tasks.values())
        order = {"running": 0, "stopping": 1, "waiting": 2, "finished_with_errors": 3, "error": 4, "expired": 5, "finished": 6}
        return [x.snap(include_details=not summary) for x in sorted(values, key=lambda item: (order.get(item.status, 9), -item.created))]


QUALITY = QualityManager()


async def run_quality_task(task):
    secrets = _secret_values(task.config)
    has_errors = False
    try:
        frozen = task.config.get("_suite_cases")
        cases = tuple(QualityCase.from_dict(c, i) for i, c in enumerate(frozen)) if frozen else load_suite(task.config.get("suite_path"))
        with task.lock:
            # 排队期间被停止的任务可能永远不进入 running；这里只在确实该跑的时候改状态。
            if task.status != "stopped":
                apply_transition(task, "running", force=True)
                task.phase = "running"
            task.started_at = task.started_at or time.time()
            task.config["case_count"] = task.public_config["case_count"] = len(cases)
            for resource in task.results:
                resource["planned_cases"] = len(cases)
            task.persist()
        resources = task.config["resources"]
        responses_by_resource = [{} for _ in resources]
        clients = {}
        active_resources = [r for r, resource in enumerate(resources) if not resource.get("historical_reference")]
        alternate = task.config.get("suite_manifest", {}).get("execution", {}).get("id") == "paired_alternating_v1" and len(active_resources) > 1
        schedule = ([(i, r) for i in range(len(cases)) for r in (active_resources if i % 2 == 0 else list(reversed(active_resources)))]
                    if alternate else [(i, r) for r in active_resources for i in range(len(cases))])
        async with AsyncExitStack() as stack:
            for resource_index, resource in enumerate(resources):
                try:
                    clients[resource_index] = await stack.enter_async_context(httpx.AsyncClient(http2=resource["http2"], verify=not resource["insecure"]))
                except Exception as exc:
                    has_errors = True
                    with task.lock:
                        task.results[resource_index].update(status="error", error=_redact_secrets(str(exc), secrets))
                        task.persist()
            for sequence, (index, resource_index) in enumerate(schedule, 1):
                if task.stop_event.is_set():
                    break
                if resource_index not in clients:
                    continue
                case = cases[index]
                resource = resources[resource_index]
                resource_result = task.results[resource_index]
                responses = responses_by_resource[resource_index]
                with task.lock:
                    resource_result["status"] = "running"
                    task.current_resource = resource["name"]
                    task.phase = "requesting"
                    task.current_case = index + 1
                headers = {"Authorization": f"Bearer {resource['api_key']}", "Accept": "application/json"}
                timeout_cfg = {"connect": resource["connect_timeout"], "read": resource["read_timeout"],
                               "write": resource["write_timeout"], "pool": resource["connect_timeout"]}
                payload = {"model": resource["model"], "messages": [{"role": "user", "content": case.prompt}], "stream": False}
                payload.update(dict(case.request))
                payload["model"] = resource["model"]
                item = {"id": index, "mode": "quality", "profile": case.category, "step_index": 1}
                try:
                    result = await do_request(clients[resource_index], item, payload, headers, timeout_cfg,
                                              chat_url=strip_url(resource["url"]), capture_raw=task.config["capture_raw"])
                except Exception as exc:
                    result = {"status_code": 0, "completed": False, "error_type": "NETWORK",
                              "reason": str(exc), "response_text": "", "ttft_ms": None, "total_ms": None}
                result = _redact_secrets(result, secrets)
                response = result.get("response_text") or ""
                request_ok = bool(result.get("completed")) and not result.get("error_type") and 200 <= (result.get("status_code") or 0) < 300
                with task.lock:
                    task.phase = "scoring"
                if request_ok:
                    responses[case.id] = response
                scored = run_quality_probe(responses, cases[:index + 1]).summary.to_dict()
                case_score = scored["results"][-1]
                case_result = {
                    "case_id": case.id, "category": case.category, "prompt": case.prompt,
                    "title": case.title, "difficulty": case.difficulty, "critical": case.critical,
                    "reference_answer": case.reference_answer, "rationale": case.rationale,
                    "evaluator": dict(case.evaluator), "weight": case.weight,
                    "benchmark_refs": list(case.benchmark_refs), "input_chars": len(case.prompt),
                    "execution_sequence": sequence,
                    "status_code": result.get("status_code"), "completed": request_ok,
                    "ttft_ms": result.get("ttft_ms"), "total_ms": result.get("total_ms"),
                    "response_preview": response[:240], "error": result.get("reason"),
                    **{key: case_score[key] for key in ("score", "passed", "checks", "failures")},
                }
                if task.config["capture_raw"]:
                    case_result.update({"response": response, "raw_input": result.get("raw_input") or payload,
                                        "raw_output": result.get("raw_output")})
                case_result = _redact_secrets(case_result, secrets)
                request_error = None if request_ok else {
                    "case_id": case.id, "status_code": result.get("status_code"),
                    "error_type": result.get("error_type") or "PROTOCOL", "error": result.get("reason") or "请求未返回完整成功响应"}
                with task.lock:
                    resource_result["case_results"].append(case_result)
                    old_summary = resource_result["summary"]
                    resource_result["summary"] = _history_safe_snapshot(scored)
                    if request_error:
                        resource_result["request_errors"].append(request_error)
                        has_errors = True
                    try:
                        # 每题完成评分与持久化后才公开进度；保存失败回滚内存进度。
                        task.persist()
                    except OSError:
                        resource_result["case_results"].pop()
                        resource_result["summary"] = old_summary
                        if request_error:
                            resource_result["request_errors"].pop()
                        raise
                persist_history()
            with task.lock:
                for resource_result in task.results:
                    if resource_result["status"] == "error":
                        continue
                    resource_result["status"] = _quality_resource_status(resource_result, len(cases))
                task.current_case = None
                task.persist()
        with task.lock:
            task.phase = "finished"
            task.current_resource = None
            stopped = task.stop_event.is_set() and any(len(r["case_results"]) < len(cases) for r in task.results)
            apply_transition(task, normalize_terminal_state(
                task.status, done=sum(len(r["case_results"]) for r in task.results),
                total=len(cases) * max(1, len(active_resources)),
                has_errors=has_errors, stopped=stopped,
            ), force=True)
            if task.status == "stopped":
                task.phase = "stopped"
                for result in task.results:
                    if result["status"] in ("running", "waiting", "stopping"):
                        result["status"] = "stopped"
            task.finished_at = time.time()
            task.refresh_artifact(verify=False)
            task.persist()
    except Exception as exc:
        with task.lock:
            task.error = _redact_secrets(str(exc), secrets)
            apply_transition(task, "error", force=True)
            task.phase = "error"
            task.current_case = None
            task.current_resource = None
            task.finished_at = time.time()
            try:
                task.persist()
            except OSError:
                pass
        raise
    finally:
        with task.lock:
            _clear_secret_fields(task.config)
        persist_history()


class TaskManager:
    def __init__(self):
        self.tasks = {}
        self.config_lock = threading.Lock()
        self.queue = deque()
        self.running_ids = []
        self.lock = threading.RLock()

    @property
    def current_id(self):
        """保留旧字段名，返回最近启动的任务 id。

        调度已经从「全局同时只能跑一个」改成由 LIMITER 控制并发上限，
        但历史代码/测试仍会读 current_id；这里保留为只读别名，避免语义漂移。
        """
        return self.running_ids[-1] if self.running_ids else None

    def add(self, raw_config, parent_comparison_id=None, parent_group_id=None):
        cfg = validate_config(raw_config)
        steps, total = build_plan(cfg)
        task = Task(cfg, steps, total)
        task.parent_comparison_id = parent_comparison_id
        task.parent_group_id = parent_group_id
        with self.lock:
            self.tasks[task.id] = task
            self.queue.append(task.id)
        EVENTS.log("task.queued", task_id=task.id, name=task.name, mode=cfg.get("mode"),
                   concurrency=cfg.get("concurrency"), planned=total, parent=parent_comparison_id)
        self._pump()
        persist_history()
        return task

    def _pump(self):
        # 队列里可能同时有多个等待任务；只要并发闸门还有名额，就把它们按提交顺序放出去。
        while True:
            with self.lock:
                if not self.queue:
                    return
                pending_id = self.queue[0]
                if not LIMITER.acquire(pending_id):
                    return
                self.queue.popleft()
                task = self.tasks.get(pending_id)
                if not task:
                    LIMITER.release(pending_id)
                    continue
                self.running_ids.append(task.id)
                apply_transition(task, "running", force=True)
                try:
                    task.open_output()
                except Exception as exc:
                    task.error = f"无法创建结果文件：{exc}"
                    apply_transition(task, "error", force=True)
                    task.close_output()
                    if task.id in self.running_ids:
                        self.running_ids.remove(task.id)
                    LIMITER.release(task.id)
                    continue
                EVENTS.log("task.started", task_id=task.id, name=task.name,
                           target=task.public_config.get("url"), planned=task.total)
                future = asyncio.run_coroutine_threadsafe(run_task(task), LOOP)
                future.add_done_callback(lambda fut, t=task: self._future_done(t, fut))

    def _future_done(self, task, future):
        try:
            future.result()
        except Exception as exc:
            with task.lock:
                task.error = task.error or str(exc)
                apply_transition(task, "error", force=True)
            EVENTS.log("task.crashed", level="error", task_id=task.id, error=str(exc))
            self.finish(task)

    def finish(self, task):
        task.close_output()
        with self.lock:
            if task.id in self.running_ids:
                self.running_ids.remove(task.id)
            LIMITER.release(task.id)
        EVENTS.log("task.finished", task_id=task.id, status=task.status, done=task.done,
                   total=task.total, counters=dict(task.counters))
        persist_history()
        self._pump()

    def stop(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return "任务不存在"
            if task.status == "running":
                task._stopped.set()
                apply_transition(task, "stopping", force=True)
            elif task.status == "waiting":
                self.queue = deque(x for x in self.queue if x != task_id)
                apply_transition(task, "stopped", force=True)
            else:
                return "任务已经结束"
        persist_history()
        return None

    def delete(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return "任务不存在"
            if task.status in ("running", "waiting", "stopping"):
                return "请先停止任务再删除"
            artifact_deleted = task.delete_artifact()
            del self.tasks[task_id]
        persist_history()
        if not artifact_deleted:
            return "任务记录已删除，但结果文件删除失败（可能正在被占用）"
        return None

    def get(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)

    def snapshot(self):
        """调度器自身状态，用于「为什么我的任务还没开始」这类问题。"""
        with self.lock:
            waiting = list(self.queue)
        state = LIMITER.snapshot()
        state["waiting"] = waiting
        state["running"] = list(self.running_ids)
        return state

    def ordered(self):
        with self.lock:
            tasks = list(self.tasks.values())
        order = {"running": 0, "waiting": 1, "error": 2, "stopped": 3, "finished": 4}
        return [task.snap() for task in sorted(tasks, key=lambda t: (order.get(t.status, 9), -t.created)) if not task.parent_comparison_id]


MANAGER = TaskManager()
SERVER_SHUTTING_DOWN = False


class _FingerprintLimiter:
    """Share capacity and wake the existing event-driven gateway queue on release."""
    def acquire(self, run_id):
        return LIMITER.acquire(run_id)

    def release(self, run_id):
        LIMITER.release(run_id)
        if not SERVER_SHUTTING_DOWN:
            MANAGER._pump()


FINGERPRINTS = FingerprintManager(BASE_DIR, limiter=_FingerprintLimiter())



async def run_task(task):
    cfg = task.config
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Accept": "text/event-stream"}
    timeout_cfg = {"connect": cfg["connect_timeout"], "read": cfg["read_timeout"],
                   "write": cfg["write_timeout"], "pool": cfg["connect_timeout"]}
    queue = asyncio.Queue(maxsize=max(cfg["concurrency"] * 4, 100))
    task._stop_event = asyncio.Event()
    workers = []
    producer_task = None
    watcher = None
    normal_signaled = False
    stop_signaled = False

    async def finish_workers_normally():
        nonlocal normal_signaled
        if normal_signaled or stop_signaled:
            return
        normal_signaled = True
        # 正常结束：哨兵必须排在所有已入队请求之后，不能清空队列。
        for _ in range(cfg["concurrency"]):
            await queue.put(None)

    async def stop_workers_after_cancel():
        nonlocal stop_signaled
        if stop_signaled:
            return
        stop_signaled = True
        # 用户停止：只移除尚未开始的请求；已在 worker 中执行的请求继续完成并落盘。
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break
        for _ in range(cfg["concurrency"]):
            await queue.put(None)

    async def watch_stop():
        while not task._stopped.is_set():
            await asyncio.sleep(.05)
        task._stop_event.set()
        if producer_task and not producer_task.done():
            producer_task.cancel()
        await stop_workers_after_cancel()

    async def producer():
        item_id = 0
        offset = 0.0
        loop = asyncio.get_running_loop()
        base = loop.time()
        for step in task.steps:
            step_start = base + offset
            with task.lock:
                task.schedule_step = step["index"]
                task.schedule_target_rpm = step["rpm"]
            rps = step["rpm"] / 60
            for index in range(step["count"]):
                if task._stop_event.is_set():
                    return
                due = step_start + index / rps
                wait = due - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                item = {"id": item_id, "step_index": step["index"], "scheduled_at": due,
                        "mode": cfg["mode"], "profile": cfg["profile"], "grade": cfg.get("grade")}
                await queue.put(item)
                with task.lock:
                    task.scheduled += 1
                    task.queue_depth = queue.qsize()
                item_id += 1
            offset += step["duration"]
        await finish_workers_normally()

    async def worker():
        loop = asyncio.get_running_loop()
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                if task._stop_event.is_set():
                    continue
                with task.lock:
                    task.started += 1
                    task.queue_depth = queue.qsize()
                    task.current_step = item["step_index"]
                    step = task.steps[item["step_index"] - 1]
                    task.current_target_rpm = step["rpm"]
                    schedule_lag_ms = max(0, int((loop.time() - item["scheduled_at"]) * 1000))
                    task.schedule_lag_ms.append(schedule_lag_ms)
                payload = build_payload(cfg["model"], cfg["profile"], None, None, None, cfg["stream"], request_index=item["id"])
                include_usage = cfg.get("include_usage", True)
                result = await do_request(client, item, payload, headers, timeout_cfg,
                                          chat_url=strip_url(cfg["url"]), capture_raw=cfg["capture_raw"],
                                          include_usage=include_usage)
                # 网关不认 stream_options.include_usage 时通常直接 400；把这一轮降级为
                # 「不带 usage 参数」再请求一次，避免把兼容性差异记成请求失败。
                if include_usage and 400 <= (result.get("status_code") or 0) < 500 and not result.get("usage_seen"):
                    cfg["include_usage"] = False
                    result = await do_request(client, item, payload, headers, timeout_cfg,
                                              chat_url=strip_url(cfg["url"]), capture_raw=cfg["capture_raw"],
                                              include_usage=False)
                result["schedule_lag_ms"] = schedule_lag_ms
                task.record(result)
            finally:
                queue.task_done()
                with task.lock:
                    task.queue_depth = queue.qsize()

    try:
        async with httpx.AsyncClient(http2=cfg["http2"], verify=not cfg["insecure"]) as client:
            watcher = asyncio.create_task(watch_stop())
            task._sampler = asyncio.create_task(task.sample())
            workers = [asyncio.create_task(worker()) for _ in range(cfg["concurrency"])]
            producer_task = asyncio.create_task(producer())
            results = await asyncio.gather(producer_task, *workers, return_exceptions=True)
            real_errors = [x for x in results if isinstance(x, Exception) and not isinstance(x, asyncio.CancelledError)]
            if real_errors:
                raise real_errors[0]
            with task.lock:
                task.dropped = max(0, task.total - task.done)
                task.finished_at = time.time()
                # 终态由状态机统一决定：计划跑完但仍有失败/异常结果时必须是
                # 「部分完成」，不能对外显示成干净的「已完成」。
                failed_requests = task.total_requests_failed()
                # 只有「计划量没跑完」才算被中断。若是按了停止但恰好已经跑满计划量，
                # 结果覆盖是完整的，应如实报「已完成／部分完成」，不能降级成「已停止」。
                interrupted = task._stopped.is_set() and task.done < task.total
                task.status = normalize_terminal_state(
                    task.status,
                    done=task.done,
                    total=task.total,
                    has_errors=failed_requests > 0,
                    stopped=interrupted,
                )
                task.stopped_at = task.finished_at if task.status == "stopped" else None
    except asyncio.CancelledError:
        with task.lock:
            apply_transition(task, "stopped", force=True)
    except Exception as exc:
        with task.lock:
            task.error = task.error or str(exc)
            apply_transition(task, "error", force=True)
    finally:
        task._stopped.set()
        # 任务结束后立即从内存配置中移除密钥；重测必须重新输入。
        task.config["api_key"] = ""
        for async_task in (watcher, task._sampler):
            if async_task and not async_task.done():
                async_task.cancel()
        MANAGER.finish(task)


def list_snapshot(item):
    keep = {"id", "name", "status", "status_label", "created", "mode", "concurrency", "target_rpm",
            "progress", "done", "total", "case_count", "planned_cases", "completed_cases", "failed_cases",
            "successful_responses", "progress_pct", "total_requests", "completed_requests", "planned_requests", "total_groups", "tier"}
    result = {key: value for key, value in item.items() if key in keep}
    result["_summary"] = True
    if "resources" in item:
        result["resources"] = [{key: resource.get(key) for key in ("name", "model", "status", "planned_cases")} for resource in item["resources"]]
    if "groups" in item:
        result["groups"] = [{key: group.get(key) for key in ("id", "name", "status", "progress")} for group in item["groups"]]
        report = item.get("report") or {}
        result["report"] = {"conclusion": report.get("conclusion"), "evidence": report.get("evidence")}
    if "sides" in item:
        result["sides"] = item["sides"]
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = f"RouteLoad/{APP_VERSION}"

    def log_message(self, fmt, *args):
        return

    def _internal_error(self, exc):
        rid = getattr(self, "_rid", None) or ops.new_request_id()
        # Stack locations aid diagnosis without logging exception strings, locals,
        # headers or request bodies (any of these can contain credentials).
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(exc.__traceback__)]
        try:
            EVENTS.log("http.error", level="error", rid=rid, error_type=type(exc).__name__, frames=frames)
        except Exception:
            pass
        return self._json({"error": "服务器处理失败，请凭请求编号查看运行诊断。", "request_id": rid}, 500)

    def _same_origin(self):
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True  # Local command-line clients do not send browser headers.
        try:
            source = urllib.parse.urlsplit(origin)
            host = self.headers.get("Host", "")
            return (source.scheme in {"http", "https"} and source.netloc.lower() == host.lower()
                    and not source.username and not source.password and source.path in {"", "/"}
                    and not source.query and not source.fragment)
        except ValueError:
            return False

    def _security_headers(self):
        # Scripts are external; inline handlers and dynamic code execution are blocked.
        return {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        }

    def _json(self, data, status=200, headers=None):
        # 防御性脱敏：即使未来某个摘要误带入 api_key，也不能通过接口返回。
        def redact(value, path=()):
            if isinstance(value, dict):
                # In evidence distributions, keys are sampled answers/model names,
                # not credential fields. Keep numeric counters intact for round-trip export.
                counters = (len(path) >= 3 and path[-3] == "cells" and path[-1] == "counts") or path[-2:] == ("diagnostics", "returned_models")
                if counters and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value.values()):
                    return value
                return {key: ("[已隐藏]" if _is_secret_field(key) else redact(item, path + (key,))) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(item, path) for item in value]
            return value
        raw = json.dumps(redact(data), ensure_ascii=False).encode()
        if getattr(self, "command", "") == "GET" and urllib.parse.urlsplit(self.path).path == "/tasks" and status == 200:
            etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
            headers = {**(headers or {}), "ETag": etag}
            if self.headers.get("If-None-Match") == etag:
                self._last_status = 304
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, no-cache")
                for name, value in self._security_headers().items():
                    self.send_header(name, value)
                self.end_headers()
                return
        self._last_status = status
        self.send_response(status)
        for key, value in self._security_headers().items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers(); self.wfile.write(raw)

    def _html(self):
        raw = INDEX_HTML.encode()
        self._last_status = 200
        self.send_response(200)
        for key, value in self._security_headers().items():
            self.send_header(key, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def _asset(self, path):
        # Fixed allowlist: no arbitrary project files or path traversal.
        assets = {
            "/static/tokens.css": ("tokens.css", "text/css; charset=utf-8"),
            "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/static/app.css": ("app.css", "text/css; charset=utf-8"),
            "/static/actions.js": ("actions.js", "text/javascript; charset=utf-8"),
            "/assets/fingerprint.js": ("fingerprint.js", "text/javascript; charset=utf-8"),
            "/assets/fingerprint.css": ("fingerprint.css", "text/css; charset=utf-8"),
        }
        filename, content_type = assets[path]
        raw = (CODE_DIR / "static" / filename).read_bytes()
        self._last_status = 200
        self.send_response(200)
        for key, value in self._security_headers().items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _fingerprint_get(self, parsed):
        parts = parsed.path.strip("/").split("/")
        if parts == ["fingerprint", "catalog"]:
            return self._json(fingerprint_protocol.catalog())
        if parts == ["fingerprints"]:
            return self._json({"runs": FINGERPRINTS.list_runs()})
        if parts == ["fingerprint", "references"]:
            return self._json({"references": FINGERPRINTS.list_references()})
        if len(parts) == 4 and parts[:2] == ["fingerprint", "references"] and parts[3] == "export":
            reference = FINGERPRINTS.get_reference(parts[2])
            if reference is None:
                return self._json({"error": "参考指纹不存在"}, 404)
            return self._json(reference["artifact"], headers={"Content-Disposition": f'attachment; filename="fingerprint-reference-{parts[2]}.json"'})
        if len(parts) in (2, 3) and parts[0] == "fingerprints":
            run = FINGERPRINTS.get(parts[1])
            if run is None:
                return self._json({"error": "指纹采集记录不存在"}, 404)
            if len(parts) == 2:
                return self._json(run)
            if parts[2] == "samples":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    offset = int(query.get("offset", [0])[0])
                    limit = int(query.get("limit", [100])[0])
                except (TypeError, ValueError):
                    raise ValueError("样本页码或条数必须为整数") from None
                return self._json(FINGERPRINTS.samples(parts[1], offset=offset, limit=limit))
            if parts[2] == "export":
                return self._json(FINGERPRINTS.export(parts[1]), headers={"Content-Disposition": f'attachment; filename="fingerprint-{parts[1]}.json"'})
            if parts[2] == "samples-export":
                artifact = FINGERPRINTS.export(parts[1])
                first = FINGERPRINTS.samples(parts[1], offset=0, limit=500)
                rows = first["samples"]
                for offset in range(500, first["total"], 500):
                    rows.extend(FINGERPRINTS.samples(parts[1], offset=offset, limit=500)["samples"])
                return self._json({"format": "workbench-fingerprint-evidence", "fingerprint": artifact, "samples": rows},
                                  headers={"Content-Disposition": f'attachment; filename="fingerprint-samples-{parts[1]}.json"'})
        return self._json({"error": "指纹接口不存在"}, 404)

    def _fingerprint_post(self, path, body):
        parts = path.strip("/").split("/")
        if parts == ["fingerprint", "models"]:
            # Discovery uses only the submitted connection; it never accesses the
            # existing model-list route's saved credentials or starts a collection.
            return self._json(discover_fingerprint_models(body))
        if parts == ["fingerprints"]:
            run = FINGERPRINTS.create(body)
            self._audit("fingerprint.create", target=run["id"], detail=run["mode"])
            return self._json({"run": run}, 201)
        if parts == ["fingerprint", "references", "import"]:
            reference = FINGERPRINTS.import_reference(body.get("data"), body.get("name"), body.get("source_note"), body.get("trusted_source", False))
            self._audit("fingerprint.reference.import", target=reference["id"])
            return self._json({"reference": reference}, 201)
        if len(parts) == 4 and parts[:2] == ["fingerprint", "references"] and parts[3] == "delete":
            if FINGERPRINTS.get_reference(parts[2]) is None:
                return self._json({"error": "参考指纹不存在"}, 404)
            FINGERPRINTS.delete_reference(parts[2])
            self._audit("fingerprint.reference.delete", target=parts[2])
            return self._json({"ok": True})
        if len(parts) == 3 and parts[0] == "fingerprints":
            if FINGERPRINTS.get(parts[1]) is None:
                return self._json({"error": "指纹采集记录不存在"}, 404)
            if parts[2] == "stop":
                run = FINGERPRINTS.stop(parts[1])
                self._audit("fingerprint.stop", target=parts[1])
                return self._json({"run": run})
            if parts[2] == "delete":
                FINGERPRINTS.delete(parts[1])
                self._audit("fingerprint.delete", target=parts[1])
                return self._json({"ok": True})
            if parts[2] == "compare":
                comparison = FINGERPRINTS.compare(parts[1], body.get("reference_id"))
                self._audit("fingerprint.compare", target=parts[1])
                return self._json({"comparison": comparison})
        return self._json({"error": "指纹接口不存在"}, 404)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            # A 2 MiB artifact plus its JSON wrapper/name/source fits in 3 MiB.
            limit = 3 * 1024 * 1024 if self.path.split("?", 1)[0] == "/fingerprint/references/import" else 2 * 1024 * 1024
            if length < 0 or length > limit:
                raise ValueError("请求体过大或长度无效")
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("请求体不是有效 JSON")

    def _begin(self):
        """每个请求一个短 id，贯穿事件日志与审计记录，方便把一次操作的全部痕迹串起来。"""
        rid = ops.new_request_id()
        try:
            self._rid = rid
            self._t0 = time.monotonic()
        except AttributeError:
            pass
        return rid

    def _note(self, status=None, *, level="info"):
        """记录一条访问事件。写日志失败绝不能影响业务响应，因此整体吞异常。"""
        try:
            if status is None:
                status = getattr(self, "_last_status", 200)
            rid = getattr(self, "_rid", "-")
            t0 = getattr(self, "_t0", None)
            elapsed = round((time.monotonic() - t0) * 1000) if t0 else None
            user = _current_user(self) or {}
            EVENTS.log("http.request", level=level, rid=rid, method=self.command,
                       path=self.path.split("?")[0], status=status,
                       elapsed_ms=elapsed, user=user.get("username"))
        except Exception:
            pass

    def _audit(self, action, *, target=None, ok=True, detail=None):
        """写审计记录。actor 取当前登录用户；未登录时记为 anonymous。"""
        try:
            user = _current_user(self) or {}
            AUDIT.record(action, actor=user.get("username") or "anonymous", target=target,
                         ok=ok, detail=detail, request_id=getattr(self, "_rid", None))
        except Exception:
            pass

    def _actor(self):
        return (_current_user(self) or {}).get("username") or "anonymous"

    def do_GET(self):
        # 读取类接口此前没有兜底：任何一处未预期的异常都会让连接直接断开，
        # 浏览器只看到「网络错误」。统一包一层，返回可读的 500。
        self._begin()
        try:
            status = self._do_get()
            self._note(status)
            return status
        except ValueError as exc:
            self._note(400, level="warn")
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._note(500, level="error")
            return self._internal_error(exc)

    def _do_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._html()
        if path in {"/assets/fingerprint.js", "/assets/fingerprint.css", "/static/app.js", "/static/app.css", "/static/actions.js", "/static/tokens.css"}:
            return self._asset(path)
        if path == "/auth/status":
            user = _current_user(self)
            return self._json({"setup_required": _auth_setup_required(), "authenticated": bool(user), "user": user})
        if path == "/auth/me":
            user = _current_user(self)
            return self._json({"authenticated": bool(user), "user": user}, 200 if user else 401)
        public_paths = {"/health", "/favicon.ico"}
        if path not in public_paths and not _current_user(self):
            return self._json({"error": "请先登录"}, 401)
        if path == "/health":
            return self._json({"ok": True, "version": APP_VERSION, "started": SERVER_STARTED})
        if path == "/favicon.ico":
            # 内联图标：放在鉴权闸门之外——登录页也要显示它，而它本身不含任何业务数据。
            blob = FAVICON_SVG.encode("utf-8")
            self._last_status = 200
            self.send_response(200)
            for key, value in self._security_headers().items():
                self.send_header(key, value)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers(); self.wfile.write(blob)
            return 200
        if path in {"/fingerprints", "/fingerprint"} or path.startswith(("/fingerprints/", "/fingerprint/")):
            return self._fingerprint_get(parsed)
        if path == "/admin/accounts":
            user = _current_user(self)
            if not user or user.get("role") != "admin":
                return self._json({"error": "仅系统管理员可管理账号"}, 403)
            return self._json({"accounts": [_public_account(x) for x in _read_accounts()]})
        if path == "/tasks":
            query = urllib.parse.parse_qs(parsed.query)
            compact = query.get("summary") == ["1"]
            selected = query.get("selected", [None])[0]
            kind = query.get("kind", [None])[0]
            data = {"tasks": MANAGER.ordered(), "comparisons": COMPARISONS.ordered(),
                    "quality_tests": QUALITY.ordered(summary=compact),
                    "routing_comparisons": ROUTING_COMPARISONS.ordered(),
                    "fingerprint_runs": FINGERPRINTS.list_runs(),
                    "fingerprint_storage_warnings": list(FINGERPRINTS.storage_warnings)}
            if compact:
                for group in ("tasks", "comparisons", "quality_tests", "routing_comparisons"):
                    data[group] = [list_snapshot(item) for item in data[group]]
                managers = {"tasks": MANAGER, "comparisons": COMPARISONS,
                            "quality_tests": QUALITY, "routing_comparisons": ROUTING_COMPARISONS}
                if kind in managers and selected:
                    task = managers[kind].get(selected)
                    if task:
                        data[kind] = [task.snap() if item["id"] == selected else item for item in data[kind]]
            return self._json(data)
        if path == "/comparisons":
            return self._json({"comparisons": COMPARISONS.ordered()})
        if path == "/routing-comparisons":
            return self._json({"routing_comparisons": ROUTING_COMPARISONS.ordered(),
                               "corpus": routing_corpus.all_tier_summaries(),
                               "corpus_version": routing_corpus.CORPUS_VERSION,
                               "metrics": routing_ab.METRIC_LABELS})
        if path == "/routing-corpus":
            # 界面上的档位说明只暴露条数、段数与复用次数，绝不返回 prompt 正文：
            # 测试集正文很大，且没有任何理由出现在浏览器里。
            tier = urllib.parse.parse_qs(parsed.query).get("tier", [None])[0]
            try:
                if tier is None:
                    return self._json({"version": routing_corpus.CORPUS_VERSION,
                                       "tiers": routing_corpus.all_tier_summaries()})
                summary = routing_corpus.tier_summary(int(tier))
                return self._json({**summary, "manifest": routing_corpus.corpus_manifest(int(tier))})
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        if path == "/quality-methodology":
            return self._json(methodology_snapshot())
        if path == "/quality-suite":
            cases = load_suite()
            return self._json({"manifest": suite_manifest(cases=cases), "cases": [c.to_dict() for c in cases]})
        if path == "/quality-tests":
            return self._json({"quality_tests": QUALITY.ordered()})
        if path == "/storage":
            return self._json(storage_overview())
        if path == "/scheduler":
            # 「为什么我的任务还没开始」——把闸门状态、排队和运行中的任务一次说清楚。
            state = MANAGER.snapshot()
            def _brief(tid):
                item = MANAGER.get(tid)
                return {"id": tid, "name": item.name if item else None}
            state["waiting_tasks"] = [_brief(tid) for tid in state.get("waiting", [])]
            state["running_tasks"] = [_brief(tid) for tid in state.get("running", [])]
            return self._json(state)
        if path == "/templates":
            return self._json({"templates": TEMPLATES.list()})
        if path == "/audit":
            limit = int(urllib.parse.parse_qs(parsed.query).get("limit", ["100"])[0])
            return self._json({"records": AUDIT.recent(limit=max(1, min(limit, 500)))})
        if path == "/events":
            limit = int(urllib.parse.parse_qs(parsed.query).get("limit", ["200"])[0])
            return self._json({"events": EVENTS.tail(limit=max(1, min(limit, 1000))), "stats": EVENTS.stats()})
        if path == "/preflight":
            # 发起前的可行性预检：不碰上游、不读密钥，只用配置本身推算请求量、
            # 时长、磁盘占用和并发是否够用，并把「打到生产环境」标成需要确认的风险。
            kind = urllib.parse.parse_qs(parsed.query).get("kind", ["run"])[0]
            cfg = read_saved_config()
            try:
                if kind == "quality":
                    cases = None
                    try:
                        cases = len(load_suite())
                    except Exception:
                        cases = None
                    return self._json(estimate_quality_run(cfg, case_count=cases))
                return self._json(estimate_run(cfg))
            except (KeyError, TypeError, ValueError) as exc:
                # 历史配置里可能有字符串型数值；预检失败不能阻断页面，降级返回可读原因。
                return self._json({"level": "ok", "findings": [], "requires_confirmation": False,
                                   "unavailable": f"配置尚不足以预检：{exc}"})
        if path == "/quality-reference-records":
            records = []
            for item in QUALITY.ordered():
                if item.get("status") not in ("finished", "finished_with_errors") or len(item.get("resources") or []) != 1:
                    continue
                report = item.get("report") or {}
                resource = (item.get("resources") or [{}])[0]
                manifest = (item.get("config") or {}).get("suite_manifest") or {}
                records.append({"id": item.get("id"), "name": item.get("name"), "created": item.get("created"), "status": item.get("status"), "model": resource.get("model"), "score_1000": (report.get("resources") or [{}])[0].get("score_1000"), "score_pct": (report.get("resources") or [{}])[0].get("score_pct"), "case_count": item.get("case_count"), "suite_fingerprint": manifest.get("fingerprint"), "eligible": bool(report.get("evidence", {}).get("complete"))})
            return self._json({"records": records})
        if path.startswith("/quality-report/"):
            quality = QUALITY.get(path.split("/")[-1])
            if not quality:
                return self._json({"error": "质量测试不存在"}, 404)
            snapshot = quality.snap()
            if parsed.query == "format=markdown":
                raw = render_quality_markdown(snapshot).encode("utf-8")
                self._last_status = 200
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="quality-report-{quality.id}.md"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            return self._json(snapshot, headers={"Content-Disposition": f'attachment; filename="quality-report-{quality.id}.json"'})
        if path.startswith("/quality-test/"):
            quality = QUALITY.get(path.split("/")[-1])
            return self._json(quality.snap() if quality else {"error": "质量测试不存在"}, 200 if quality else 404)
        if path.startswith("/comparison/"):
            run = COMPARISONS.get(path.split("/")[-1])
            return self._json(run.snap() if run else {"error": "对比实验不存在"}, 200 if run else 404)
        if path.startswith("/routing-comparison/"):
            run = ROUTING_COMPARISONS.get(path.split("/")[-1])
            return self._json(run.snap() if run else {"error": "路由对照不存在"}, 200 if run else 404)
        if path.startswith("/task/") and "/record/" in path:
            parts = path.split("/")
            if len(parts) != 5 or not parts[2] or not parts[4].isdigit():
                return self._json({"error": "原始记录地址无效"}, 400)
            task = MANAGER.get(parts[2])
            if not task:
                return self._json({"error": "任务不存在"}, 404)
            if not task.config.get("capture_raw"):
                return self._json({"error": "该任务未开启原始数据记录"}, 400)
            if not task.out_path or not os.path.exists(task.out_path):
                return self._json({"error": "结果文件不存在"}, 404)
            wanted = int(parts[4])
            try:
                with open(task.out_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if item.get("item_id") == wanted:
                            return self._json(item)
            except OSError as exc:
                return self._internal_error(exc)
            return self._json({"error": "记录不存在"}, 404)
        if path.startswith("/task/"):
            task = MANAGER.get(path.split("/")[-1])
            return self._json(task.snap() if task else {"error": "任务不存在"}, 200 if task else 404)
        if path == "/credential-status":
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
            key = _load_api_key(url)
            return self._json({"saved": bool(key), "hint": _api_key_hint(key), "storage_available": _keychain_available()})
        if path == "/config":
            return self._json({"config": read_saved_config()})
        if path.startswith("/download/"):
            task = MANAGER.get(path.split("/")[-1])
            if not task or not task.out_path or not os.path.exists(task.out_path):
                return self._json({"error": "结果文件不存在"}, 404)
            size = os.path.getsize(task.out_path)
            self._last_status = 200
            self.send_response(200)
            for key, value in self._security_headers().items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(task.out_path)}"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(task.out_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self._begin()
        try:
            if not self._same_origin():
                return self._json({"error": "已拒绝跨站状态变更请求"}, 403)
            result = self._do_post(parsed, path)
            self._note()
            return result
        except ValueError as exc:
            self._note(400, level="warn")
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._note(500, level="error")
            return self._internal_error(exc)

    def _do_post(self, parsed, path):
        try:
            body = self._body()
            if not isinstance(body, dict):
                raise ValueError("请求内容必须为对象")
            if path == "/auth/setup":
                if not _auth_setup_required():
                    return self._json({"error": "管理员账号已经初始化"}, 409)
                username = str(body.get("username") or "").strip()
                password = str(body.get("password") or "")
                if len(username) < 3 or len(username) > 40:
                    raise ValueError("管理员账号长度应为 3 到 40 个字符")
                if len(password) < 8:
                    raise ValueError("管理员密码至少需要 8 个字符")
                salt, digest = _password_hash(password)
                account = {"username": username, "role": "admin", "salt": salt, "password_hash": digest, "enabled": True, "created_at": time.time()}
                _write_accounts([account])
                token = _new_session(username)
                self._audit("auth.setup", target=username, detail="初始化管理员")
                return self._json({"ok": True, "user": _public_account(account)}, headers=_session_headers(token))
            if path == "/auth/login":
                retry_after = _login_rate_status(self)
                if retry_after is not None:
                    return self._json(
                        {"error": "登录失败次数过多，请稍后再试"},
                        429,
                        headers={"Retry-After": str(retry_after)},
                    )
                username = str(body.get("username") or "").strip()
                password = str(body.get("password") or "")
                account = next((x for x in _read_accounts() if x.get("username") == username and x.get("enabled", True)), None)
                if not account or not _check_password(password, account):
                    _record_login_failure(self)
                    self._audit("auth.login_failed", target=username, ok=False, detail=_client_ip(self))
                    return self._json({"error": "账号或密码错误"}, 401)
                _clear_login_failures(self)
                token = _new_session(username)
                self._audit("auth.login", target=username, detail=_client_ip(self))
                return self._json({"ok": True, "user": _public_account(account)}, headers=_session_headers(token))
            if path == "/auth/logout":
                cookie = SimpleCookie(); cookie.load(self.headers.get("Cookie", ""))
                token = cookie.get(SESSION_COOKIE)
                if token:
                    with AUTH_LOCK: SESSIONS.pop(token.value, None)
                self._audit("auth.logout")
                return self._json({"ok": True}, headers=_session_headers(clear=True))
            user = _current_user(self)
            if path not in ("/health",) and not user:
                return self._json({"error": "请先登录"}, 401)
            if path in {"/fingerprints", "/fingerprint"} or path.startswith(("/fingerprints/", "/fingerprint/")):
                return self._fingerprint_post(path, body)
            if path == "/admin/accounts":
                if user.get("role") != "admin":
                    return self._json({"error": "仅系统管理员可管理账号"}, 403)
                action = parsed.query
                if action.startswith("delete="):
                    target = urllib.parse.unquote(action.split("=", 1)[1])
                    accounts = _read_accounts()
                    if target == user.get("username"):
                        return self._json({"error": "不能删除当前登录的管理员账号"}, 400)
                    next_accounts = [x for x in accounts if x.get("username") != target]
                    if len(next_accounts) == len(accounts):
                        return self._json({"error": "账号不存在"}, 404)
                    SESSIONS.revoke_user(target)
                    _write_accounts(next_accounts)
                    self._audit("account.delete", target=target)
                    return self._json({"ok": True})
                username = str(body.get("username") or "").strip()
                password = str(body.get("password") or "")
                role = str(body.get("role") or "user").strip().lower()
                if len(username) < 3 or len(password) < 8 or role not in ("user", "admin"):
                    raise ValueError("账号至少 3 个字符，密码至少 8 个字符，角色只能是 user（使用者）或 admin（管理员）")
                accounts = _read_accounts()
                if any(x.get("username") == username for x in accounts):
                    return self._json({"error": "账号已存在"}, 409)
                salt, digest = _password_hash(password)
                account = {"username": username, "role": role, "salt": salt, "password_hash": digest, "enabled": True, "created_at": time.time()}
                _write_accounts(accounts + [account])
                self._audit("account.create", target=username, detail=role)
                return self._json({"ok": True, "account": _public_account(account)})
            if path == "/models":
                url = str(body.get("url") or "").strip()
                key = str(body.get("api_key") or "").strip()
                # 前端不会回填已保存的真实密钥；模型刷新时按 URL 从 macOS Keychain（钥匙串）复用。
                if not key:
                    key = _load_api_key(url)
                if not url or not key:
                    raise ValueError("需要网关 URL 和 API Key；也可以先保存密钥后留空复用")
                result = fetch_models_detailed_sync(url, {"Authorization": f"Bearer {key}"})
                if not result["ok"]:
                    return self._json({"ok": False, "models": [], "error": result["error"]}, 502)
                saved = _save_api_key(url, key)
                return self._json({"ok": True, "models": result["models"], "attempted": result["attempted"], "saved": saved, "hint": _api_key_hint(key)})
            if path == "/preflight":
                # 对「表单里的当前草稿」预检，而不是上一次保存的配置：
                # 用户改了目标地址或题量能立刻看到风险变化，不需要先提交一次。
                # 这里只做估算，不落盘、不读密钥之外的状态，失败一律降级。
                kind = str(body.get("kind") or "run")
                draft = body.get("config")
                if not isinstance(draft, dict):
                    raise ValueError("preflight 需要携带 config 对象")
                # 抽屉刚打开时草稿是空的（没有 URL / 模型），这里必须返回可读的
                # 「配置尚不足以预检」，而不是 400——否则每打开一次新建面板
                # 浏览器就记一条失败请求，真正的故障反而被噪声淹没。
                # 校验与降级都在 preflight_or_degrade 里，这里只负责取草稿。
                return self._json(preflight_or_degrade(draft, kind=kind))
            if path == "/tasks":
                # 预检闸门：生产类目标必须显式确认，避免脚本绕过页面的确认弹窗。
                blocked = require_risk_confirmation(validate_config(body, require_secret=False), body, kind="run")
                if blocked:
                    return self._json({"error": blocked, "requires_confirmation": True}, 400)
                task = MANAGER.add(body)
                save_config(body)
                self._audit("task.create", target=task.id, detail=body.get("mode"))
                return self._json({"ok": True, "task_id": task.id}, 201)
            if path == "/comparisons" and not parsed.query:
                run = COMPARISONS.create(body)
                save_config(body.get("shared_config") or {})
                return self._json({"ok": True, "comparison_id": run.id}, 201)
            if path == "/routing-comparisons" and not parsed.query:
                # 路由对照同样受预检闸门约束：它会真实打到两条路由，可能产生费用。
                draft = routing_compare.validate_config(body)
                blocked = require_risk_confirmation(draft, body, kind="routing")
                if blocked:
                    return self._json({"error": blocked, "requires_confirmation": True}, 400)
                run = ROUTING_COMPARISONS.create(body)
                # 两侧的地址与模型回填到常规表单，方便用户接着发起普通压测做交叉验证；
                # 密钥不落常规配置（save_config 会在写入前剔除密钥字段）。
                save_config({"url": draft["baseline"]["url"], "model": draft["baseline"]["model"]})
                self._audit("routing_compare.create", target=run.id, detail=draft["tier"])
                persist_history()
                return self._json({"ok": True, "routing_comparison_id": run.id}, 201)
            if path.startswith("/quality-test/") and parsed.query == "stop":
                # 停止语义统一走 QualityManager.stop：排队中直接判定已停止，
                # 运行中先进入「停止中」，避免界面在收尾期间仍显示「运行中」。
                error = QUALITY.stop(path.split("/")[-1])
                return self._json({"ok": not error, "error": error}, 200 if not error else 400)
            if path == "/quality-tests" and not parsed.query:
                # 质量评测同样受预检闸门约束：双资源评测极易打到线上模型并产生费用。
                draft = validate_quality_config(body)
                blocked = require_risk_confirmation(draft, body, kind="quality")
                if blocked:
                    return self._json({"error": blocked, "requires_confirmation": True}, 400)
                quality = QUALITY.create(body)
                return self._json({"ok": True, "quality_test_id": quality.id}, 201)
            if path == "/comparisons" and parsed.query.startswith("delete="):
                target = urllib.parse.unquote(parsed.query.split("=", 1)[1])
                error = COMPARISONS.delete(target)
                self._audit("comparison.delete", target=target, ok=not error, detail=error)
                return self._json({"ok": not error, "error": error}, 200 if not error else 400)
            if path == "/routing-comparisons" and parsed.query.startswith("delete="):
                target = urllib.parse.unquote(parsed.query.split("=", 1)[1])
                error = ROUTING_COMPARISONS.delete(target)
                self._audit("routing_compare.delete", target=target, ok=not error, detail=error)
                persist_history()
                return self._json({"ok": not error, "error": error}, 200 if not error else 400)
            if path == "/routing-comparisons" and parsed.query.startswith("stop="):
                target = urllib.parse.unquote(parsed.query.split("=", 1)[1])
                error = ROUTING_COMPARISONS.stop(target)
                self._audit("routing_compare.stop", target=target, ok=not error, detail=error)
                return self._json({"ok": not error, "error": error}, 200 if not error else 400)
            if path == "/quality-tests" and parsed.query.startswith("delete="):
                target = urllib.parse.unquote(parsed.query.split("=", 1)[1])
                error = QUALITY.delete(target)
                self._audit("quality.delete", target=target, ok=not error, detail=error)
                return self._json({"ok": not error, "error": error}, 200 if not error else 400)
            if path.startswith("/task/"):
                task_id = path.split("/")[-1]
                action = parsed.query
                if action == "stop":
                    error = MANAGER.stop(task_id)
                    self._audit("task.stop", target=task_id, ok=not error, detail=error)
                    return self._json({"ok": not error, "error": error}, 200 if not error else 400)
                if action == "delete":
                    error = MANAGER.delete(task_id)
                    self._audit("task.delete", target=task_id, ok=not error, detail=error)
                    return self._json({"ok": not error, "error": error}, 200 if not error else 400)
                if action == "retry":
                    old = MANAGER.get(task_id)
                    if not old:
                        raise ValueError("任务不存在")
                    cfg = dict(old.config)
                    if body.get("api_key"):
                        cfg["api_key"] = str(body["api_key"]).strip()
                    task = MANAGER.add(cfg)
                    self._audit("task.retry", target=task.id, detail=f"来源 {task_id}")
                    return self._json({"ok": True, "task_id": task.id}, 201)
            if path == "/templates":
                saved = TEMPLATES.save(body)
                self._audit("template.save", target=saved.get("id"), detail=saved.get("name"))
                return self._json({"ok": True, "template": saved}, 201)
            if path.startswith("/templates/") and parsed.query == "delete":
                template_id = urllib.parse.unquote(path.split("/")[-1])
                TEMPLATES.delete(template_id)
                self._audit("template.delete", target=template_id)
                return self._json({"ok": True})
            if path == "/settings/concurrency":
                if user.get("role") != "admin":
                    return self._json({"error": "仅系统管理员可调整并发上限"}, 403)
                limit = int(body.get("limit") or 0)
                if not 1 <= limit <= 16:
                    raise ValueError("并发上限应为 1 到 16 之间的整数")
                previous = LIMITER.snapshot()
                LIMITER.set_limit(limit)
                self._audit("settings.concurrency", target=str(limit),
                            detail=f"{previous.get('active')} 个运行中")
                return self._json({"ok": True, "scheduler": MANAGER.snapshot()})
            if path == "/storage/cleanup":
                # 只清理没有任何记录引用的孤儿文件；被引用的结果文件必须通过删除对应记录来删除，
                # 这样“删文件”与“删记录”永远是同一个可审计的动作。
                referenced = referenced_artifacts()
                older_than = float(body.get("older_than_seconds") or 0)
                result = storage.cleanup_orphans(BASE_DIR, referenced, older_than_seconds=older_than)
                persist_history()
                self._audit("storage.cleanup", detail=f"删除 {result.get('deleted')} 个文件 / {result.get('freed_bytes', 0)} 字节")
                return self._json({"ok": True, **result, "storage": storage_overview()})
            if path == "/config":
                cfg = dict(body)
                if cfg.get("clear_api_key"):
                    _delete_api_key(cfg.get("url"))
                save_config(cfg)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._internal_error(exc)


def read_saved_config(*, include_credential=True):
    try:
        with open(TASKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cfg = data.get("last_config", {}) if isinstance(data, dict) else {}
        if isinstance(cfg, dict):
            result = public_config(cfg)
            key = _load_api_key(result.get("url")) if include_credential else ""
            if key:
                result["api_key_saved"] = True
                result["api_key_hint"] = _api_key_hint(key)
            return result
    except (OSError, ValueError):
        pass
    return {}


CONFIG_SAVE_LOCK = threading.Lock()

def save_config(cfg):
    raw = dict(cfg or {})
    if raw.get("url") and raw.get("api_key"):
        _save_api_key(raw.get("url"), raw.get("api_key"))
    cfg = public_config(raw)
    temp = f"{TASKS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with CONFIG_SAVE_LOCK:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump({"last_config": cfg}, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, TASKS_FILE)
    try:
        os.chmod(TASKS_FILE, 0o600)
    except OSError:
        pass




def _history_safe_snapshot(value):
    """生成可持久化的历史摘要：不保存密钥，也不把原始正文复制进索引。"""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if _is_secret_field(key) or normalized in {"raw_input", "raw_output", "response"}:
                continue
            clean[key] = _history_safe_snapshot(item)
        return clean
    if isinstance(value, list):
        return [_history_safe_snapshot(item) for item in value]
    if isinstance(value, tuple):
        return [_history_safe_snapshot(item) for item in value]
    return value


def _history_write(entries):
    HistoryStore(HISTORY_FILE).write(entries)


def referenced_artifacts():
    """收集仍被任务引用的结果文件名 → 归属描述。

    结果文件可能很大且长期累积，必须能回答两个问题：
    哪些文件还被记录引用（不能删），哪些已经是孤儿（可以清理）。
    对照实验的子任务也在这里登记，避免它们的文件被误判成孤儿。
    """
    referenced = {}
    for task in MANAGER.tasks.values():
        path = task.artifact_path or task.out_path
        if path:
            referenced[os.path.basename(path)] = f"压测任务「{task.name}」"
    for task in QUALITY.tasks.values():
        path = task.artifact_path or task.out_path
        if path:
            referenced[os.path.basename(path)] = f"质量评测「{task.name}」"
    for run in ROUTING_COMPARISONS.runs.values():
        for path in run.out_paths.values():
            if path:
                referenced[os.path.basename(path)] = f"路由对照「{run.name}」"
    return referenced


def storage_overview():
    return storage.storage_summary(BASE_DIR, referenced_artifacts())


HISTORY_SNAPSHOT_LOCK = threading.RLock()


def persist_history():
    """Serialize snapshot collection and commit so older writes cannot overwrite newer state."""
    with HISTORY_SNAPSHOT_LOCK:
        try:
            tasks = [_history_safe_snapshot(item) for item in MANAGER.ordered()]
            comparisons = [_history_safe_snapshot(item) for item in COMPARISONS.ordered()]
            quality_tests = [quality_index(_history_safe_snapshot(item)) for item in QUALITY.ordered(summary=True)]
            # 路由对照的摘要只带清单指纹与两侧地址，不带 prompt 正文，也不带密钥：
            # 正文是批量合成文本，写进索引只会把每次启动都拖慢。
            routing_comparisons = [_routing_history_snapshot(item) for item in ROUTING_COMPARISONS.ordered()]
            _history_write({"tasks": tasks, "comparisons": comparisons, "quality_tests": quality_tests,
                            "routing_comparisons": routing_comparisons})
        except Exception as exc:
            # Evidence persists independently; index errors must be diagnosable.
            EVENTS.log("history.write_failed", level="error", error_type=type(exc).__name__)
            return


def _routing_history_snapshot(snapshot):
    """路由对照的历史摘要：只保留能重建「这次比的是什么」的最小字段。"""
    def side(item):
        return {"name": item.get("name"), "url": item.get("url"), "model": item.get("model"),
                "records": item.get("records")}

    return {
        "id": snapshot.get("id"),
        "name": snapshot.get("name"),
        "status": snapshot.get("status"),
        "error": snapshot.get("error"),
        "created": snapshot.get("created"),
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "tier": snapshot.get("tier"),
        "rpm": snapshot.get("rpm"),
        "concurrency": snapshot.get("concurrency"),
        "config": {"capture_raw": bool((snapshot.get("config") or {}).get("capture_raw"))},
        "corpus": snapshot.get("corpus"),
        "sides": {key: side(value) for key, value in (snapshot.get("sides") or {}).items()},
        # 报告整体保留：它是这条记录存在的理由，且本身不含密钥与正文。
        "report": snapshot.get("report"),
        "limits": snapshot.get("limits"),
    }


def routing_result_paths(run_id):
    """结果文件路径的唯一来源在执行器里，接口层只做转发，避免两处各写一遍格式。"""
    return routing_compare.result_paths(run_id)


def _restore_routing_comparison(snapshot):
    run = routing_compare.RouteComparison.from_snapshot(snapshot)
    with ROUTING_COMPARISONS.lock:
        ROUTING_COMPARISONS.runs[run.id] = run
    return run


def _history_config(snapshot, fallback=None):
    """将公共摘要补齐为只读历史任务需要的最小配置。"""
    cfg = dict(fallback or {})
    cfg.update(snapshot.get("config") or {})
    cfg.pop("api_key", None)
    cfg.setdefault("name", snapshot.get("name") or "历史测试")
    cfg.setdefault("url", snapshot.get("target_url") or "")
    cfg.setdefault("model", snapshot.get("model") or "")
    cfg.setdefault("mode", "smoke")
    cfg.setdefault("profile", "smoke")
    cfg.setdefault("rpm", 600.0)
    cfg.setdefault("iterations", max(1, int(snapshot.get("total") or 1)))
    cfg.setdefault("duration", max(1, int(round(snapshot.get("planned_duration") or 1))))
    cfg.setdefault("concurrency", 1)
    cfg.setdefault("connect_timeout", 10.0)
    cfg.setdefault("read_timeout", 60.0)
    cfg.setdefault("write_timeout", 30.0)
    cfg.setdefault("stream", True)
    cfg.setdefault("capture_raw", False)
    cfg.setdefault("http2", False)
    cfg.setdefault("insecure", False)
    cfg.setdefault("ramps", "")
    # 历史摘要来自旧版本表单，数值字段可能是字符串；恢复只读任务前统一归一化，
    # 避免 build_plan（计划构建）在服务启动阶段因类型不一致中断整个工作台。
    def _history_float(name, default, minimum=None):
        try:
            value = float(cfg.get(name, default))
        except (TypeError, ValueError):
            value = float(default)
        if minimum is not None:
            value = max(float(minimum), value)
        return value
    def _history_int(name, default, minimum=None):
        try:
            value = int(float(cfg.get(name, default)))
        except (TypeError, ValueError):
            value = int(default)
        if minimum is not None:
            value = max(int(minimum), value)
        return value
    cfg["rpm"] = _history_float("rpm", 600.0, 0.01)
    cfg["concurrency"] = _history_int("concurrency", 1, 1)
    cfg["iterations"] = _history_int("iterations", 1, 1)
    cfg["duration"] = _history_int("duration", 1, 1)
    cfg["connect_timeout"] = _history_float("connect_timeout", 10.0, 0.1)
    cfg["read_timeout"] = _history_float("read_timeout", 60.0, 0.1)
    cfg["write_timeout"] = _history_float("write_timeout", 30.0, 0.1)
    return cfg


def _restore_task_snapshot(snapshot, out_path=None):
    """从历史摘要恢复一个只读 Task（任务）对象，不恢复执行状态和密钥。"""
    cfg = _history_config(snapshot)
    try:
        steps, total = build_plan(cfg)
    except (KeyError, ValueError):
        cfg["mode"] = "smoke"
        cfg["iterations"] = max(1, int(snapshot.get("total") or 1))
        cfg["rpm"] = max(0.01, float(cfg.get("rpm") or 600))
        steps, total = build_plan(cfg)
    task = Task(cfg, steps, int(snapshot.get("total") or total))
    task.id = str(snapshot.get("id") or task.id)
    task.name = str(snapshot.get("name") or task.name)
    task.status = str(snapshot.get("status") or "finished")
    if task.status in ("running", "waiting", "stopping"):
        task.status = "stopped"
        task.error = "服务在任务完成前重启，历史记录未恢复执行。"
    else:
        task.error = snapshot.get("error")
    task.created = float(snapshot.get("created") or time.time())
    task.started_at = snapshot.get("started_at")
    task.finished_at = snapshot.get("finished_at") or task.created
    task.stopped_at = snapshot.get("stopped_at")
    task.total = int(snapshot.get("total") or total)
    task.scheduled = int(snapshot.get("scheduled") or task.total)
    task.started = int(snapshot.get("started") or snapshot.get("done") or 0)
    task.done = int(snapshot.get("done") or 0)
    task.dropped = int(snapshot.get("dropped") or max(0, task.total - task.done))
    task.queue_depth = 0
    task.out_path = out_path or snapshot.get("out_path")
    if task.out_path:
        name = Path(task.out_path).name
        relocated = Path(BASE_DIR) / name
        if name.startswith("run_") and name.endswith(".jsonl") and relocated.is_file():
            task.out_path = str(relocated)
    task.history_only = True
    # 结果文件是历史记录的唯一证据。启动时核对一次：文件还在就登记容量与校验值，
    # 文件已被手动删除或移动就标记为「结果已过期」，列表里直接显示，而不是点开才报错。
    task.artifact_path = task.out_path
    task.artifact_state = "unknown"
    restored_artifact = snapshot.get("artifact") or {}
    # 启动恢复要遍历全部历史，不能对每个结果文件都做一次全量 sha256（大文件很贵）。
    # 先用文件大小判断：大小一致才值得算哈希；大小不一致直接判为被外部改动。
    recorded_bytes = restored_artifact.get("bytes")
    task.refresh_artifact(verify=False)
    if task.artifact_state == "missing":
        task.mark_artifact_expired("结果文件已不存在（可能已被手动删除或移动）")
    elif recorded_bytes is not None and task.artifact_bytes != recorded_bytes:
        task.mark_artifact_expired("结果文件大小与索引记录不一致，可能被外部修改")
    elif restored_artifact.get("sha256"):
        task.refresh_artifact(verify=True)
        if task.artifact_sha256 != restored_artifact.get("sha256"):
            task.mark_artifact_expired("结果文件内容与索引记录的校验值不一致，可能被外部修改")
    task.parent_comparison_id = snapshot.get("parent_comparison_id")
    task.parent_group_id = snapshot.get("parent_group_id")
    task._history_snapshot = _history_safe_snapshot(snapshot)
    task._history_snapshot["out_path"] = task.out_path
    task.public_config = public_config(cfg)
    return task


def _classify_history_result(result):
    status = result.get("status_code") or 0
    error = result.get("error_type")
    completed = bool(result.get("completed"))
    if 200 <= status < 300 and completed:
        return "ok", "成功", "ok"
    if error == "TIMEOUT" or status == 408:
        return "timeout", "超时", "timeout"
    if error == "NETWORK" or (status == 0 and error != "TIMEOUT"):
        return "network", "网络错误", "network"
    if error == "PROTOCOL" or (200 <= status < 300 and not completed):
        return "protocol", "响应不完整", "protocol"
    if status == 429 or error == "HTTP_429":
        return "r429", "限流", "r429"
    if 500 <= status < 600 or error == "HTTP_5XX":
        return "r5xx", "服务端错误", "r5xx"
    return "r4xx", "客户端错误", "r4xx"


def _legacy_task_from_file(path, fallback_cfg):
    """流式扫描旧版 JSONL（逐行 JSON）结果文件，绝不把原始正文批量读入内存。"""
    counters = {key: 0 for key in COUNTER_KEYS}
    ttft = []
    total_ms = []
    lag = []
    buckets = {}
    recent = deque(maxlen=100)
    first_ts = last_ts = None
    profile = None
    mode = None
    model = None
    target_url = None
    capture_raw = False
    done = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    result = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(result, dict):
                    continue
                done += 1
                category, label, css = _classify_history_result(result)
                counters[category] += 1
                if result.get("ttft_ms") is not None:
                    try: ttft.append(float(result["ttft_ms"]))
                    except (TypeError, ValueError): pass
                if result.get("total_ms") is not None:
                    try: total_ms.append(float(result["total_ms"]))
                    except (TypeError, ValueError): pass
                if result.get("schedule_lag_ms") is not None:
                    try: lag.append(float(result["schedule_lag_ms"]))
                    except (TypeError, ValueError): pass
                ts = result.get("ts")
                if isinstance(ts, (int, float)):
                    first_ts = ts if first_ts is None else min(first_ts, ts)
                    last_ts = ts if last_ts is None else max(last_ts, ts)
                    bucket = int(ts // 1000)
                    bucket_data = buckets.setdefault(bucket, {"count": 0, "ok": 0, "failed": 0})
                    bucket_data["count"] += 1
                    if category == "ok": bucket_data["ok"] += 1
                    else: bucket_data["failed"] += 1
                profile = profile or result.get("profile")
                mode = mode or result.get("mode")
                model = model or result.get("model")
                target_url = target_url or result.get("url") or result.get("target_url")
                capture_raw = capture_raw or "raw_input" in result or "raw_output" in result
                recent.append({"id": result.get("item_id"), "step": result.get("step_index"),
                               "status": result.get("status_code") or 0, "cat": category, "cls": css,
                               "label": label, "ttft": result.get("ttft_ms"), "total": result.get("total_ms"),
                               "reason": result.get("reason")})
    except (OSError, UnicodeError):
        return None
    if not done:
        return None
    try:
        stat = os.stat(path)
        created = stat.st_ctime
    except OSError:
        created = time.time()
    if first_ts is not None:
        started_at = first_ts / 1000
        finished_at = (last_ts or first_ts) / 1000
    else:
        started_at = created
        finished_at = created
    elapsed = max(0.001, finished_at - started_at)
    cfg = dict(fallback_cfg or {})
    cfg.update({"mode": mode or cfg.get("mode", "smoke"), "profile": profile or cfg.get("profile", "smoke"),
                "iterations": done, "capture_raw": capture_raw})
    cfg["api_key"] = ""
    cfg.setdefault("name", "")
    cfg.setdefault("model", model or "")
    cfg.setdefault("url", target_url or "")
    # 旧历史索引中的表单字段可能以字符串保存；恢复历史时必须先转成数值，
    # 否则启动服务会在容量估算阶段直接崩溃，导致整个工作台无法打开。
    try:
        legacy_concurrency = max(1, float(cfg.get("concurrency", 1) or 1))
    except (TypeError, ValueError):
        legacy_concurrency = 1.0
    try:
        legacy_rpm = max(0.0, float(cfg.get("rpm", 0) or 0))
    except (TypeError, ValueError):
        legacy_rpm = 0.0
    task_id = Path(path).stem.rsplit("_", 1)[-1]
    if len(task_id) < 8:
        task_id = uuid.uuid4().hex[:12]
    name = cfg.get("name") or f"历史结果 · {time.strftime('%m-%d %H:%M:%S', time.localtime(created))}"
    avg = sum(total_ms) / len(total_ms) if total_ms else None
    timeline = []
    for second in sorted(buckets):
        item = buckets[second]
        count = item["count"]
        timeline.append({"t": second * 1000, "ok": item["ok"], "r429": 0, "r5xx": 0, "r4xx": item["failed"],
                         "timeout": 0, "network": 0, "protocol": 0, "rpm": count * 60,
                         "target_rpm": legacy_rpm, "step": 1, "queue": 0})
    recent = list(reversed(recent))
    snapshot = {
        "id": task_id, "name": name, "status": "finished", "error": None,
        "mode": {"smoke": "冒烟验证", "ramp": "阶梯加压", "soak": "稳定性长跑"}.get(cfg["mode"], cfg["mode"]), "concurrency": legacy_concurrency,
        "model": cfg.get("model") or "历史结果（模型未记录）", "target_url": cfg.get("url") or "历史结果（地址未记录）",
        "config": public_config(cfg), "total": done, "scheduled": done, "started": done, "done": done,
        "progress": 1, "remaining": 0, "pending": 0, "inflight": 0, "dropped": 0, "queue_depth": 0,
        "cur_step": 1, "target_rpm": float(cfg.get("rpm") or 0), "schedule_step": 1,
        "schedule_target_rpm": float(cfg.get("rpm") or 0), "counters": counters,
        "success_pct": counters["ok"] * 100 / done if done else None,
        "ttft": {"n": len(ttft), "p50": percentile(ttft, .5), "p95": percentile(ttft, .95)},
        "total_ms": {"n": len(total_ms), "p50": percentile(total_ms, .5), "p95": percentile(total_ms, .95), "avg": avg},
        "rpm": {"live": 0, "avg": done * 60 / elapsed, "capacity_estimate": (legacy_concurrency * 60000 / avg) if avg else None},
        "schedule_lag": {"p50": percentile(lag, .5), "p95": percentile(lag, .95)},
        "phase": f"历史结果 · 已完成全部 {done} 条请求", "recent": list(reversed(recent)),
        "timeline": timeline, "out_path": path, "write_error": None, "created": created,
        "started_at": started_at, "finished_at": finished_at, "stopped_at": None, "elapsed": elapsed,
        "planned_duration": elapsed, "planned_count": done, "history_only": True,
    }
    return _restore_task_snapshot(snapshot, out_path=path)


def restore_history():
    """服务启动时恢复历史和质量证据；绝不自动继续执行中断的任务。"""
    entries = HistoryStore(HISTORY_FILE).read()
    for snapshot in entries.get("tasks", []) if isinstance(entries.get("tasks", []), list) else []:
        if not isinstance(snapshot, dict):
            continue
        task = _restore_task_snapshot(snapshot)
        if task.id:
            with MANAGER.lock:
                MANAGER.tasks[task.id] = task
    # 兼容历史索引缺失的升级前结果文件。
    fallback_cfg = read_saved_config(include_credential=False)
    known_paths = {str(item.out_path) for item in MANAGER.tasks.values() if item.out_path}
    for path in sorted(Path(BASE_DIR).glob("run_*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        if str(path) in known_paths:
            continue
        task = _legacy_task_from_file(str(path), fallback_cfg)
        if task is None or task.id in MANAGER.tasks:
            continue
        with MANAGER.lock:
            MANAGER.tasks[task.id] = task
    for snapshot in entries.get("comparisons", []) if isinstance(entries.get("comparisons", []), list) else []:
        if not isinstance(snapshot, dict):
            continue
        run = ComparisonRun.__new__(ComparisonRun)
        run.id = str(snapshot.get("id") or uuid.uuid4().hex[:12]); run.name = str(snapshot.get("name") or "历史对比实验")
        run.status = snapshot.get("status") if snapshot.get("status") not in ("running", "waiting") else "error"
        run.created = float(snapshot.get("created") or time.time()); run.started_at = snapshot.get("started_at"); run.finished_at = snapshot.get("finished_at")
        run.group_results = list(snapshot.get("groups") or []); run.lock = threading.RLock(); run.history_only = True; run._history_snapshot = _history_safe_snapshot(snapshot)
        run.current_group_id = None; run.current_group_name = None
        restored_groups = []
        for item in run.group_results:
            if not isinstance(item, dict):
                continue
            restored_groups.append({key: item.get(key) for key in ("id", "name", "role", "concurrency", "rpm", "iterations") if item.get(key) is not None})
            # 子任务可能从旧结果文件恢复；还原归属，避免重启后混入单次压测列表。
            child = MANAGER.get(item.get("task_id"))
            if child is not None:
                child.parent_comparison_id = run.id
                child.parent_group_id = item.get("id")
        run.config = {"shared_config": dict(snapshot.get("config") or {}), "groups": restored_groups, "execution_policy": snapshot.get("execution_policy") or "serial"}
        with COMPARISONS.lock: COMPARISONS.runs[run.id] = run
    for snapshot in entries.get("quality_tests", []) if isinstance(entries.get("quality_tests", []), list) else []:
        if not isinstance(snapshot, dict):
            continue
        task_id = str(snapshot.get("id") or uuid.uuid4().hex[:12])
        artifact = os.path.join(BASE_DIR, f"quality_run_{task_id}.json")
        if Path(artifact).parent == Path(BASE_DIR):
            try:
                with open(artifact, encoding="utf-8") as source:
                    evidence = json.load(source)
                if isinstance(evidence, dict) and evidence.get("id") == task_id:
                    snapshot = evidence
            except (OSError, ValueError):
                pass
        config = dict(snapshot.get("config") or {})
        config["name"] = snapshot.get("name") or "历史质量测试"
        config["case_count"] = snapshot.get("case_count") or config.get("case_count") or 0
        task = QualityTask(config)
        task.id = task_id
        task.out_path = task.artifact_path = artifact
        task.artifact_state = "unknown"
        restored_artifact = snapshot.get("artifact") or {}
        recorded_bytes = restored_artifact.get("bytes")
        task.refresh_artifact(verify=False)
        if task.artifact_state == "missing":
            # 结果是证据链的一部分：文件不在就明确标成「结果已过期」，不让结论看起来仍可复核。
            task.mark_artifact_expired("评测证据文件已不存在（可能已被手动删除或移动）")
        elif recorded_bytes is not None and task.artifact_bytes != recorded_bytes:
            task.mark_artifact_expired("评测证据文件大小与索引记录不一致，可能被外部修改")
        elif restored_artifact.get("sha256"):
            task.refresh_artifact(verify=True)
            if task.artifact_sha256 != restored_artifact.get("sha256"):
                task.mark_artifact_expired("评测证据文件内容与索引记录的校验值不一致，可能被外部修改")
        interrupted = snapshot.get("status") in ("running", "waiting", "stopping")
        task.status = "error" if interrupted else snapshot.get("status", "error")
        task.created = float(snapshot.get("created") or time.time())
        task.started_at = snapshot.get("started_at")
        task.finished_at = snapshot.get("finished_at") or (time.time() if interrupted else None)
        task.error = snapshot.get("error") or ("服务在质量评测完成前重启，已保留证据，未自动继续执行。" if interrupted else None)
        task.results = list(snapshot.get("resources") or task.results)
        task.history_only = True
        task.phase = "error" if task.status == "error" else "finished"
        with QUALITY.lock: QUALITY.tasks[task.id] = task
    for snapshot in entries.get("routing_comparisons", []) if isinstance(entries.get("routing_comparisons", []), list) else []:
        if isinstance(snapshot, dict) and snapshot.get("id"):
            _restore_routing_comparison(snapshot)
    # 首次升级时将扫描到的旧结果固化为索引，后续启动不再反复扫描大型 JSONL 文件。
    if not entries.get("tasks") and MANAGER.tasks:
        persist_history()


def fetch_models_detailed_sync(base, headers):
    loop = asyncio.new_event_loop()
    try:
        async def runner():
            async with httpx.AsyncClient() as client:
                return await fetch_models_detailed(client, base, headers)
        return loop.run_until_complete(runner())
    finally:
        loop.close()


def fetch_models_sync(base, headers):
    return fetch_models_detailed_sync(base, headers)["models"]


SERVER_STARTED = time.time()


def main():
    global LOOP, SERVER_SHUTTING_DOWN
    LOOP = asyncio.new_event_loop()
    threading.Thread(target=LOOP.run_forever, daemon=True, name="route-test-loop").start()
    restore_history()
    FINGERPRINTS.restore()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"网关与模型评测工作台 {APP_VERSION} 已启动：http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        SERVER_SHUTTING_DOWN = True
        FINGERPRINTS.close()
        server.server_close(); LOOP.call_soon_threadsafe(LOOP.stop)

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#3975ef"/>'
    '<path d="M7 22V10m0 12h18M12 17l4-5 4 3 5-8" fill="none" stroke="#fff" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

INDEX_HTML = (Path(__file__).parent / 'static' / 'index.html').read_text(encoding='utf-8')


if __name__ == '__main__':
    main()
