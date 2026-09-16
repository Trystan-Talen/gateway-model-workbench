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
import subprocess
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


APP_VERSION = "2026.09.16.1"
PORT = int(os.environ.get("ROUTE_TEST_PORT", "8666"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(BASE_DIR, "tasks.json")
HISTORY_FILE = os.path.join(BASE_DIR, "task_index.json")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
LOOP = None
AUTH_LOCK = threading.RLock()
SESSIONS = {}
SESSION_COOKIE = "route_session"

# 运维基础件：并发闸门、事件日志、审计日志、方案模板。
# 这些状态在进程内共享，跨线程访问各自带锁，不依赖 Web 层。
EVENTS = ops.EventLog()
AUDIT = ops.AuditLog()
LIMITER = ops.ConcurrencyLimiter(1)
TEMPLATES = test_plan.TemplateStore()

# Authentication/HTTP hardening is intentionally conservative for the local HTTP UI.
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 60.0
LOGIN_FAILURES = {}
LOGIN_RATE_LOCK = threading.RLock()

_KEYCHAIN_SERVICE = "route-load-workbench"


def _read_accounts():
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
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

def _keychain_account(url):
    """Use one macOS Keychain entry per gateway URL; URL itself is not sensitive."""
    normalized = str(url or "").strip().rstrip("/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32] or "default"

def _keychain_available():
    return os.name == "posix" and os.uname().sysname == "Darwin"

def _load_api_key(url):
    if not url or not _keychain_available():
        return ""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", _keychain_account(url), "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""

def _save_api_key(url, key):
    key = str(key or "").strip()
    if not url or not key or not _keychain_available():
        return False
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-a", _keychain_account(url), "-s", _KEYCHAIN_SERVICE, "-w", key, "-U"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def _delete_api_key(url):
    if not url or not _keychain_available():
        return False
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-a", _keychain_account(url), "-s", _KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

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

    def snap(self):
        with self.lock:
            return {
                "id": self.id, "name": self.name, "status": self.status,
                "status_label": status_label(self.status),
                "created": self.created, "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": max(0, (self.finished_at or time.time()) - self.started_at) if self.started_at else 0,
                "error": self.error, "config": copy.deepcopy(self.public_config),
                "case_count": self.config.get("case_count", 0),
                "resources": copy.deepcopy(self.results),
                "out_path": self.out_path, "history_only": self.history_only,
                "artifact": self.artifact_snapshot(),
                **_quality_progress(self),
                "report": _quality_report(self.status, self.results, self.config.get("case_count", 0), self.public_config.get("suite_manifest")),
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

    def ordered(self):
        with self.lock:
            values = list(self.tasks.values())
        order = {"running": 0, "stopping": 1, "waiting": 2, "finished_with_errors": 3, "error": 4, "expired": 5, "finished": 6}
        return [x.snap() for x in sorted(values, key=lambda item: (order.get(item.status, 9), -item.created))]


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
                # 立即进入「停止中」，让界面在收尾期间不再显示「运行中」。
                apply_transition(task, "stopping", force=True)
                persist_history()
                return None
            if task.status == "waiting":
                self.queue = deque(x for x in self.queue if x != task_id)
                apply_transition(task, "stopped", force=True)
                persist_history()
                return None
            return "任务已经结束"

    def delete(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return "任务不存在"
            if task.status in ("running", "waiting"):
                return "请先停止任务再删除"
            # 删除任务即删除其结果文件：否则每次删除都会在磁盘上留下一个
            # 再也无法从界面访问的 JSONL，长期运行后必然堆积。
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


class Handler(BaseHTTPRequestHandler):
    server_version = f"RouteLoad/{APP_VERSION}"

    def log_message(self, fmt, *args):
        return

    def _security_headers(self):
        # Keep CSP compatible with the current single-file UI (inline CSS/JS), while
        # still blocking framing, plugin content, and non-same-origin network targets.
        return {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
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
            "/assets/fingerprint.js": ("fingerprint.js", "text/javascript; charset=utf-8"),
            "/assets/fingerprint.css": ("fingerprint.css", "text/css; charset=utf-8"),
        }
        filename, content_type = assets[path]
        raw = (Path(BASE_DIR) / "static" / filename).read_bytes()
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
            return self._json({"error": f"服务器处理失败：{exc}"}, 500)

    def _do_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._html()
        if path in {"/assets/fingerprint.js", "/assets/fingerprint.css"}:
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
            return self._json({"tasks": MANAGER.ordered(), "comparisons": COMPARISONS.ordered(),
                               "quality_tests": QUALITY.ordered(),
                               "routing_comparisons": ROUTING_COMPARISONS.ordered(),
                               "fingerprint_runs": FINGERPRINTS.list_runs(),
                               "fingerprint_storage_warnings": list(FINGERPRINTS.storage_warnings)})
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
                return self._json({"error": f"读取原始记录失败：{exc}"}, 500)
            return self._json({"error": "记录不存在"}, 404)
        if path.startswith("/task/"):
            task = MANAGER.get(path.split("/")[-1])
            return self._json(task.snap() if task else {"error": "任务不存在"}, 200 if task else 404)
        if path == "/credential-status":
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
            key = _load_api_key(url)
            return self._json({"saved": bool(key), "hint": _api_key_hint(key)})
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
            result = self._do_post(parsed, path)
            self._note()
            return result
        except ValueError as exc:
            self._note(400, level="warn")
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._note(500, level="error")
            return self._json({"error": f"服务器处理失败：{exc}"}, 500)

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
            return self._json({"error": f"服务器处理失败：{exc}"}, 500)


def read_saved_config():
    try:
        with open(TASKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cfg = data.get("last_config", {}) if isinstance(data, dict) else {}
        if isinstance(cfg, dict):
            result = public_config(cfg)
            key = _load_api_key(result.get("url"))
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
    payload = {"version": 1, "updated_at": time.time(), **entries}
    temp = f"{HISTORY_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with CONFIG_SAVE_LOCK:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, HISTORY_FILE)
    try:
        os.chmod(HISTORY_FILE, 0o600)
    except OSError:
        pass


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


def persist_history():
    """把任务、质量测试和对比实验的摘要写入索引，不写入 API Key 或原始正文。"""
    try:
        tasks = [_history_safe_snapshot(item) for item in MANAGER.ordered()]
        comparisons = [_history_safe_snapshot(item) for item in COMPARISONS.ordered()]
        quality_tests = [_history_safe_snapshot(item) for item in QUALITY.ordered()]
        # 路由对照的摘要只带清单指纹与两侧地址，不带 prompt 正文，也不带密钥：
        # 正文是批量合成文本，写进索引只会把每次启动都拖慢。
        routing_comparisons = [_routing_history_snapshot(item) for item in ROUTING_COMPARISONS.ordered()]
        _history_write({"tasks": tasks, "comparisons": comparisons, "quality_tests": quality_tests,
                        "routing_comparisons": routing_comparisons})
    except (NameError, OSError, TypeError, ValueError):
        # 历史索引不能阻断压测本身；结果文件仍由任务独立写盘。
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
    entries = {}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            entries = data
    except (OSError, ValueError, TypeError):
        entries = {}
    for snapshot in entries.get("tasks", []) if isinstance(entries.get("tasks", []), list) else []:
        if not isinstance(snapshot, dict):
            continue
        task = _restore_task_snapshot(snapshot)
        if task.id:
            with MANAGER.lock:
                MANAGER.tasks[task.id] = task
    # 兼容历史索引缺失的升级前结果文件。
    fallback_cfg = read_saved_config()
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
    print(f"网关压测工作台 {APP_VERSION} 已启动：http://127.0.0.1:{PORT}")
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

INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.ico">
<title>网关压测工作台</title>
<style>
/* 页面基础变量：统一浅色表面、间距、圆角与状态。 */
:root{
--canvas:#edf1f6;
--canvas2:#edf1f6;
--shell:#ffffff;
--panel:#ffffff;
--panel-solid:#ffffff;
--line:#e1e7ef;
--line-strong:#c8d2df;
--text:#17263c;
--sub:#4b5c73;
--muted:#62738a;
--blue:#316bdc;
--blue2:#79a9f3;
--indigo:#5a70d8;
--green:#198f68;
--red:#c94d5a;
--amber:#b97822;
--shadow:0 12px 36px rgba(26,47,77,.08);
--card-shadow:0 2px 8px rgba(26,47,77,.035);
--radius:22px;
--radius-sm:14px;
--radius-shell:18px;
--radius-card:14px;
--radius-control:8px;
--text-strong:#17263c;
--text-body:#384b63;
--text-secondary:#53667e;
--text-muted:#62738a;
--space-1:4px;
--space-2:8px;
--space-3:12px;
--space-4:16px;
--space-5:20px;
--space-6:24px;
--space-8:32px;
--detail-inset:var(--space-6);
--card-inset:var(--space-6);
--section-gap:var(--space-4);
--radius-inner:10px;
--card-border:#e0e6ef;
--control-shadow:0 1px 3px rgba(26,47,77,.06);
--primary-gradient:#316bdc;
--primary-shadow:0 2px 5px rgba(49,107,220,.14);
}
/* 工作区：导航、记录和详情保持明确的三层信息结构。 */
html,body{margin:0;min-height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--text);font-size:13px;line-height:1.55}
body{padding:12px;background:var(--canvas);overflow:hidden;letter-spacing:0}
.app{width:min(1920px,100%);height:calc(100vh - 24px);height:calc(100dvh - 24px);min-height:0;margin:auto;display:grid;grid-template-rows:auto minmax(0,1fr);background:var(--shell);border:1px solid #dbe3ee;border-radius:var(--radius-shell);box-shadow:var(--shadow);overflow:clip}
.top{display:flex;align-items:center;min-height:68px;padding:14px 20px;gap:12px;border-bottom:1px solid var(--line);background:#fff}
.logo{width:34px;height:34px;flex:none;display:grid;place-items:center;border-radius:10px;background:var(--blue);color:#fff;font-size:18px;font-weight:800}
.brand{font-size:16px;font-weight:750;letter-spacing:-.02em;white-space:nowrap}
.crumb{padding-left:12px;border-left:1px solid var(--line);color:var(--muted);font-size:12px;white-space:nowrap}
.health{display:flex;align-items:center;gap:8px;margin-left:auto;color:var(--sub);font-size:12px;white-space:nowrap}
.layout{display:grid;grid-template-columns:184px 264px minmax(0,1fr);grid-template-rows:minmax(0,1fr);grid-template-areas:"navigation records detail";width:100%;height:100%;min-width:0;min-height:0;align-items:stretch}
.sidebar,.record-panel,.main{min-width:0;min-height:0;height:100%;overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable}
.sidebar{grid-area:navigation;padding:24px 12px;background:#f5f7fb;border-right:1px solid var(--line)}
.record-panel{grid-area:records;padding:24px 16px;background:#fbfcfe;border-right:1px solid var(--line)}
.main{grid-area:detail;padding:var(--detail-inset);background:#f2f5f9;container-type:inline-size;container-name:detail}
.section-title{margin:0 10px 12px;color:var(--muted);font-size:11px;font-weight:650;letter-spacing:.04em}
.module-nav{display:flex;flex-direction:column;gap:6px;margin:0}
.module-nav-item{display:flex;align-items:center;gap:10px;position:relative;width:100%;padding:12px 10px;border:1px solid transparent;border-radius:10px;background:transparent;color:var(--sub);text-align:left;transition:background .16s,color .16s,border-color .16s}
.module-nav-item:hover{background:#ebf0f8;color:var(--text)}
.module-nav-item.active{background:#e6eefc;color:#245bc1;border-color:#d8e5fb;box-shadow:none}
.module-nav-item b{display:block;font-size:13px;font-weight:700;line-height:1.5}
.module-nav-item small{display:block;margin-top:3px;color:var(--muted);font-size:11px;font-weight:400;line-height:1.45}
.module-icon{width:30px;height:30px;flex:0 0 30px;display:grid;place-items:center;border-radius:8px;background:#e9eef7;color:#607895}
.module-nav-item.active .module-icon{background:#d8e6fe;color:#316bdc}
.module-icon .ui-icon{width:18px;height:18px}
.nav-note{display:flex;align-items:flex-start;gap:8px;margin:26px 8px 0;padding:14px 0;border-top:1px solid var(--line);color:var(--muted);font-size:11px;line-height:1.7}
.nav-note-dot{width:5px;height:5px;flex:none;margin-top:6px;border-radius:50%;background:#91a4bc}
.record-panel-head{display:flex;flex-direction:column;align-items:stretch;gap:18px;margin:0 0 22px}
.record-panel-head>div{min-width:0}
.record-kicker{color:var(--muted);font-size:11px;font-weight:600;line-height:1.5;letter-spacing:.03em}
.record-panel-head h2{margin:5px 0;font-size:21px;font-weight:750;line-height:1.4;letter-spacing:-.02em}
.record-subtitle{color:var(--muted);font-size:12px;line-height:1.7}
.record-create-button{display:flex;align-items:center;justify-content:center;gap:8px;flex:none;width:100%;min-width:0;max-width:none;height:40px;margin:0;padding:0 12px;border:1px solid transparent;border-radius:var(--radius-control);background:var(--blue);color:#fff;font-size:13px;font-weight:650;white-space:nowrap;box-shadow:var(--primary-shadow)}
.record-create-button:hover{background:#285cc5}
.workspace-subnav.record-filter{display:flex;align-items:stretch;gap:4px;margin:0 0 20px;padding:4px;border:1px solid #e3e9f2;border-radius:10px;background:#edf1f7;overflow-x:auto;scrollbar-width:none}
.record-filter::-webkit-scrollbar{display:none}
.record-filter .filter-caption{display:none}
.subnav-item{display:flex;align-items:center;justify-content:center;gap:6px;flex:1 1 0;min-width:0;padding:8px 5px;border:1px solid transparent;border-radius:7px;background:transparent;color:var(--sub);text-align:center;white-space:nowrap}
.subnav-item span{font-size:12px;font-weight:600;line-height:1.5}
.subnav-item em{font-style:normal;font-size:11px;color:var(--muted);margin-left:3px;font-variant-numeric:tabular-nums}
.subnav-item:hover{background:#f7f9fc}
.subnav-item.active{background:#fff;color:#245bc1;border-color:#e0e7f1;box-shadow:var(--control-shadow)}
.record-summary{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 16px;padding:0 2px 12px;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--sub);font-size:12px;font-weight:650;line-height:1.5}
.record-count,.record-summary em{min-width:24px;padding:2px 7px;border-radius:6px;background:#e9eef6;color:#536880;font-size:11px;font-style:normal;font-weight:600;text-align:center;font-variant-numeric:tabular-nums}
.task-section-title{margin:0 2px 12px;font-size:11px;line-height:1.5;color:var(--muted);font-weight:600;letter-spacing:0}
.task-list{display:flex;flex-direction:column;gap:8px;min-width:0}
.task{width:100%;min-width:0;padding:13px 12px;border:1px solid var(--line);border-radius:10px;background:#fff;color:inherit;text-align:left;transition:border-color .16s,background .16s,box-shadow .16s}
.task:hover{border-color:#b9cce8;background:#fafdff;transform:none;box-shadow:0 2px 7px rgba(35,63,100,.04)}
.task.sel{border-color:#b7cdf3;background:#f3f7ff;box-shadow:inset 3px 0 var(--blue)}
.task-name{flex:1;min-width:0;font-size:13px;font-weight:650;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.task-meta{margin:8px 0 10px;color:var(--muted);font-size:11px;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.task-pct{width:32px;font-size:11px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
.task-empty{display:flex;flex-direction:column;justify-content:center;gap:7px;min-height:156px;padding:20px 14px;border:1px dashed #cfd9e6;border-radius:10px;background:transparent;text-align:center}
.task-empty b{display:block;font-size:13px;font-weight:650;line-height:1.5;color:var(--sub)}
.task-empty span{display:block;color:var(--muted);font-size:12px;line-height:1.65}
.empty{display:grid;place-items:center;min-height:100%;padding:32px 20px;text-align:center;color:var(--muted)}
.empty>div{max-width:520px}
.empty-visual{display:grid;place-items:center;width:76px;height:76px;margin:0 auto 22px;border:1px solid #dce6f6;border-radius:20px;background:#e7effb;color:var(--blue);box-shadow:none}
.empty-visual .ui-icon{width:30px;height:30px}
.empty b{display:block;margin:0 0 10px;font-size:22px;font-weight:700;line-height:1.45;color:var(--text)}
.empty span{display:block;font-size:13px;line-height:1.75;color:var(--muted)}
.empty-action{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;margin-top:24px;padding:0 16px;border:1px solid transparent;border-radius:var(--radius-control);background:var(--blue);color:#fff;font-size:13px;font-weight:650;box-shadow:var(--primary-shadow)}
.empty-action:hover{background:#285cc5}
.mobile-config{display:none}
/* 创建抽屉：标题与提交固定，正文独立滚动。 */
.config{display:flex;flex-direction:column;position:fixed;z-index:61;inset:0 0 0 auto;width:min(560px,100vw);height:100vh;height:100dvh;max-height:100dvh;margin:0;padding:0;overflow:hidden;background:#fff;border:0;border-left:1px solid var(--line);border-radius:16px 0 0 16px;box-shadow:-16px 0 56px rgba(21,44,78,.16);transform:translate3d(100%,0,0);opacity:0;visibility:hidden;pointer-events:none;transition:transform .22s ease,opacity .18s,visibility 0s linear .22s}
.config.mobile-open{transform:translate3d(0,0,0);opacity:1;visibility:visible;pointer-events:auto;transition-delay:0s}
.config>*{min-width:0;max-width:100%}
.drawer-backdrop{position:fixed;z-index:60;inset:0;background:rgba(23,38,60,.3);backdrop-filter:blur(2px)}
.drawer-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;flex:none;padding:24px 28px 20px;margin:0;border-bottom:1px solid var(--line);background:#fff}
.drawer-head>div{min-width:0}
.drawer-head .form-title{font-size:21px;font-weight:750;line-height:1.4}
.drawer-head .form-sub{margin:8px 0 0;font-size:12px;line-height:1.7;color:var(--muted)}
.drawer-close{display:grid;place-items:center;flex:0 0 34px;width:34px;height:34px;padding:0;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--sub)}
.drawer-close:hover{background:#eef3fa;border-color:#c6d4e6;color:var(--text)}
.drawer-body{flex:1 1 auto;min-width:0;min-height:0;padding:24px 28px;overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable}
.drawer-footer{flex:none;padding:16px 28px 20px;border-top:1px solid var(--line);background:#fff}
.drawer-footer .submit{position:static;height:44px;margin:0;background:var(--blue);color:#fff;box-shadow:var(--primary-shadow)}
.drawer-footer .submit:hover{background:#285cc5}
.drawer-footer .form-note{margin:10px 0 0;padding:0;background:transparent;border:0;font-size:11px;line-height:1.55}
.drawer-footer .form-status:empty{display:none}
.drawer-footer .form-status{margin:0 0 10px;font-size:12px;line-height:1.5}
body.drawer-open{overflow:hidden}
.form-section-title{margin:24px 0 14px;padding-top:20px;border-top:1px solid var(--line);color:var(--text);font-size:14px;font-weight:700;line-height:1.5}
.form-section-title:first-child{margin-top:0;padding-top:0;border-top:0}
#routingSideTabs .tab{display:flex;align-items:center;justify-content:center;gap:8px;min-height:40px;font-size:13px}
.suite-actions{display:flex;flex-wrap:wrap;gap:8px}.suite-details{margin-top:12px}.suite-details summary{cursor:pointer;font-size:12px;color:var(--sub)}
.routing-side-status{padding:2px 6px;border-radius:5px;background:#e7edf6;color:var(--muted);font-size:11px;font-weight:500}
#routingSideTabs .is-complete .routing-side-status{background:#e2f3eb;color:#19724f}
#routingSideTabs{grid-template-columns:repeat(2,minmax(0,1fr));margin-bottom:16px}
[data-routing-side]{min-width:0;padding:18px;border:1px solid var(--line);border-radius:12px;background:#f8fafd}
[data-routing-side] .field:last-child{margin-bottom:0}
.comparison-fields{display:grid;gap:16px;min-width:0;max-width:100%;overflow:visible}
.group-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;min-width:0;max-width:100%;padding:16px;border:1px solid var(--line);border-radius:12px;background:#f8fafd;box-shadow:none}
.group-row .group-name-cell{grid-column:1/-1}
.group-row .group-cell{min-width:0;margin:0}
.group-row .group-cell input,.group-row .group-cell select{width:100%;min-width:0;max-width:100%}
.group-row .group-remove{grid-column:1/-1;justify-self:end;min-width:0}
.model-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;min-width:0;max-width:100%}
.model-row .icon-btn{min-width:80px}
.model-row.is-loading .icon-btn::before{content:'';display:inline-block;width:11px;height:11px;margin-right:6px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;vertical-align:-2px;animation:spin .7s linear infinite}
.model-row.is-done .icon-btn::before{content:'✓';margin-right:5px}.model-row.is-error .icon-btn::before{content:'!';margin-right:5px;font-weight:800}
.secret-row{display:grid;grid-template-columns:minmax(0,1fr) auto 40px;gap:8px;min-width:0;max-width:100%}
.secret-input{min-width:0;max-width:100%;overflow-x:auto;white-space:nowrap;text-overflow:clip}
.field-label-row{display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0}
.field-label-row label{min-width:0;font-size:12px;font-weight:650;line-height:1.5;margin-bottom:8px}
.text-btn{min-width:0;max-width:100%;padding:5px 0;border:0;background:transparent;color:#285fc7;font-size:12px;font-weight:600;white-space:normal;text-align:left;line-height:1.5}
.text-btn:hover{text-decoration:underline;text-underline-offset:3px}

/* 报告、表单与业务组件。 */
*{box-sizing:border-box}
button,input,select{font:inherit}
button{cursor:pointer}
button:disabled{cursor:not-allowed}
button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid rgba(57,117,239,.22);outline-offset:2px}
.health-dot{width:8px;height:8px;border-radius:50%;background:#23b17a;box-shadow:0 0 0 5px rgba(35,177,122,.12)}
.task-top{display:flex;align-items:center;gap:8px}
.status{display:inline-flex;align-items:center;gap:5px;padding:4px 8px;border-radius:999px;background:#e8edf3;color:#68758a;font-size:11px;font-weight:700;white-space:nowrap}
.status.running{background:#e1ebff;color:#3264d5}
.status.finished{background:#dff5ec;color:#167e58}
.status.stopped{background:#ebeff4;color:#697589}
.status.error{background:#fde7e9;color:#b23b43}
.status.finished_with_errors{background:#fff0d9;color:#94601b}
.status.waiting{background:#fff0d9;color:#94601b}
.task-foot{display:flex;align-items:center;gap:9px}
.mini-track{height:5px;border-radius:99px;background:#dfe6ef;overflow:hidden;flex:1}
.mini-track i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#3975ef,#70adff)}
[hidden]{display:none!important}
.hero{display:flex;align-items:flex-start;gap:24px;margin-bottom:20px}
.hero-main{min-width:0;flex:1}
.eyebrow{font-size:11px;color:#4b75d7;font-weight:800;letter-spacing:.11em;text-transform:uppercase;margin-bottom:9px}
.title-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.title-row h1{font-size:32px;line-height:1.15;margin:0;letter-spacing:-.04em}
.phase{font-size:12px;color:#6f7d90;margin-top:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hero-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.btn{height:36px;padding:0 13px;border:1px solid var(--line-strong);border-radius:11px;background:rgba(255,255,255,.75);color:#344157;font-size:12px;font-weight:680}
.btn:hover{background:#fff;border-color:rgba(57,117,239,.28);color:#2c61c9}
.btn:active{transform:translateY(1px);background:#edf4ff}
.btn:disabled{opacity:.52;filter:none}
.btn.danger:hover{background:#fff1f2;border-color:rgba(223,91,99,.44);color:#ad3942}
.btn.danger{color:#bd454c;border-color:rgba(223,91,99,.28);background:rgba(255,246,247,.8)}
.btn.primary{background:#243149;color:#fff;border-color:#243149}
.notice{border:1px solid rgba(223,91,99,.28);background:#fff3f4;color:#a53b43;border-radius:15px;padding:12px 14px;font-size:12px;margin-bottom:14px}
.progress-card{position:relative;overflow:hidden;background:linear-gradient(145deg,rgba(255,255,255,.98),rgba(245,249,254,.92));border:1px solid rgba(255,255,255,.95);border-radius:var(--radius-card);padding:25px 26px 22px;box-shadow:var(--card-shadow);margin-bottom:16px}
.progress-card:before{content:"";position:absolute;width:260px;height:260px;border-radius:50%;right:-150px;top:-170px;background:rgba(91,142,242,.08)}
.progress-head{position:relative;display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:17px}
.progress-copy strong{font-size:15px}
.progress-copy div{font-size:12px;color:var(--muted);margin-top:6px}
.progress-num{font-size:39px;font-weight:820;line-height:1;letter-spacing:-.06em;color:#20304b}
.progress-num small{font-size:15px;color:#8190a4;font-weight:600;margin-left:4px}
.track{height:15px;border-radius:999px;background:#dfe6f0;overflow:hidden;position:relative;box-shadow:inset 0 2px 5px rgba(47,65,89,.08)}
.fill{height:100%;min-width:0;border-radius:inherit;background:linear-gradient(90deg,#326ce6 0%,#477ff3 45%,#79b5ff 100%);box-shadow:0 6px 18px rgba(57,117,239,.32);transition:width .45s ease;position:relative}
.progress-card.running .fill:after{content:"";position:absolute;inset:0;background:linear-gradient(105deg,transparent 15%,rgba(255,255,255,.5) 45%,transparent 75%);animation:flow 1.8s linear infinite}
@keyframes flow{from{transform:translateX(-100%)}to{transform:translateX(140%)}}
.progress-foot{display:flex;justify-content:space-between;gap:16px;margin-top:12px;color:#6e7d91;font-size:11px}
.progress-warning{margin-top:13px;padding:10px 12px;border-radius:12px;background:#fff1dc;color:#8b5a17;font-size:11px;line-height:1.55}
.summary{display:grid;grid-template-columns:1.06fr .94fr;gap:16px;margin-bottom:16px}
.success-card{min-height:205px;border-radius:var(--radius-card);padding:25px 26px;color:#fff;background:radial-gradient(circle at 86% 18%,rgba(112,166,255,.28),transparent 31%),linear-gradient(145deg,#22314d,#182338);position:relative;overflow:hidden;box-shadow:var(--card-shadow)}
.success-card:after{content:"";position:absolute;width:185px;height:185px;border:28px solid rgba(104,158,248,.15);border-radius:50%;right:-70px;bottom:-115px}
.success-label{font-size:12px;color:#b7c5d9}
.success-number{font-size:67px;line-height:1;font-weight:820;letter-spacing:-.07em;margin:22px 0 14px}
.success-number span{font-size:24px;color:#aebed5;margin-left:4px}
.success-note{font-size:11px;line-height:1.55;color:#aebbd0;max-width:80%}
.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.metric{background:rgba(255,255,255,.88);border:1px solid rgba(255,255,255,.95);border-radius:var(--radius-card);padding:16px;min-height:97px;box-shadow:var(--card-shadow)}
.metric-label{font-size:11px;color:#718095}
.metric-value{font-size:23px;font-weight:770;margin-top:13px;letter-spacing:-.035em}
.metric-value small{font-size:11px;color:var(--muted);font-weight:550;margin-left:4px}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding-bottom:30px}
.card{background:rgba(251,253,255,.9);border:1px solid rgba(255,255,255,.92);border-radius:var(--radius-card);padding:20px;box-shadow:var(--card-shadow)}
.card.wide{grid-column:1/-1}
.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:15px;margin-bottom:17px}
.card-title{font-size:14px;font-weight:760}
.card-sub{font-size:11px;color:var(--muted);margin-top:5px}
.error-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
.error-item{background:#f4f7fb;border:1px solid rgba(95,113,140,.08);border-radius:14px;padding:13px}
.error-item b{font-size:21px}
.error-item span{display:block;font-size:11px;color:#768398;margin-top:5px}
.capacity-note{margin-top:12px;padding:10px 12px;border-radius:12px;background:rgba(57,117,239,.08);color:#45618d;font-size:11px;line-height:1.6}
.capacity-note.warn{background:rgba(217,148,50,.12);color:#8b641f}
.chart-wrap{height:250px;position:relative;min-width:0;width:100%;overflow:hidden}
.chart-wrap canvas{display:block;width:100%;height:100%;max-width:100%}
.chart-empty{position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);font-size:12px}
.chart-legend{display:flex;gap:13px;flex-wrap:wrap;font-size:11px;color:#778498}
.chart-legend i{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
.table-wrap{overflow:auto;max-height:330px;border:1px solid var(--line);border-radius:14px;background:#fff}
table{width:100%;border-collapse:collapse;font-size:11px}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #edf0f4;white-space:nowrap}
th{color:#778397;background:#f7f9fc;position:sticky;top:0;z-index:1}
.tag{display:inline-block;border-radius:99px;padding:4px 8px}
.tag.ok{background:#e2f5ed;color:#147b55}
.tag.timeout{background:#fff0da;color:#945c12}
.tag.network{background:#edf1f5;color:#637084}
.tag.protocol{background:#e9edff;color:#5266c7}
.tag.r429,.tag.r5xx{background:#fde7e9;color:#ae3b43}
.tag.r4xx{background:#fff0da;color:#945c12}
.path{max-width:350px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:11px}
.form-title{font-size:18px;font-weight:800;letter-spacing:-.02em}
.form-sub{font-size:11px;color:var(--muted);margin:5px 0 20px;line-height:1.5}
.field{margin-bottom:14px}
.field label{display:block;margin-bottom:7px;color:#566379;font-size:11px;font-weight:680}
.input,select{width:100%;height:40px;border:1px solid rgba(88,106,133,.2);border-radius:11px;background:rgba(255,255,255,.82);padding:0 11px;color:#243149;outline:0;font-size:13px;line-height:40px}
.input::placeholder{color:var(--muted);opacity:1;font-size:12px}
.input:hover,select:hover{border-color:rgba(88,106,133,.36);background:#fff}
.input:focus,select:focus{border-color:#6f9aee;box-shadow:0 0 0 4px rgba(57,117,239,.11);background:#fff}
.inline{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.icon-btn{min-width:92px;height:40px;border:1px solid rgba(88,106,133,.2);border-radius:11px;background:#fff;padding:0 12px;color:#40516d;font-size:12px;font-weight:700;transition:.18s;white-space:nowrap}
.icon-btn:hover{border-color:rgba(57,117,239,.35);background:#edf4ff;color:#2f64d2}
.icon-btn:active{transform:translateY(1px)}
.icon-btn:disabled{color:#94a0af;background:#edf1f6;border-color:#dbe2eb}
.icon-btn.done{color:#17815b;border-color:rgba(32,168,117,.28);background:#edf9f4}
.model-select:disabled{cursor:not-allowed}
.model-fallback{display:flex;align-items:center;min-height:25px}
.model-fallback .text-btn{font-size:11px}
.model-fallback[hidden]{display:none}
.secret-toggle{width:40px;height:40px;display:grid;place-items:center;border:1px solid rgba(88,106,133,.2);border-radius:11px;background:#fff;color:#65758c;font-size:16px;transition:.18s}
.secret-toggle:hover{background:#edf4ff;border-color:rgba(57,117,239,.35);color:#2f64d2}
.secret-toggle:active{transform:translateY(1px)}
.secret-toggle svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.secret-toggle[aria-pressed="true"]{background:#e7f0ff;color:#2f64d2;border-color:rgba(57,117,239,.35)}
.tabs{display:grid;grid-template-columns:repeat(3,1fr);background:#e7edf4;border-radius:12px;padding:4px;margin-bottom:16px}
.tab{border:0;background:transparent;border-radius:9px;padding:9px 3px;font-size:11px;color:#6c7a8e}
.tab.active{background:#fff;color:#20304a;font-weight:730;box-shadow:0 3px 11px rgba(42,58,82,.1)}
.tab:hover{color:#2f64d2}
.tab:active{transform:translateY(1px)}
details{border-top:1px solid var(--line);padding-top:15px;margin-top:18px}
summary{font-size:11px;font-weight:730;cursor:pointer;color:#5c6a80}
.adv-toggle{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:16px;padding:13px;border-radius:14px;background:#edf2f8}
.switch-copy{min-width:0}
.switch-title{font-size:11px;font-weight:730;color:#334158}
.switch-txt{font-size:11px;color:#7e8b9d;line-height:1.45;margin-top:4px}
.switch{position:relative;width:42px;height:24px;flex:0 0 auto}
.switch input{position:absolute;opacity:0}
.switch .slider{position:absolute;inset:0;border-radius:99px;background:#cbd4df;transition:.2s}
.switch .slider:after{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;border-radius:50%;background:#fff;box-shadow:0 2px 6px rgba(35,49,72,.25);transition:.2s}
.switch input:checked+.slider{background:linear-gradient(90deg,#3975ef,#6aa8ff)}
.switch input:checked+.slider:after{transform:translateX(18px)}
.switch input:focus-visible+.slider{outline:3px solid rgba(57,117,239,.22)}
.submit{width:100%;height:46px;border:0;border-radius:13px;background:linear-gradient(135deg,#316ce5,#5e9df5);color:#fff;font-weight:750;box-shadow:0 12px 24px rgba(57,117,239,.24);margin-top:5px}
.submit:hover{filter:brightness(1.03)}
.submit:disabled{opacity:.55}
.form-note{font-size:11px;color:var(--muted);line-height:1.5;margin-top:10px}
.form-status{min-height:16px;font-size:11px;color:#6c7b90;margin:-2px 0 10px}
.form-status.error{color:#bb454d}
.form-status.ok{color:#18815b}
.preflight-card{margin:0 0 14px;padding:13px 14px;border:1px solid rgba(88,106,133,.18);border-radius:14px;background:#f5f8fc}
.preflight-card.level-warn{border-color:rgba(217,148,50,.34);background:#fffaf0}
.preflight-card.level-risk{border-color:rgba(223,91,99,.34);background:#fff5f6}
.preflight-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.preflight-badge{flex:0 0 auto;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:760;background:#e6ecf5;color:#5b6b83}
.preflight-card.level-warn .preflight-badge{background:#fbe6c4;color:#8b5a17}
.preflight-card.level-risk .preflight-badge{background:#fadcdf;color:#a53b43}
.preflight-title{font-size:12px;font-weight:740;color:#2c3a51}
.preflight-summary{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:9px;font-size:11px;color:#69778b}
.preflight-summary b{color:#2c3a51;font-weight:730}
.preflight-findings{margin:10px 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:7px}
.preflight-findings li{padding-left:10px;border-left:2px solid #d3dbe6;font-size:11px;line-height:1.55;color:#5b6879}
.preflight-findings li b{display:block;color:#2c3a51;font-size:11px;font-weight:720}
.preflight-findings li.lv-risk{border-left-color:#e08a91}
.preflight-findings li.lv-warn{border-left-color:#e0b473}
.preflight-findings li.lv-info{border-left-color:#9fb6d8}
.preflight-note{margin-top:9px;font-size:11px;color:var(--muted);line-height:1.5}
.preflight-ack{display:flex;align-items:flex-start;gap:8px;margin-top:11px;padding:10px 11px;border-radius:11px;background:rgba(223,91,99,.08);font-size:11px;color:#8d3f46;line-height:1.5}
.preflight-ack input{margin:2px 0 0;flex:0 0 auto;width:14px;height:14px;accent-color:#df5b63}
.toast{position:fixed;left:50%;bottom:35px;transform:translateX(-50%) translateY(18px);padding:11px 15px;border-radius:12px;background:#1d2940;color:#fff;font-size:12px;opacity:0;pointer-events:none;transition:.2s;z-index:99;box-shadow:0 12px 35px rgba(23,32,51,.25)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.conn-bad .health-dot{background:#df5b63;box-shadow:0 0 0 5px rgba(223,91,99,.12)}
.muted-help{font-size:11px;color:var(--muted);line-height:1.5;margin-top:6px}
.field-hint{font-size:11px;color:var(--muted);line-height:1.45;margin-top:6px}
.field-hint.error{color:#bb454d}
.field-hint.ok{color:#18815b}
.overview-shell{background:linear-gradient(145deg,rgba(249,252,255,.8),rgba(224,232,243,.68));border:1px solid rgba(255,255,255,.8)}
.raw-link{border:1px solid rgba(57,117,239,.2);background:#edf4ff;color:#3264d5;border-radius:9px;padding:5px 9px;font-size:11px;font-weight:720}
.raw-link:hover{background:#dfeaff}
.modal[hidden]{display:none}
.modal{position:fixed;inset:0;z-index:120;display:grid;place-items:center;padding:24px}
.modal-backdrop{position:absolute;inset:0;background:rgba(24,34,52,.44);backdrop-filter:blur(8px)}
.modal-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:18px}
.modal-head h2{margin:0;font-size:24px;letter-spacing:-.035em}
.modal-close{width:38px;height:38px;border:1px solid var(--line);border-radius:12px;background:#fff;color:#657389;font-size:22px}
.raw-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.raw-label{font-size:11px;font-weight:800;color:#6d7b90;letter-spacing:.08em;margin:0 0 7px 4px}
.raw-grid pre{margin:0;min-height:340px;max-height:58vh;overflow:auto;padding:16px;border-radius:17px;background:#172238;color:#dbe7f8;font:11px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:inset 0 1px rgba(255,255,255,.05)}
@media(max-width:1260px){
.summary{grid-template-columns:1fr}
.success-card{min-height:180px}
}
@media(max-width:780px){
.raw-grid{grid-template-columns:1fr}
.raw-grid pre{min-height:220px}
.modal{padding:10px}
.hero{display:block}
.hero-actions{justify-content:flex-start;margin-top:15px}
.title-row h1{font-size:27px}
.progress-card{padding:20px}
.progress-foot{display:block}
.progress-foot span{display:block;margin-top:5px}
.summary,.cards{display:block}
.success-card,.card{margin-bottom:13px}
.error-grid{grid-template-columns:1fr 1fr}
.chart-legend{display:none}
}
.loading-state{height:100%;display:grid;place-content:center;justify-items:center;gap:10px;text-align:center;color:#78869a}
.loading-state b{color:#223047;font-size:16px}
.loading-state span{font-size:12px}
.loading-spinner{width:28px;height:28px;border:3px solid #dce5f2;border-top-color:#3975ef;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.secret-action{height:40px;border:1px solid rgba(88,106,133,.2);border-radius:11px;background:#fff;color:#40516d;font-size:11px;font-weight:700;transition:.18s}
.secret-action:hover{border-color:rgba(57,117,239,.35);background:#edf4ff;color:#2f64d2}
.secret-action:active{transform:translateY(1px)}
.secret-action:disabled{opacity:.45}
.model-select{min-width:0;padding-right:30px;line-height:normal}
.model-select option{color:#243149}
.model-select:disabled{color:var(--muted);background:#edf1f6}
.create-kind{display:none}
.kind-btn{display:none}
.module-card{max-width:860px;margin:8vh auto;padding:42px;border:1px solid var(--line);border-radius:28px;background:rgba(255,255,255,.7);box-shadow:var(--card-shadow)}
.module-card h1{margin:12px 0;font-size:34px}
.module-card p{max-width:620px;color:var(--sub);line-height:1.8}
.module-kicker{font-size:11px;letter-spacing:.14em;color:#3975ef;font-weight:800}
.module-actions{margin-top:28px;max-width:240px}
.comparison-hint{font-size:11px;line-height:1.5;color:var(--sub);padding:10px 11px;background:#eef4ff;border-radius:11px}
.report-section-title-row{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
.evidence-badge{padding:6px 10px;border-radius:999px;background:#edf3fb;color:#50627d;font-size:11px;font-weight:760;white-space:nowrap}
.report-supplement{background:linear-gradient(145deg,rgba(255,255,255,.74),rgba(241,246,252,.72))}
.report-method-grid{margin-top:16px}
.report-footer-actions{padding-bottom:8px}
.report-tab[aria-selected="true"]{font-weight:800}
.report-tab:active{transform:translateY(1px)}
@media(max-width:620px){
.report-section-title-row{display:block}
.evidence-badge{display:inline-flex;margin-top:8px}
}
.module-card{max-width:none;margin:0;padding:0;border:0;background:transparent;box-shadow:none}
.report-head{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;border:1px solid rgba(255,255,255,.8);background:linear-gradient(135deg,rgba(255,255,255,.92),rgba(239,245,253,.8))}
.report-head h1{margin:6px 0 8px;font-size:29px;line-height:1.2;letter-spacing:-.04em}
.report-head p{max-width:760px;margin:0;color:var(--sub);font-size:13px;line-height:1.65}
.report-kicker{color:#3975ef;font-size:11px;font-weight:800;letter-spacing:.14em}
.report-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:13px}
.report-meta span{padding:5px 9px;border-radius:999px;background:#edf3fb;color:var(--sub);font-size:11px}
.report-status{min-width:138px;padding:13px 14px;border-radius:15px;background:#f1f4f8;color:var(--sub);font-size:11px;font-weight:750;text-align:center}
.report-status.good{background:#e5f6ee;color:#177b59}
.report-status.caution{background:#fff2dc;color:#95611e}
.report-status.risk{background:#fde8eb;color:#b13d4b}
.report-tabs{display:flex;gap:7px;margin:18px 0 14px;padding:4px;border-bottom:1px solid var(--line)}
.report-tab{border:0;border-bottom:2px solid transparent;border-radius:9px 9px 0 0;padding:10px 13px;background:transparent;color:var(--sub);font-size:12px;font-weight:750}
.report-tab:hover{background:rgba(255,255,255,.58);color:var(--text)}
.report-tab.active{border-bottom-color:var(--blue);color:var(--text);background:rgba(255,255,255,.62)}
.report-section-title{margin:0 0 9px;font-size:15px;letter-spacing:-.02em}
.report-section-sub{margin:-3px 0 11px;color:var(--muted);font-size:11px;line-height:1.5}
.conclusion-banner{padding:18px 20px;border:1px solid var(--line);border-left:4px solid #9ba8b8;border-radius:16px;background:rgba(255,255,255,.76)}
.conclusion-banner.good{border-left-color:var(--green)}
.conclusion-banner.caution{border-left-color:var(--amber)}
.conclusion-banner.risk{border-left-color:var(--red)}
.conclusion-banner strong{display:block;margin-bottom:6px;font-size:16px}
.conclusion-banner p{margin:0;color:var(--sub);font-size:12px;line-height:1.65}
.signal-list{display:grid;gap:7px;margin-top:13px}
.signal{display:flex;gap:9px;padding:10px 12px;border-radius:12px;background:#f3f6fa;color:var(--sub);font-size:11px;line-height:1.5}
.signal-dot{width:7px;height:7px;flex:none;margin-top:5px;border-radius:50%;background:#8e9aae}
.signal.risk .signal-dot{background:var(--red)}
.signal.caution .signal-dot{background:var(--amber)}
.signal.good .signal-dot{background:var(--green)}
.report-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.report-metric{min-height:102px;padding:15px 16px;border:1px solid var(--line);border-radius:var(--radius-card);background:rgba(255,255,255,.76)}
.report-metric .metric-label{margin-bottom:8px}
.report-metric .metric-value{font-size:23px}
.report-metric .metric-note{margin-top:6px;color:var(--muted);font-size:11px;line-height:1.4}
.report-table{width:100%;border-collapse:separate;border-spacing:0;min-width:930px}
.report-table th{position:sticky;top:0;z-index:2;background:#eef3f8;color:var(--sub);font-size:11px;font-weight:800;white-space:nowrap}
.report-table td{font-size:11px}
.report-table th,.report-table td{padding:11px 10px;border-bottom:1px solid var(--line);text-align:left}
.report-table tr:last-child td{border-bottom:0}
.report-table .baseline-row{background:#f4f8ff}
.table-wrap.report-wrap{overflow:auto;max-height:min(52vh,560px);overscroll-behavior:contain;border:1px solid var(--line);border-radius:15px;background:rgba(255,255,255,.72)}
.delta-good{color:var(--green);font-weight:750}
.delta-risk{color:var(--red);font-weight:750}
.delta-neutral{color:var(--sub)}
.method-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}
.method-card{padding:13px 14px;border-radius:var(--radius-card);background:rgba(255,255,255,.62);border:1px solid var(--line)}
.method-card b{display:block;margin-bottom:5px;font-size:11px}
.method-card span{display:block;color:var(--sub);font-size:11px;line-height:1.55}
.group-detail-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.group-detail-head h2{margin:0;font-size:21px}
.group-detail-head p{margin:6px 0 0;color:var(--sub);font-size:11px}
.group-badge{padding:5px 9px;border-radius:999px;background:#eaf1ff;color:#3566d5;font-size:11px;font-weight:750;white-space:nowrap}
.quality-ab{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.quality-ab .card{margin:0}
.quality-delta{padding:13px;border-radius:14px;background:#f4f7fb;border:1px solid var(--line);font-size:11px;color:var(--sub);line-height:1.6}
.quality-delta strong{color:var(--text);font-size:18px}
.quality-report-table{min-width:820px}
.quality-report-table th,.quality-report-table td{padding:10px 9px}
.quality-report-table td{vertical-align:top}
.case-prompt{max-width:240px;color:var(--text);line-height:1.45}
.case-answer{max-width:280px;color:var(--sub);line-height:1.45}
.report-limitations{margin:0;padding-left:18px;color:var(--sub);font-size:11px;line-height:1.75}
.template-picker{margin:0 0 14px;padding:11px 12px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.5)}
.template-picker[hidden]{display:none}
.template-picker-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.template-picker-title{font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--sub)}
.template-picker-body{display:grid;gap:6px;margin-top:9px}
.template-picker-body[hidden]{display:none}
.template-picker-note{margin:9px 0 0;color:var(--muted);font-size:11px;line-height:1.5}
.template-chip{display:block;width:100%;border:1px solid var(--line);border-radius:11px;background:#fff;padding:9px 11px;text-align:left}
.template-chip:hover{border-color:rgba(57,117,239,.35);background:#f6faff}
.template-chip.active{border-color:rgba(57,117,239,.55);background:#eef5ff}
.template-chip b{display:block;font-size:11px;color:var(--text)}
.template-chip small{display:block;margin-top:3px;font-size:11px;color:var(--muted);line-height:1.45}
.template-chip em{display:inline-block;margin-top:5px;padding:2px 7px;border-radius:999px;background:#eef3fb;color:var(--sub);font-size:11px;font-style:normal}
.template-picker-more{margin-top:2px;font-size:11px}
.diag-grid{display:grid;gap:14px}
.diag-block{border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.66);padding:13px 14px}
.diag-block h3{margin:0 0 4px;font-size:13px}
.diag-block .diag-sub{margin:0 0 10px;color:var(--muted);font-size:11px;line-height:1.5}
.diag-kv{display:flex;flex-wrap:wrap;gap:6px}
.diag-kv span{padding:4px 9px;border-radius:999px;background:#edf3fb;color:var(--sub);font-size:11px}
.diag-list{margin:0;padding:0;list-style:none;display:grid;gap:5px}
.diag-list li{display:flex;gap:8px;align-items:baseline;padding:7px 9px;border-radius:10px;background:#f4f7fb;font-size:11px;color:var(--sub);line-height:1.45}
.diag-list li b{color:var(--text);font-weight:750;white-space:nowrap}
.diag-list li time{margin-left:auto;color:var(--muted);white-space:nowrap}
.diag-empty{padding:10px 11px;border-radius:10px;background:#f4f7fb;color:var(--muted);font-size:11px}
.diag-tools{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.diag-limit{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--sub)}
.diag-limit input{width:76px}
@media(max-width:900px){
.report-head{display:block}
.report-status{margin-top:14px}
.report-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
.method-grid{grid-template-columns:1fr}
.quality-ab{grid-template-columns:1fr}
}
@media(max-width:620px){
.report-head h1{font-size:24px}
.report-tabs{overflow-x:auto}
.report-tab{white-space:nowrap}
.report-metrics{grid-template-columns:1fr}
}
.status{padding:3px 7px;font-size:11px}
.overview-shell{background:linear-gradient(145deg,rgba(249,252,255,.9),rgba(226,233,242,.78))}
.hero .title-row h1{font-size:30px;letter-spacing:-.04em}
.hero .phase{font-size:11px}
.progress-card{border-radius:var(--radius-card);background:rgba(255,255,255,.77);padding:18px 19px}
.progress-head strong{font-size:14px}
.progress-num{font-size:30px}
.track{height:10px}
.summary{gap:13px}
.success-card{border-radius:var(--radius-card)}
.metric{border-radius:var(--radius-card);background:rgba(249,251,254,.7)}
.card{border-radius:var(--radius-card)}
.card-title{font-size:14px}
.card-sub{font-size:11px}
.chart-wrap{min-height:220px;height:clamp(210px,27vh,310px);border-radius:14px}
.report-head{background:linear-gradient(135deg,rgba(255,255,255,.96),rgba(235,242,251,.85))}
.report-head h1{font-size:29px;letter-spacing:-.04em}
.report-head p{max-width:720px;font-size:12px}
.report-tabs{position:sticky;top:-1px;z-index:2;margin:17px 0 18px;padding:5px;border:1px solid var(--line);border-radius:13px;background:rgba(241,246,252,.9);backdrop-filter:blur(12px);box-shadow:0 8px 20px rgba(42,60,83,.07)}
.report-tab{border:0;border-radius:9px;padding:10px 13px;font-size:11px}
.report-tab.active{background:#fff;border:1px solid rgba(61,114,223,.18);box-shadow:0 4px 12px rgba(44,61,84,.08)}
.report-section-title{font-size:16px}
.report-section-sub{font-size:11px}
.report-section-title-row{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
.decision-section{display:grid;grid-template-columns:minmax(0,1fr) 170px;gap:14px;padding:20px;border:1px solid var(--line);border-radius:var(--radius-card);background:linear-gradient(135deg,rgba(255,255,255,.9),rgba(240,245,251,.8));box-shadow:var(--card-shadow)}
.decision-copy{min-width:0}
.decision-eyebrow{margin-bottom:7px;color:var(--blue);font-size:11px;font-weight:800;letter-spacing:.14em}
.decision-copy strong{display:block;font-size:20px;letter-spacing:-.03em}
.decision-copy p{margin:8px 0 0;color:var(--sub);font-size:12px;line-height:1.65}
.decision-state{align-self:start;min-height:78px;padding:14px;border-radius:15px;background:#eef2f6;color:var(--sub);text-align:center}
.decision-state span{display:block;font-size:14px;font-weight:800}
.decision-state small{display:block;margin-top:5px;font-size:11px;line-height:1.4}
.decision-state.good{background:#e5f6ee;color:#177b59}
.decision-state.caution{background:#fff1da;color:#95611e}
.decision-state.risk{background:#fde8eb;color:#b13d4b}
.decision-section .signal-list{grid-column:1/-1}
.evidence-badge,.table-hint{padding:6px 9px;border-radius:999px;background:#eaf1fc;color:#4a6794;font-size:11px;font-weight:750;white-space:nowrap}
.report-metrics-emphasis .report-metric{min-height:112px;background:linear-gradient(145deg,rgba(255,255,255,.92),rgba(240,245,251,.76))}
.report-metrics-emphasis .report-metric:first-child{border-color:rgba(61,114,223,.22);box-shadow:inset 0 3px 0 rgba(61,114,223,.85)}
.report-metric .metric-value{letter-spacing:-.035em}
.role-label{display:block;margin-top:4px;color:var(--muted);font-size:11px}
.report-table th{position:sticky;top:0;z-index:1}
.report-table td{vertical-align:middle}
.report-table td:nth-child(4),.report-table td:nth-child(6){font-weight:750}
.report-explain{padding-top:2px}
.group-detail-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-top:2px}
.group-detail-actions .field-hint{flex:1;min-width:220px}
.group-cell label{display:block;margin-bottom:5px;font-size:11px;color:var(--sub)}
.group-remove{width:28px;height:34px;padding:0;border-radius:9px;color:var(--muted)}
.group-remove:hover{color:var(--red);background:#fff0f2}
.comparison-hint{border:1px solid rgba(61,114,223,.14);background:#edf4ff;font-size:11px;line-height:1.6}
.comparison-hint b{color:var(--text)}
.form-title{font-size:19px}
.form-sub{font-size:11px;line-height:1.55}
.field label{font-size:11px}
.input{min-height:38px;border-radius:11px;font-size:12px}
.field-hint,.muted-help,.form-note{font-size:11px;line-height:1.5}
.tabs{border-radius:11px}
.tab{font-size:11px}
.submit{height:44px;border-radius:12px;font-size:12px}
.icon-btn,.secret-action,.secret-toggle{min-height:36px}
@media(max-width:1100px){
.report-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:840px){
.decision-section{grid-template-columns:1fr}
.decision-state{max-width:220px}
.report-head h1{font-size:24px}
}
.decision-scope{margin-top:12px;padding:10px 12px;border-radius:12px;background:#edf4ff;color:var(--sub);font-size:11px;line-height:1.6}
.decision-scope b{color:var(--text)}
.quality-delta{display:grid;gap:5px}
.quality-delta b{color:var(--sub);font-size:11px}
.quality-delta strong{display:block;color:var(--text);font-size:22px;letter-spacing:-.03em}
.quality-delta span{color:var(--muted);font-size:11px;line-height:1.45}
.case-answer{max-width:300px;max-height:72px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.raw-block{max-width:360px;max-height:260px;overflow:auto;padding:10px;border-radius:10px;background:#152338;color:#d9e7ff;font:10px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.report-footer-actions{display:flex;justify-content:flex-start;padding-bottom:20px}
@media(max-width:900px){
.quality-delta strong{font-size:20px}
}
.field label{font-size:11px;font-weight:720;color:var(--sub);line-height:1.35}
.input,.input::placeholder{font-size:12px}
.input::placeholder{color:var(--muted);opacity:1}
.field-hint,.muted-help,.form-note,.form-sub{font-size:11px;line-height:1.55;color:var(--muted)}
.btn,.submit,.icon-btn,.secret-action,.secret-toggle{font-size:12px;line-height:1.2}
.btn:disabled,.submit:disabled,.icon-btn:disabled{opacity:.56;filter:saturate(.7)}
.btn:focus-visible,.submit:focus-visible,.icon-btn:focus-visible,.secret-action:focus-visible,.secret-toggle:focus-visible{box-shadow:0 0 0 4px rgba(57,117,239,.14)}
@keyframes spin{to{transform:rotate(360deg)}}
.report-section-sub{max-width:760px}
.quality-report-table td{line-height:1.45}
.quality-report-table .tag{white-space:nowrap}
.report-readout-section{background:linear-gradient(145deg,rgba(250,252,255,.9),rgba(237,243,250,.74))}
.report-readout-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px}
.readout-card{min-width:0;padding:15px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.78);box-shadow:0 8px 22px rgba(45,61,84,.045)}
.readout-label,.decision-action-label{display:block;color:var(--text-muted);font-size:11px;font-weight:800;letter-spacing:.02em}
.readout-value{display:block;margin-top:7px;color:var(--text-strong);font-size:18px;line-height:1.2;letter-spacing:-.025em;word-break:break-word}
.readout-value small{font-size:11px;font-weight:700;color:var(--text-secondary)}
.readout-value.good{color:var(--green)}
.readout-value.caution{color:var(--amber)}
.readout-value.risk{color:var(--red)}
.readout-card p{min-height:34px;margin:8px 0 0;color:var(--text-secondary);font-size:11px;line-height:1.55}
.decision-action-row{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:16px;margin-top:2px;padding:13px 14px;border-top:1px solid rgba(82,101,128,.1);border-radius:13px;background:rgba(239,245,255,.72)}
.decision-action-row strong{display:block;margin-top:4px;color:var(--text-body);font-size:12px;line-height:1.5}
.decision-guardrail{flex:0 0 auto;padding:9px 12px;border:1px solid rgba(223,91,99,.16);border-radius:11px;background:rgba(255,245,245,.84);text-align:right}
.decision-guardrail span{display:block;color:var(--text-muted);font-size:11px}
.decision-guardrail b{display:block;margin-top:3px;color:#b5444d;font-size:11px}
.report-signal-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.report-signal-list .signal{margin:0}
.report-method-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.report-limitations{margin:15px 0 0;padding:13px 15px 13px 30px;border:1px dashed rgba(82,101,128,.18);border-radius:14px;background:rgba(247,250,253,.66);color:var(--text-secondary);font-size:11px;line-height:1.65}
.report-limitations li+li{margin-top:4px}
@media(max-width:1100px){
.report-readout-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:720px){
.report-readout-grid,.report-signal-list,.report-method-grid{grid-template-columns:1fr}
.decision-action-row{align-items:flex-start;flex-direction:column}
.decision-guardrail{width:100%;text-align:left}
.readout-card p{min-height:0}
}
.card,.metric,.report-metric,.method-card{border-radius:var(--radius-card)}
.input,select,.btn,.submit,.icon-btn,.raw-link{border-radius:var(--radius-control)}
.field label{font-size:11px;line-height:1.35}
.input,.input::placeholder,select{font-size:12px;line-height:1.35}
.input::placeholder{color:var(--muted)}
.muted-help,.field-hint,.form-note,.form-sub{font-size:11px;line-height:1.55}
.report-decision-hero{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:18px;padding:24px;border:1px solid rgba(255,255,255,.86);border-radius:var(--radius-card);background:linear-gradient(135deg,rgba(255,255,255,.96),rgba(232,239,248,.84));box-shadow:var(--card-shadow);position:relative;overflow:hidden}
.report-decision-hero::before{content:'';position:absolute;inset:0 auto 0 0;width:4px;background:var(--blue)}
.report-decision-hero.hold::before{background:var(--red)}
.report-decision-hero.review::before{background:var(--amber)}
.report-decision-hero.pending::before{background:#8b98aa}
.decision-main{min-width:0;position:relative;z-index:1}
.decision-main h2{margin:0 0 9px;font-size:22px;line-height:1.25;letter-spacing:-.035em}
.decision-summary{max-width:780px;margin:0;color:var(--text-body);font-size:13px;line-height:1.7}
.decision-scope{display:flex;flex-wrap:wrap;gap:8px 12px;margin-top:16px;padding:11px 13px;border-radius:12px;background:rgba(237,244,255,.82);color:var(--text-secondary);font-size:11px;line-height:1.55}
.decision-scope b{color:#4d6ea5;font-weight:800}
.decision-badge{align-self:start;display:grid;gap:7px;min-height:92px;padding:16px;border-radius:17px;background:#eef2f6;color:var(--text-secondary);text-align:center;align-content:center}
.decision-badge span{font-size:12px;font-weight:850}
.decision-badge small{font-size:11px;line-height:1.45}
.report-decision-hero .signal-list{grid-column:1/-1;position:relative;z-index:1;margin-top:0}
.report-decision-hero .signal{background:rgba(247,250,253,.82);border:1px solid rgba(82,101,128,.08)}
.report-metrics-emphasis .metric-primary{border-color:rgba(57,117,239,.28);background:linear-gradient(145deg,#eef5ff,#fff);box-shadow:var(--card-shadow)}
.report-metrics-emphasis .metric-primary .metric-value{color:#2f66d7}
.next-step-section{padding:20px;border:1px solid var(--line);border-radius:19px;background:rgba(247,250,253,.64)}
.next-step-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.next-step{display:flex;gap:10px;align-items:flex-start;padding:13px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.78)}
.next-step span{display:grid;place-items:center;flex:0 0 24px;height:24px;border-radius:8px;background:#e8f0ff;color:#3264d5;font-size:11px;font-weight:850}
.next-step p{margin:2px 0 0;color:var(--text-body);font-size:11px;line-height:1.55}
.comparison-result-table td:first-child{min-width:150px}
.comparison-result-table .baseline-row td{background:rgba(237,244,255,.52)}
.comparison-result-table .baseline-row td:first-child{box-shadow:inset 3px 0 var(--blue)}
@media(max-width:920px){
.report-decision-hero{grid-template-columns:1fr}
.decision-badge{max-width:260px}
.next-step-grid{grid-template-columns:1fr 1fr}
}
@media(max-width:620px){
.report-decision-hero{padding:18px;border-radius:var(--radius-card)}
.decision-main h2{font-size:19px}
.decision-summary{font-size:12px}
.next-step-grid{grid-template-columns:1fr}
.report-metrics-emphasis{grid-template-columns:1fr}
.report-tabs{top:0}
}
.report-overview{display:grid}
.report-progress-card,.report-focus-card{min-width:0;padding:19px 20px;border:1px solid rgba(190,204,220,.62);border-radius:var(--radius-card);background:linear-gradient(145deg,rgba(255,255,255,.88),rgba(236,243,251,.8));box-shadow:var(--card-shadow)}
.report-progress-card{background:linear-gradient(135deg,#edf4ff,#fff)}
.report-card-kicker{color:#6a7d98;font-size:11px;font-weight:850;letter-spacing:.08em}
.report-progress-line{display:flex;align-items:baseline;gap:10px;margin:8px 0 12px}
.report-progress-line strong{font-size:34px;line-height:1;letter-spacing:-.05em;color:#2f66d7}
.report-progress-line span{color:var(--text-secondary);font-size:11px}
.report-progress-track{height:9px;border-radius:99px;background:#dbe5f1;overflow:hidden}
.report-progress-track i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#3975ef,#70adff);transition:width .35s ease}
.report-progress-card p{margin:11px 0 0;color:var(--text-secondary);font-size:11px;line-height:1.55}
.report-focus-card{display:flex;flex-direction:column;justify-content:center}
.report-focus-card strong{margin:9px 0 5px;font-size:19px;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.report-focus-card span,.report-focus-card small{color:var(--text-secondary);font-size:11px}
.report-focus-card small{margin-top:9px;color:var(--muted)}
.report-baseline-strip{display:flex;align-items:center;flex-wrap:wrap;gap:8px 14px;margin:0 0 16px;padding:10px 13px;border:1px solid rgba(190,204,220,.52);border-radius:12px;background:rgba(232,240,252,.7);color:#64758e;font-size:11px;line-height:1.45}
.report-baseline-strip span{flex:1 1 220px}
.report-baseline-strip b{color:#4169ad;font-weight:800;white-space:nowrap}
@media(max-width:780px){
.report-progress-line strong{font-size:30px}
}
.quality-health-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}
.quality-health-card{display:grid;grid-template-columns:1fr auto;gap:8px;padding:15px 16px;border:1px solid var(--line);border-radius:var(--radius-card);background:rgba(255,255,255,.78)}
.quality-health-card.good{border-color:rgba(32,168,117,.25);background:linear-gradient(145deg,#f0fbf6,#fff)}
.quality-health-card.caution{border-color:rgba(217,148,50,.28);background:linear-gradient(145deg,#fff8eb,#fff)}
.quality-health-card.risk{border-color:rgba(223,91,99,.28);background:linear-gradient(145deg,#fff1f2,#fff)}
.health-card-label{display:block;color:var(--muted);font-size:11px;font-weight:800}
.quality-health-card strong{display:block;margin-top:5px;font-size:14px;line-height:1.35}
.quality-health-card p{grid-column:1/-1;margin:2px 0 0;color:var(--text-secondary);font-size:11px}
.health-card-badge{align-self:start;padding:5px 8px;border-radius:999px;background:#e7edf4;color:var(--text-secondary);font-size:11px;font-weight:800}
.quality-health-card.good .health-card-badge{background:#ddf5ea;color:#167e58}
.quality-health-card.caution .health-card-badge{background:#fff0d9;color:#94601b}
.quality-health-card.risk .health-card-badge{background:#fde7e9;color:#b23b43}
.input,.inline{min-width:0;max-width:100%}
#modulePlaceholder{width:100%;min-width:0;max-width:100%}
.report-head,.report-tabs,.report-section,.group-detail{min-width:0;max-width:100%;box-sizing:border-box}
.report-progress-card{border:1px solid rgba(190,204,220,.72);background:linear-gradient(145deg,rgba(255,255,255,.92),rgba(236,243,251,.84))}
.report-progress-card .progress-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.report-progress-card .progress-head b{font-size:13px;color:var(--text)}
.report-progress-card .progress-percent{font-size:20px;font-weight:800;color:var(--blue)}
.report-progress-track{height:9px;border-radius:999px;background:#dce6f2;overflow:hidden}
.report-progress-track i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#3975ef,#79b4ff);transition:width .3s ease}
.report-progress-card p{margin:10px 0 0;color:var(--sub);font-size:11px;line-height:1.5}
.report-section{border:1px solid var(--line);background:rgba(255,255,255,.78)}
.report-section.report-supplement{background:linear-gradient(145deg,rgba(255,255,255,.84),rgba(241,246,252,.82))}
.group-detail-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding-bottom:16px;border-bottom:1px solid var(--line)}
.group-detail .report-metrics{margin-top:16px}
.group-detail .signal,.group-detail .signal-list{margin-top:14px}
.group-detail .method-grid{margin-top:18px}
.report-wrap{min-width:0;max-width:100%;overflow-x:auto}
.report-table{min-width:680px}
@media(max-width:760px){
.report-section,.group-detail{padding:16px}
.group-detail-head{display:block}
.group-badge{display:inline-flex;margin-top:12px}
.report-progress-card .progress-head{align-items:flex-start}
.report-progress-percent{font-size:18px}
}
#modulePlaceholder{min-width:0;max-width:100%}
.report-overview{min-width:0}
.report-progress-line span{min-width:0;white-space:normal;line-height:1.5}
.report-focus-card strong{min-width:0;max-width:100%}
.group-detail-head,.group-detail .report-metrics,.group-detail .signal-list,.group-detail>section.report-section,.group-detail>.method-grid{width:100%;max-width:100%;min-width:0}
.group-detail-head h2{min-width:0;overflow-wrap:anywhere}
.group-detail-head p{max-width:100%;line-height:1.55}
.group-badge.good{background:#e3f6ed;color:#177d59}
.group-badge.running{background:#e6efff;color:#3568d7}
.group-progress-card{border:1px solid rgba(190,204,220,.62);background:linear-gradient(145deg,rgba(246,250,255,.96),rgba(235,243,252,.82))}
.group-progress-head{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--text-secondary);font-size:11px;font-weight:750}
.group-progress-head b{color:var(--blue);font-size:16px}
.group-progress-track{height:7px;margin-top:9px;border-radius:99px;background:#dbe5f1;overflow:hidden}
.group-progress-track i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#3975ef,#79b4ff);transition:width .35s ease}
.group-progress-card p{margin:8px 0 0;color:var(--text-secondary);font-size:11px;line-height:1.45}
.group-detail>section.report-section{box-sizing:border-box}
.group-detail .report-wrap{width:100%;max-width:100%;min-width:0;overflow:auto}
.group-detail .report-table{width:100%;min-width:680px}
.group-detail>.method-grid{box-sizing:border-box}
@media(max-width:900px){
.group-detail .report-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:620px){
.group-detail .report-metrics{grid-template-columns:1fr}
.group-progress-card{margin-top:14px}
}
.auth-gate{position:fixed;inset:0;z-index:200;display:grid;place-items:center;padding:24px;background:linear-gradient(145deg,rgba(39,58,86,.48),rgba(83,111,151,.38));backdrop-filter:blur(18px)}
.auth-gate[hidden]{display:none}
.auth-card{width:min(440px,100%);padding:32px;border:1px solid rgba(255,255,255,.84);border-radius:26px;background:linear-gradient(145deg,rgba(250,253,255,.98),rgba(233,241,251,.97));box-shadow:0 30px 90px rgba(18,34,57,.3)}
.auth-mark{width:46px;height:46px;margin-bottom:18px;border-radius:15px;display:grid;place-items:center;background:linear-gradient(135deg,#3975ef,#80b5ff);color:#fff;font-size:20px;font-weight:850;box-shadow:0 12px 26px rgba(57,117,239,.28)}
.auth-card h1{margin:0;color:var(--text);font-size:23px;letter-spacing:-.04em}
.auth-card p{margin:9px 0 22px;color:var(--sub);font-size:12px;line-height:1.65}
.auth-form{display:grid;gap:13px}
.password-field{position:relative}
.password-field .input{padding-right:45px}
.password-toggle{position:absolute;right:8px;bottom:5px;width:34px;height:34px;display:grid;place-items:center;border:0;border-radius:9px;background:transparent;color:#7d8aa0}
.password-toggle:hover{background:#edf3fb;color:#3975ef}
.password-toggle[aria-pressed="true"]{color:#3975ef;background:#eaf2ff}
.password-toggle .ui-icon{width:18px;height:18px}
.auth-error{min-height:18px;color:#b5444d;font-size:11px;line-height:1.5}
.auth-submit{width:100%;height:44px;border:0;border-radius:13px;background:linear-gradient(135deg,#3975ef,#71a8ff);color:#fff;font-size:12px;font-weight:800;box-shadow:0 12px 24px rgba(57,117,239,.24);transition:.18s}
.auth-submit:hover{filter:brightness(1.04);transform:translateY(-1px)}
.auth-submit:active{transform:translateY(1px)}
.auth-submit:disabled{opacity:.58;transform:none}
.account-tools{display:flex;align-items:center;gap:7px;margin-left:12px}
.account-user{max-width:160px;padding:7px 10px;border-radius:999px;background:#edf3fb;color:var(--sub);font-size:11px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.top-action{height:32px;padding:0 10px;border:1px solid rgba(82,101,128,.17);border-radius:10px;background:rgba(255,255,255,.65);color:var(--sub);font-size:11px;font-weight:750}
.top-action:hover{background:#fff;border-color:rgba(57,117,239,.3);color:var(--blue)}
.top-action.danger:hover{color:var(--red);border-color:rgba(223,91,99,.28);background:#fff5f6}
.account-modal-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}
.account-modal-head h2{margin:0;font-size:20px}
.account-modal-head p{margin:6px 0 0;color:var(--muted);font-size:11px}
.account-list{display:grid;gap:8px;margin-bottom:20px}
.account-row{display:grid;grid-template-columns:minmax(0,1fr) 90px 145px 70px;gap:10px;align-items:center;padding:12px 13px;border:1px solid var(--line);border-radius:13px;background:rgba(255,255,255,.76)}
.account-row b{font-size:12px;overflow:hidden;text-overflow:ellipsis}
.account-row span{color:var(--sub);font-size:11px}
.role-badge{justify-self:start;padding:4px 8px;border-radius:999px;background:#eaf1fc;color:#3965bf;font-size:11px!important;font-weight:750}
.role-badge.admin{background:#fff0d9;color:#94601b}
.account-delete{height:30px;padding:0 8px;border:1px solid rgba(223,91,99,.2);border-radius:9px;background:#fff5f6;color:#b5444d;font-size:11px;font-weight:750}
.account-delete:hover{background:#fde8eb}
.account-create{padding-top:18px;border-top:1px solid var(--line)}
.account-create h3{margin:0 0 12px;font-size:14px}
.account-create-grid{display:grid;grid-template-columns:1fr 1fr 120px;gap:10px;align-items:end}
.account-create-grid .field{margin:0}
.account-create-submit{height:38px}
.report-overview{align-items:stretch}
.report-progress-card,.report-focus-card{min-height:176px;height:100%;display:flex;flex-direction:column;justify-content:center}
.submit{background:linear-gradient(135deg,#3975ef,#70aaff);box-shadow:0 10px 22px rgba(57,117,239,.22)}
.submit:hover{background:linear-gradient(135deg,#2f63d2,#5f97f0);filter:none}
.quality-resource{padding:14px;border:1px solid var(--line);border-radius:var(--radius-card);background:rgba(255,255,255,.58);box-shadow:var(--card-shadow)}
.quality-resource+.quality-resource{margin-top:11px}
.quality-resource .card-title{margin-bottom:12px}
.quality-model-row{display:grid;grid-template-columns:minmax(0,1fr) 88px;gap:7px}
.quality-model-row .input{min-width:0}
.quality-model-refresh{white-space:nowrap}
.quality-model-row.is-loading .quality-model-refresh:before{content:'';display:inline-block;width:10px;height:10px;margin-right:5px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;vertical-align:-2px;animation:spin .7s linear infinite}
.quality-model-row.is-done .quality-model-refresh{color:#17815b;background:#edf9f4;border-color:rgba(32,168,117,.28)}
.quality-model-row.is-error .quality-model-refresh{color:#b5444d;background:#fff4f5;border-color:rgba(223,91,99,.28)}
@media(max-width:840px){
.account-tools{margin-left:auto}
.account-user{display:none}
.account-create-grid{grid-template-columns:1fr 1fr}
.account-create-grid .account-create-submit{grid-column:1/-1}
.account-row{grid-template-columns:minmax(0,1fr) 80px 70px}
.account-row span.created-at{display:none}
}
@media(max-width:520px){
.auth-card{padding:24px}
.account-create-grid{grid-template-columns:1fr}
.account-row{grid-template-columns:1fr auto}
.account-row .role-badge{grid-row:2}
.account-row .account-delete{grid-row:2;grid-column:2}
.account-modal-head{gap:8px}
}
.main>#detail,.main>#modulePlaceholder,.detail-layout{width:100%;min-width:0;max-width:none;margin:0;padding:0;overflow:visible;}
.detail-layout{display:flow-root;padding-bottom:var(--space-2)}
.overview-shell,.report-head,.report-section,.group-detail-head,.group-progress-card,.report-progress-card,.report-focus-card,.report-decision-hero,.decision-section,.card,.progress-card,.success-card{min-width:0;max-width:100%;padding:var(--card-inset);border:1px solid var(--card-border);
  border-radius:var(--radius-card);box-shadow:var(--card-shadow);}
.overview-shell{margin:0 0 var(--section-gap);padding-bottom:var(--space-2)}
.overview-shell .hero{margin:0 0 var(--space-5)}
.overview-shell .progress-card,.overview-shell .success-card,.overview-shell .metric,.card .metric,.report-section .report-metric,.report-section .method-card,.report-section .quality-health-card,.report-section .quality-delta,.report-section .conclusion-banner,.report-section .decision-section,.report-section .report-decision-hero,.report-section .card,.report-section .quality-resource{box-shadow:none;border-radius:var(--radius-inner);}
.card,.report-section{background:rgba(251,253,255,.9)}
.progress-card,.summary{margin-bottom:var(--section-gap)}
.summary{display:grid}
.cards,.summary,.report-metrics,.metric-grid,.method-grid,.quality-health-grid,.quality-ab{gap:var(--section-gap);min-width:0}
.cards{display:grid;padding-bottom:0;grid-template-columns:repeat(2,minmax(0,1fr))}
.cards>*{min-width:0}
.metric,.report-metric,.method-card,.quality-health-card,.quality-resource,.error-item{padding:var(--space-4);border-radius:var(--radius-inner);border-color:var(--card-border);}
.metric,.report-metric{box-shadow:none}
.metric-value,.report-metric .metric-value{font-size:24px;line-height:1.2}
.next-step-grid{gap:var(--section-gap)}
.next-step,.quality-delta,.decision-badge{padding:var(--space-4);border-radius:var(--radius-inner)}
.next-step{gap:var(--space-3)}
.next-step p,.method-card span{font-size:12px;line-height:1.6}
.table-wrap th,.table-wrap td{padding:var(--space-3);line-height:1.5}
.table-wrap th{font-size:11px}
.table-wrap td{font-size:12px}
.report-metrics-emphasis .metric-primary{box-shadow:none}
.report-head{gap:var(--space-6);margin:0;background:linear-gradient(135deg,rgba(255,255,255,.96),rgba(235,242,251,.85))}
.report-head>div:first-child{min-width:0;flex:1}
.report-head h1,.hero h1{margin:var(--space-2) 0;font-size:28px;line-height:1.3;letter-spacing:-.025em;overflow-wrap:anywhere}
.report-head p,.hero .phase{margin:0;font-size:12px;line-height:1.65;color:var(--sub);overflow-wrap:anywhere}
.report-head p{max-width:none}
.report-meta{gap:var(--space-2);margin-top:var(--space-3)}
.report-status{flex:0 0 auto;min-width:128px;border-radius:var(--radius-inner);padding:var(--space-3)}
.report-tabs{gap:var(--space-2);margin:var(--section-gap) 0;padding:var(--space-1);border-radius:var(--radius-inner);box-shadow:none;max-width:100%;flex-wrap:wrap;top:0}
.report-tab{border:1px solid transparent;border-radius:var(--radius-control);padding:var(--space-3) var(--space-4);font-size:12px;line-height:1.4;min-width:0;max-width:100%;white-space:normal;overflow-wrap:anywhere}
.report-tab.active{box-shadow:var(--control-shadow)}
.report-section{margin:var(--section-gap) 0}
.report-section-title-row,.card-head{gap:var(--space-4);margin-bottom:var(--space-4)}
.report-section-title,.card-title{font-size:15px;line-height:1.5;font-weight:750;margin:0 0 var(--space-1)}
.report-section-sub,.card-sub{font-size:12px;line-height:1.6;margin:var(--space-1) 0 0;color:var(--muted)}
.report-overview{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,.65fr);gap:var(--section-gap);margin:var(--section-gap) 0;align-items:stretch}
.report-progress-card,.report-focus-card{margin:0;min-height:180px;height:auto;display:flex;flex-direction:column;justify-content:center}
.report-progress-line{gap:var(--space-3);margin:var(--space-2) 0 var(--space-3);flex-wrap:wrap}
.report-focus-card strong{white-space:normal;overflow:visible;overflow-wrap:anywhere;margin:var(--space-2) 0;font-size:20px;line-height:1.4}
.report-progress-card p,.report-focus-card small{margin-top:var(--space-3);font-size:12px;line-height:1.6}
.report-card-kicker,.report-kicker{font-size:11px;line-height:1.5;letter-spacing:.06em}
.report-baseline-strip{gap:var(--space-2) var(--space-4);padding:var(--space-3) var(--space-4);margin:0 0 var(--section-gap);border-radius:var(--radius-inner);line-height:1.6}
.group-detail{display:grid;gap:var(--section-gap);width:100%;min-width:0;margin:0;padding:0;border:0;background:transparent;box-shadow:none;overflow:visible}
.group-detail-head{display:flex;gap:var(--space-4);align-items:flex-start;background:rgba(251,253,255,.9)}
.group-detail-head>div{min-width:0}
.group-detail-head h2{font-size:22px;line-height:1.4;margin:var(--space-1) 0 0}
.group-detail-head p{font-size:12px;line-height:1.65;margin:var(--space-2) 0 0}
.group-detail .report-metrics,.group-detail .signal-list,.group-detail .signal,.group-detail>.method-grid,.group-detail>section.report-section,.group-progress-card{margin:0}
.group-detail>section.report-section{padding:var(--card-inset)}
.group-progress-card{background:linear-gradient(135deg,#edf4ff,#f9fbff)}
.group-progress-head{font-size:13px;line-height:1.5}
.group-progress-head b{font-size:24px}
.group-progress-card p{margin:var(--space-3) 0 0;font-size:12px;line-height:1.6}
.group-progress-track,.report-progress-track{height:10px;margin-top:var(--space-3)}
.group-detail .report-metrics{grid-template-columns:repeat(4,minmax(0,1fr))}
.group-detail .report-metric{background:rgba(251,253,255,.9);border-radius:var(--radius-card);box-shadow:var(--card-shadow)}
.report-footer-actions{display:flex;flex-wrap:wrap;gap:var(--space-3);margin:var(--space-5) 0 0;padding:var(--space-4) 0 0;border:0;border-top:1px solid var(--line);border-radius:0;background:transparent;box-shadow:none}
.report-wrap,.table-wrap{border-radius:var(--radius-inner)}
.signal,.decision-scope,.comparison-hint,.form-note{padding:var(--space-3) var(--space-4);border-radius:var(--radius-inner);line-height:1.6}
.ui-icon,.secret-toggle svg{display:inline-block;width:18px;height:18px;flex:0 0 18px;vertical-align:middle;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
button:has(>.ui-icon){display:inline-flex;justify-content:center;align-items:center;gap:var(--space-2)}
.modal-close{display:grid;place-items:center;width:36px;height:36px;padding:0;border-radius:var(--radius-control)}
.modal-close .ui-icon{width:20px;height:20px}
.btn,.top-action,.icon-btn,.secret-action,.secret-toggle,.submit,.auth-submit{border-radius:var(--radius-control);font-size:12px;line-height:1.4;transition:background .16s,border-color .16s,box-shadow .16s,color .16s,transform .16s;white-space:nowrap;}
.btn.primary,.submit,.auth-submit{background:var(--primary-gradient);color:#fff;border-color:transparent;box-shadow:var(--primary-shadow)}
.btn.primary:hover,.submit:hover,.auth-submit:hover{background:linear-gradient(135deg,#2a5fc8,#528ade);color:#fff;filter:none}
button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid rgba(57,117,239,.35);outline-offset:3px}
button:disabled{opacity:.5;cursor:not-allowed;box-shadow:none;transform:none;filter:none}
button:not(:disabled):active{transform:translateY(1px)}
.input,select{font-size:13px;line-height:1.5;border-radius:var(--radius-control);min-height:40px}
.input::placeholder{font-size:inherit;color:var(--muted);opacity:1}
.field label,.group-cell label{font-size:12px;line-height:1.5;margin-bottom:var(--space-2)}
.field{margin-bottom:var(--space-4)}
.auth-form,.account-list{gap:var(--space-4)}
.account-create-grid{gap:var(--space-3)}
.field-hint,.muted-help,.form-note,.form-sub{font-size:12px;line-height:1.6}
.quality-resource+.quality-resource{margin-top:var(--space-4)}
@container detail (max-width:720px){
.report-overview,.report-decision-hero,.decision-section,.summary,.cards{grid-template-columns:minmax(0,1fr)}
.report-metrics,.group-detail .report-metrics,.method-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.report-head{flex-wrap:wrap}
.report-status{min-width:0;text-align:left}
.hero{flex-wrap:wrap}
.hero-actions{justify-content:flex-start}
.report-head h1,.hero h1{font-size:24px}
}
@container detail (max-width:420px){
.report-metrics,.group-detail .report-metrics,.method-grid,.quality-health-grid,.quality-ab{grid-template-columns:minmax(0,1fr)}
.report-progress-line{display:grid}
.group-detail-head{flex-wrap:wrap}
.report-head h1,.hero h1{font-size:22px}
}
@media(max-width:640px){
.healthText{display:none}
.account-tools{margin-left:0}
}
@media(prefers-reduced-motion:reduce){
*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
}
.fact-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:14px}
.routing-metric-table{width:100%;border-collapse:separate;border-spacing:0;margin-bottom:16px;border:1px solid var(--line);border-radius:15px;overflow:hidden;background:rgba(255,255,255,.76)}
.routing-metric-table th,.routing-metric-table td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;font-size:11px}
.routing-metric-table thead th{background:#eef3f8;color:var(--sub);font-size:11px;font-weight:800;white-space:nowrap}
.routing-metric-table tbody th{color:var(--sub);font-weight:750;white-space:nowrap}
.routing-metric-table tr:last-child th,.routing-metric-table tr:last-child td{border-bottom:0}
.routing-metric-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:12px;margin-bottom:12px}
.routing-metric-card{padding:15px 17px;border:1px solid var(--line);border-radius:var(--radius-card);background:rgba(255,255,255,.76)}
.routing-side-pair{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-top:11px}
.routing-side-pair .metric-value{margin-top:0}
.routing-side-arrow{color:var(--muted);font-size:13px}
.routing-metric-card small{display:block;margin-top:5px;color:var(--muted);font-size:11px;line-height:1.5}
.routing-metric-card .metric-value small{display:inline;margin:0;color:var(--muted);font-size:11px}
.report-note{margin:9px 0 0;color:var(--sub);font-size:11px;line-height:1.7}
.notice{border:1px solid rgba(223,91,99,.28);background:#fff3f4;color:#a53b43;border-radius:15px;padding:12px 14px;font-size:12px;margin:14px 0 0}
.notice b{display:block;margin-bottom:4px;font-size:12px}
.notice span{display:block;color:inherit;opacity:.86;font-size:11px;line-height:1.6;margin-top:3px}
.notice.good{border-color:rgba(23,123,89,.26);background:#eefaf4;color:#177b59}
.notice.caution{border-color:rgba(149,97,30,.26);background:#fff8ec;color:#95611e}
.notice.info,.notice:not([class*=good]):not([class*=caution]){border-color:rgba(106,125,152,.24);background:#f3f6fa;color:#50627d}
.fact-card{padding:18px 19px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(150deg,rgba(255,255,255,.94),rgba(243,247,252,.78))}
.fact-headline{margin:8px 0 12px;color:var(--text);font-size:13px;font-weight:750;line-height:1.55}
.fact-list{margin:0;padding:0}
.fact-row{display:grid;grid-template-columns:minmax(96px,1.1fr) minmax(0,1.4fr);gap:10px;align-items:baseline;padding:7px 0;border-top:1px solid var(--line)}
.fact-row:first-child{border-top:0}
.fact-row dt{color:var(--sub);font-size:11px;line-height:1.5}
.fact-row dd{margin:0;min-width:0}
.fact-row dd b{font-size:17px;font-weight:800;letter-spacing:-.02em}
.fact-ratio{margin-left:7px;padding:2px 7px;border-radius:999px;background:#edf3fb;color:#50627d;font-size:11px;font-weight:750;white-space:nowrap}
.fact-row dd small{display:block;margin-top:3px;color:var(--muted);font-size:11px;line-height:1.5}
.layer-notes{margin:14px 0 0;padding-left:18px;color:var(--sub);font-size:11px;line-height:1.7}
.layer-notes li+li{margin-top:5px}
.layer-basis{margin:10px 0 0;color:var(--muted);font-size:11px;line-height:1.6}
.conclusion-banner.advice p{margin:0}
@media(max-width:620px){
.fact-row{grid-template-columns:1fr}
.fact-row dd b{font-size:16px}
}
.quality-score-grid,.quality-scenario-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:14px}
.quality-score-card{border:1px solid var(--line);border-radius:18px;padding:23px;background:linear-gradient(135deg,#fff,#eef4ff)}
.quality-score-number{font-size:30px;font-weight:800;letter-spacing:-.03em;margin:6px 0;color:#3a5680}
.quality-score-number small{font-size:14px;color:var(--muted)}
.quality-score-card.risk .quality-score-number{color:#ae354e}
.quality-score-card p{font-weight:750}
.quality-score-card small,.quality-scenario-grid small{display:block;color:var(--muted);line-height:1.6;margin-top:8px}
.quality-score-facts{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;border-top:1px solid var(--line);padding-top:14px}
.quality-progress-secondary .report-progress-line strong{font-size:22px}
.quality-recommendation{margin-top:12px!important;color:var(--text)!important}
.evidence-pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;padding:15px;border-radius:12px;background:#152338;color:#d9e7ff;font:12px/1.7 ui-monospace,monospace}
.method-card span{overflow-wrap:anywhere}
.report-footer-actions{flex-wrap:wrap}
.quality-suite-note{padding:14px;margin:12px 0;border:1px solid var(--line);border-radius:12px;background:#eef4ff;font-size:12px;line-height:1.7}
.quality-suite-note .btn{margin-top:8px}
.quality-suite-cases summary{cursor:pointer;padding:10px;background:#eef3f8;border-radius:8px;margin:6px 0}
.table-wrap.report-wrap{scrollbar-gutter:stable;min-height:0}
.table-wrap.report-wrap:focus-visible{outline:2px solid var(--blue)}
.modal{padding:16px;overflow:hidden;overscroll-behavior:none;isolation:isolate}
.modal-panel{position:relative;display:flex;flex-direction:column;min-width:0;min-height:0;width:min(1080px,100%);max-width:100%;max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);overflow:hidden;padding:0;border:1px solid rgba(255,255,255,.9);border-radius:22px;background:#f8fafd;box-shadow:0 28px 80px rgba(25,40,62,.28)}
#accountModal .modal-panel{width:min(680px,100%)}
.modal-head,.account-modal-head{flex:none;min-width:0;margin:0;padding:20px 24px 16px;border-bottom:1px solid var(--line);background:#f8fafd}
.modal-head>div,.account-modal-head>div{min-width:0}
.modal-head h2,.account-modal-head h2{overflow-wrap:anywhere;font-size:21px;line-height:1.4}
.modal-close,.modal-head .drawer-close,.account-modal-head .drawer-close{flex:none}
.modal-body{flex:1 1 auto;min-height:0;min-width:0;max-width:100%;overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding:20px 24px 24px;border-radius:0 0 21px 21px;overflow-wrap:anywhere}
.modal-body>*{min-width:0;max-width:100%}
.modal-body:focus-visible{outline:2px solid var(--blue);outline-offset:-3px}
.raw-grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
.raw-grid>div{min-width:0}
.raw-grid pre{min-width:0;min-height:180px;max-height:52vh;max-height:52dvh;overscroll-behavior:contain;scrollbar-gutter:stable}
.modal .evidence-pre,.modal .raw-block{min-width:0;max-width:100%;overscroll-behavior:contain;scrollbar-gutter:stable}
body.modal-open{overflow:hidden}
body.modal-open .main,body.modal-open .sidebar,body.modal-open #config{overflow:hidden!important}
.methodology-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:16px 0}
.methodology-card{min-width:0;border:1px solid var(--line);border-radius:14px;padding:16px;background:white;font-size:12px;line-height:1.75}
.methodology-card h3{margin:0 0 8px;font-size:14px}
.methodology-card p{margin:6px 0}
.methodology-card a{color:#285ec6;text-underline-offset:3px;overflow-wrap:anywhere}
.methodology-steps{padding-left:24px;line-height:1.9;font-size:13px}
.methodology-status{display:inline-block;padding:3px 8px;border-radius:6px;background:#edf3fb;font-size:11px;color:#435b78}
@media(max-width:680px){
.modal{padding:10px}
.modal-panel{max-height:calc(100vh - 20px);max-height:calc(100dvh - 20px);border-radius:18px}
.modal-head,.account-modal-head{padding:16px;gap:12px}
.modal-head h2{font-size:18px}
.modal-body{padding:16px;border-radius:0 0 17px 17px}
.raw-grid,.methodology-grid{grid-template-columns:minmax(0,1fr)}
.raw-grid pre{min-height:140px;max-height:42vh;max-height:42dvh}
}
/* 工作区只在这里改变布局；报告的内容断点保留于对应组件。 */
@media(max-width:1180px){.crumb{display:none}.layout{grid-template-columns:164px 244px minmax(0,1fr)}.top{padding:12px 16px;gap:10px}.health #healthText{display:none}.account-tools{gap:6px;margin-left:6px}.account-user{max-width:110px}}
@media(min-width:641px) and (max-width:980px){
  .layout{grid-template-columns:224px minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);grid-template-areas:"navigation navigation" "records detail"}
  .sidebar{height:auto;display:flex;padding:10px 14px;border-right:0;border-bottom:1px solid var(--line)}
  .sidebar .section-title,.sidebar .nav-note{display:none}
  .module-nav{flex:1;flex-direction:row;gap:8px}.module-nav-item{flex:1;width:auto;justify-content:center;padding:8px 10px}.module-nav-item small{display:none}.module-icon{width:26px;height:26px;flex-basis:26px}
  .record-panel{padding:20px 12px}.record-panel-head h2{font-size:20px}
  .top{flex-wrap:wrap}.account-tools{margin-left:auto}
}
@media(max-width:640px){
  :root{--detail-inset:16px;--card-inset:16px}
  body{padding:0;overflow:auto}.app{height:auto;min-height:100dvh;border:0;border-radius:0;grid-template-rows:auto 1fr}
  .top{min-height:60px;padding:12px 14px;flex-wrap:wrap;gap:8px}.brand{font-size:15px}.logo{width:30px;height:30px}.crumb{display:none}.health{margin-left:auto}.health #healthText{display:none}.account-tools{width:100%;margin:3px 0 0;flex-wrap:wrap;gap:6px}.account-user{display:none}.top-action{flex:1;padding:0 7px;font-size:11px}
  .layout{display:block;height:auto}.sidebar{height:auto;padding:8px 10px;border-right:0;border-bottom:1px solid var(--line)}.sidebar .section-title,.sidebar .nav-note{display:none}.module-nav{flex-direction:row;gap:4px}.module-nav-item{flex:1;width:auto;justify-content:center;padding:10px 6px;gap:6px}.module-nav-item b{font-size:12px}.module-nav-item small{display:none}.module-icon{width:25px;height:25px;flex-basis:25px}
  .record-panel{height:auto;max-height:340px;padding:18px 16px;border-right:0;border-bottom:1px solid var(--line)}.record-panel-head{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;margin-bottom:16px}.record-panel-head h2{font-size:18px;margin:2px 0}.record-kicker,.record-subtitle{display:none}.record-create-button{width:auto;min-width:120px;padding:0 12px;font-size:12px}.record-summary{margin-bottom:12px;padding-bottom:10px}.workspace-subnav.record-filter{margin-bottom:12px}.task-section-title{display:none}.task-list{display:flex;flex-direction:column;gap:8px}.task-empty{min-height:100px;padding:14px}.main{height:auto;min-height:450px;scrollbar-gutter:auto}.empty{min-height:400px;padding:28px 12px}.empty b{font-size:20px}
  .config{width:100vw;border-radius:0;border-left:0}.drawer-head{padding:20px 20px 16px}.drawer-body{padding:20px}.drawer-footer{padding:14px 20px 18px}.drawer-head .form-title{font-size:20px}.drawer-head .form-sub{line-height:1.55}.group-row{gap:12px;padding:14px}.form-section-title{margin-top:22px}
}
@media(max-width:380px){.group-row,.inline{grid-template-columns:minmax(0,1fr)}.group-row .group-name-cell,.group-row .group-remove{grid-column:auto}.module-icon{display:none}.record-create-button{min-width:0}.secret-row{gap:6px;grid-template-columns:minmax(0,1fr) auto 36px}.drawer-head,.drawer-body,.drawer-footer{padding-left:16px;padding-right:16px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}

/* Shared single-select geometry across every module and dialog. */
:root{
  --select-arrow-inset:14px;
  --select-arrow-size:16px;
  --select-content-end:42px;
  --select-arrow:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none'%3E%3Cpath d='m4 6 4 4 4-4' stroke='%2362738a' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
}
:root select:not([multiple]):not([size]),:root select[size="1"]:not([multiple]){
  -webkit-appearance:none;
  appearance:none;
  background-image:var(--select-arrow);
  background-repeat:no-repeat;
  background-position:right var(--select-arrow-inset) center;
  background-size:var(--select-arrow-size) var(--select-arrow-size);
  padding-right:var(--select-content-end);
  text-overflow:ellipsis;
  cursor:pointer;
}
:root select:disabled{cursor:not-allowed}
@media(forced-colors:active){
  :root select:not([multiple]):not([size]),:root select[size="1"]:not([multiple]){
    -webkit-appearance:auto;appearance:auto;background-image:none;padding-right:12px;
  }
}
</style>
<link rel="stylesheet" href="/assets/fingerprint.css?v=20260916.1">
</head>
<body>
<div class="auth-gate" id="authGate" hidden>
  <section class="auth-card" aria-labelledby="authTitle">
    <div class="auth-mark">R</div>
    <h1 id="authTitle">登录网关压测工作台</h1>
    <p id="authHint">请输入系统管理员分发的账号。首次使用时需要先初始化管理员账号。</p>
    <form class="auth-form" id="authForm">
      <div class="field"><label for="authUsername">账号</label><input class="input" id="authUsername" autocomplete="username" required placeholder="输入账号"></div>
      <div class="field password-field"><label for="authPassword">密码</label><input class="input" id="authPassword" type="password" autocomplete="current-password" required placeholder="至少 8 个字符"><button class="password-toggle" type="button" aria-label="显示密码" aria-pressed="false" onclick="togglePasswordVisibility('authPassword',this)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></svg></button></div>
      <div class="field password-field" id="authConfirmField" hidden><label for="authConfirm">确认密码</label><input class="input" id="authConfirm" type="password" autocomplete="new-password" placeholder="再次输入管理员密码"><button class="password-toggle" type="button" aria-label="显示密码" aria-pressed="false" onclick="togglePasswordVisibility('authConfirm',this)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></svg></button></div>
      <div class="auth-error" id="authError" aria-live="polite"></div>
      <button class="auth-submit" id="authSubmit" type="submit">登录</button>
    </form>
  </section>
</div>
<div class="app">
<header class="top"><div class="logo">R</div><div class="brand">网关压测工作台</div><div class="crumb" id="crumb">黑盒请求执行与观测</div><div class="health" id="health"><span class="health-dot"></span><span id="healthText">服务正常</span></div><div class="account-tools"><button class="top-action" type="button" onclick="openDiagnostics()">运行诊断</button><button class="top-action" type="button" onclick="openStorageModal()">存储与结果文件</button><span class="account-user" id="accountUser"></span><button class="top-action" id="accountManage" type="button" onclick="openAccountModal()" hidden>账号管理</button><button class="top-action danger" type="button" onclick="logout()">退出登录</button></div></header>
<div class="layout">
<aside class="sidebar" aria-label="测试模块导航"><div class="section-title">产品模块</div><div class="module-nav" aria-label="测试模块"><button class="module-nav-item active" data-module="gateway" type="button"><span class="module-icon"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V5M4 19h16M8 14l4-5 4 3 4-7"/></svg></span><span><b>网关压测</b><small>吞吐与稳定性</small></span></button><button class="module-nav-item" data-module="routing" type="button"><span class="module-icon"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h6l4 10h6"/><path d="M4 17h4l3-6"/><circle cx="18" cy="17" r="2"/></svg></span><span><b>路由对照</b><small>固定与智能路由</small></span></button><button class="module-nav-item" data-module="quality" type="button"><span class="module-icon"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/></svg></span><span><b>质量评测</b><small>能力与一致性</small></span></button><button class="module-nav-item" data-module="fingerprint" type="button"><span class="module-icon"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 11a8 8 0 0 1 16 0v3M7 20c2-3 2-5 2-9a3 3 0 0 1 6 0c0 5-1 8-3 11M4 16c1-2 1-3 1-5M12 11c0 4 0 5-1 7M17 20c1-3 1-6 1-9a6 6 0 0 0-12 0"/></svg></span><span><b>模型指纹</b><small>行为分布与通道自检</small></span></button></div><div class="nav-note"><span class="nav-note-dot"></span><span>先选择模块，再在模块内创建和管理测试</span></div></aside><aside class="record-panel" id="recordPanel" aria-label="当前模块记录"><div class="record-panel-head"><div><div class="record-kicker">测试记录</div><h2 id="recordTitle">压测记录</h2><div class="record-subtitle" id="recordSubtitle">网关吞吐与稳定性测试</div></div><button class="new-btn record-create-button" id="recordCreateButton" onclick="openCreateForContext()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg><span>新建压测</span></button></div><section class="workspace-subnav record-filter" id="gatewaySubnav" aria-label="网关压测记录分类"><button class="subnav-item active" data-view="single" type="button"><span>单次压测 <em id="gatewaySingleCount">0</em></span></button><button class="subnav-item" data-view="compare" type="button"><span>对照实验 <em id="gatewayCompareCount">0</em></span></button></section><section class="workspace-subnav record-filter" id="qualitySubnav" aria-label="质量评测记录分类" hidden><button class="subnav-item active" data-view="single" type="button"><span>单资源测试 <em id="qualitySingleCount">0</em></span></button><button class="subnav-item" data-view="compare" type="button"><span>资源对照 <em id="qualityCompareCount">0</em></span></button></section><section class="record-summary" id="routingSubnav" aria-label="路由对照记录" hidden><span>全部对照记录</span><span class="record-count" id="routingCount">0</span></section><div class="section-title task-section-title" id="recordListLabel" hidden>压测记录</div><div class="task-list" id="gatewayList"></div><div class="task-list" id="qualityList" hidden></div><div class="task-list" id="routingList" hidden></div></aside>
<main class="main"><div class="loading-state" id="loadingState"><div class="loading-spinner" aria-hidden="true"></div><b>正在恢复任务记录</b><span>正在读取本地任务索引，请稍候…</span></div><div class="empty" id="empty" hidden><div><div class="empty-visual"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V5M4 19h16M8 14l4-5 4 3 4-7"/></svg></div><b id="emptyTitle">还没有可查看的测试</b><span id="emptyHint">当前工作区还没有记录。点击左侧按钮开始。</span></div></div><div id="detail" class="detail-layout" hidden></div><div id="modulePlaceholder" hidden></div></main>
</div>
</div>
<div class="drawer-backdrop" id="drawerBackdrop" onclick="toggleConfig(false)" hidden></div>
<aside class="config" id="config" role="dialog" aria-modal="true" aria-labelledby="createTitle" aria-hidden="true" inert>
  <header class="drawer-head"><div><div class="form-title" id="createTitle">新建压测</div><div class="form-sub" id="createSubtitle">配置网关连接和压力参数，开始后查看实时结果。</div></div><button class="drawer-close" type="button" onclick="toggleConfig(false)" aria-label="关闭新建测试"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button></header>
  <div class="drawer-body">
    <div id="templatePicker" class="template-picker" hidden><div class="template-picker-head"><span class="template-picker-title">快速填入压力参数</span><button class="text-btn" type="button" id="templatePickerToggle" aria-expanded="true">收起</button></div><div class="template-picker-body" id="templatePickerBody"><div class="field-hint">正在读取模板…</div></div><p class="template-picker-note">模板只填入并发、速率与时长；连接信息保留。</p></div>
    <div id="routingFields" class="routing-only" hidden>
      <section class="routing-form-scope">
        <div class="field"><label for="routingName">对照名称</label><input class="input" id="routingName" placeholder="例如：智能路由与固定路由 · 电商问答"></div>
        <h3 class="form-section-title">1 <span>连接两条路由</span></h3>
        <p class="field-hint">为两条路由分别填写连接信息。切换标签会保留已填内容。</p>
        <div class="tabs" id="routingSideTabs" role="tablist" aria-label="路由连接配置"><button class="tab active" id="routingBaselineTab" role="tab" aria-selected="true" aria-controls="routing-baseline-panel" data-routing-tab="baseline" type="button">固定路由<span class="routing-side-status">待填写</span></button><button class="tab" id="routingCandidateTab" role="tab" aria-selected="false" aria-controls="routing-candidate-panel" tabindex="-1" data-routing-tab="candidate" type="button">智能路由<span class="routing-side-status">待填写</span></button></div>
        <div id="routingSidePanels"></div>
        <h3 class="form-section-title">2 <span>设置共同参数</span></h3>
        <div class="field"><label for="routingModel">共同模型 <span class="field-required">必填</span></label><input class="input" id="routingModel" required autocomplete="off" placeholder="输入两条路由都支持的模型名称"><div class="field-hint">两侧使用同一个模型名称；请确认两条路由均支持该模型。</div></div>
        <div class="field"><label for="routingTier">每侧请求数</label><select class="input" id="routingTier" required aria-describedby="routingTierHint"></select><div class="field-hint" id="routingTierHint" aria-live="polite">正在读取测试集…</div><button class="text-btn" type="button" id="routingCorpusRetry" onclick="loadRoutingCorpus()" hidden>重新加载测试集</button></div>
        <div class="inline"><div class="field"><label for="routingConcurrency">每侧并发上限</label><input class="input" id="routingConcurrency" type="number" min="1" max="2000" required value="20"></div><div class="field"><label for="routingRpm">每侧每分钟请求数</label><input class="input" id="routingRpm" type="number" min="0.01" max="600000" step="any" required value="600"></div></div>
        <div class="comparison-hint">两侧按相同清单、节奏和并发交错发出请求，再按请求编号配对比较。</div>
        <details class="routing-advanced"><summary>超时与原始记录</summary><div class="inline"><div class="field"><label for="routingConnectTimeout">连接超时（秒）</label><input class="input" id="routingConnectTimeout" type="number" min="1" max="120" required value="10"></div><div class="field"><label for="routingReadTimeout">读取超时（秒）</label><input class="input" id="routingReadTimeout" type="number" min="1" max="600" required value="60"></div></div><div class="adv-toggle"><div class="switch-copy"><div class="switch-title">保存原始输入与输出</div><div class="switch-txt">便于逐条核对结果，会增加本地存储占用。</div></div><label class="switch"><input type="checkbox" id="routingCapture" aria-label="保存路由对照原始输入与输出"><span class="slider"></span></label></div></details>
      </section>
    </div>
    <div class="field gateway-single-name"><label for="name">任务名称</label><input class="input" id="name" placeholder="例如：生产网关晚高峰"></div><div class="field gateway-connection-field"><label for="url">网关 URL（统一资源定位符）</label><input class="input" id="url" placeholder="https://gateway.example.com/v1"></div><div class="field gateway-connection-field"><label for="api_key">API Key（应用程序编程接口密钥）</label><div class="secret-row"><input class="input secret-input" id="api_key" type="password" autocomplete="off" spellcheck="false" placeholder="输入新密钥；已保存密钥可留空复用"><button class="secret-action" id="copyKey" type="button" aria-label="复制 API Key（应用程序编程接口密钥）" title="复制 API Key（应用程序编程接口密钥）">复制</button><button class="secret-toggle" id="toggleKey" type="button" aria-label="显示 API Key（应用程序编程接口密钥）" aria-pressed="false" title="显示 API Key（应用程序编程接口密钥）"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.2 12s3.4-5.3 9.8-5.3 9.8 5.3 9.8 5.3-3.4 5.3-9.8 5.3S2.2 12 2.2 12Z"></path><circle cx="12" cy="12" r="2.7"></circle></svg></button></div><div class="field-hint" id="keyInputHint">密钥不会写入任务记录；保存后刷新页面可按 URL 自动复用。</div></div><div class="field gateway-connection-field"><label for="modelSelect">模型</label><div class="model-row"><select class="input model-select" id="modelSelect" aria-describedby="modelListHint"><option value="">请先填写 URL 和 API Key，再获取模型</option></select><button class="icon-btn" id="loadModels" type="button" title="刷新并获取模型列表">刷新模型</button></div><div class="model-fallback" id="modelFallback"><button class="text-btn" id="manualModelToggle" type="button" aria-expanded="false">无法获取列表？手动指定模型</button></div><div id="modelManualWrap" hidden><input class="input" id="model" placeholder="输入模型名称" autocomplete="off"><div class="field-hint" id="modelHint">仅在网关不支持模型列表时使用；手动输入不会自动校验模型是否存在。</div></div><div class="field-hint" id="modelListHint" aria-live="polite">尚未获取模型列表。</div></div><div class="tabs gateway-single-pressure" role="tablist"><button class="tab active" data-mode="smoke">冒烟</button><button class="tab" data-mode="ramp">阶梯</button><button class="tab" data-mode="soak">长跑</button></div><div id="modeFields" class="gateway-single-pressure"></div><div class="field gateway-single-pressure single-only"><label for="concurrency">并发上限</label><input class="input" id="concurrency" type="number" min="1" max="2000" value="20"><div class="muted-help">实际完成能力约等于：并发数 ÷ 单条平均耗时。</div></div>
<details class="gateway-single-advanced"><summary>高级选项</summary><div class="inline" style="margin-top:14px"><div class="field"><label for="connect_timeout">连接超时（秒）</label><input class="input" id="connect_timeout" type="number" value="10"></div><div class="field"><label for="read_timeout">读取超时（秒）</label><input class="input" id="read_timeout" type="number" value="60"></div></div><div class="field"><label for="profile">请求内容</label><select id="profile"><option value="smoke">常规短问答</option><option value="short">最短响应</option><option value="long">长输入</option></select></div><div class="adv-toggle"><div class="switch-copy"><div class="switch-title">记录所有请求的原始数据</div><div class="switch-txt">开启后每条 JSONL（逐行 JSON）都会包含原始输入与输出；单条响应最多记录 1MB。</div></div><label class="switch"><input type="checkbox" id="capture_raw"><span class="slider"></span></label></div></details>
    <div id="comparisonFields" hidden></div>
    <div id="preflightCard" class="preflight-card" aria-live="polite" hidden></div>
  </div>
  <footer class="drawer-footer"><div class="form-status" id="formStatus" role="status" aria-live="polite"></div><button class="submit" id="submit" type="button">创建并开始压测</button><div class="form-note">开始后会向所填目标发送真实请求，请核对配置。</div></footer>
</aside>
<div class="modal" id="qualityMethodologyModal" hidden><div class="modal-backdrop" onclick="closeModal('qualityMethodologyModal')"></div><section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="qualityMethodologyTitle"><header class="modal-head"><div><div class="modal-kicker">评分依据 · 实验设计 · 官方来源</div><h2 id="qualityMethodologyTitle">评测方法与参考</h2></div><button class="modal-close" type="button" onclick="closeModal('qualityMethodologyModal')" aria-label="关闭评测方法">×</button></header><div class="modal-body" id="qualityMethodologyBody" tabindex="0"></div></section></div>
<div class="modal" id="qualityEvidenceModal" hidden><div class="modal-backdrop" onclick="closeQualityEvidence()"></div><section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="qualityEvidenceTitle"><header class="modal-head"><h2 id="qualityEvidenceTitle">逐题证据</h2><button class="modal-close" onclick="closeQualityEvidence()" aria-label="关闭逐题证据">×</button></header><div class="modal-body" id="qualityEvidenceBody" tabindex="0"></div></section></div>
<div class="modal" id="qualitySuiteModal" hidden><div class="modal-backdrop" onclick="closeQualitySuite()"></div><section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="qualitySuiteTitle"><header class="modal-head"><h2 id="qualitySuiteTitle">题集与评分规则</h2><button class="modal-close" onclick="closeQualitySuite()" aria-label="关闭题集">×</button></header><div class="modal-body" id="qualitySuiteBody" tabindex="0"></div></section></div>
<div class="modal" id="storageModal" hidden>
  <div class="modal-backdrop" onclick="closeStorageModal()"></div>
  <section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="storageTitle">
    <header class="modal-head">
      <div><div class="modal-kicker">本机结果文件</div><h2 id="storageTitle">存储占用与清理</h2></div>
      <button class="modal-close" type="button" onclick="closeStorageModal()" aria-label="关闭存储面板"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button>
    </header>
    <div class="modal-body" tabindex="0"><div id="storageBody"><div class="field-hint">正在读取本机结果文件占用…</div></div></div>
  </section>
</div>
<div class="modal" id="diagnosticsModal" hidden>
  <div class="modal-backdrop" onclick="closeDiagnostics()"></div>
  <section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="diagnosticsTitle">
    <header class="modal-head">
      <div><div class="modal-kicker">调度 · 事件 · 审计</div><h2 id="diagnosticsTitle">运行诊断</h2></div>
      <button class="modal-close" type="button" onclick="closeDiagnostics()" aria-label="关闭运行诊断"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button>
    </header>
    <div class="modal-body" tabindex="0">
      <div class="diag-tools"><button class="btn" type="button" onclick="loadDiagnostics()">刷新</button><label class="diag-limit">读取条数<input class="input" id="diagLimit" type="number" min="10" max="500" step="10" value="50" onchange="loadDiagnostics()"></label></div>
      <div class="diag-grid" id="diagnosticsBody"><div class="field-hint">正在读取调度状态、事件与审计记录…</div></div>
    </div>
  </section>
</div>
<div class="modal" id="rawModal" hidden>
  <div class="modal-backdrop" onclick="closeRaw()"></div>
  <section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="rawTitle">
    <header class="modal-head">
      <div><div class="modal-kicker">原始请求记录</div><h2 id="rawTitle">请求原始输入与输出</h2></div>
      <button class="modal-close" type="button" onclick="closeRaw()" aria-label="关闭原始数据"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button>
    </header>
    <div class="modal-body" tabindex="0"><div class="raw-grid">
      <div><div class="raw-label">输入</div><pre id="rawInput"></pre></div>
      <div><div class="raw-label">输出</div><pre id="rawOutput"></pre></div>
    </div></div>
  </section>
</div>
<div class="modal" id="accountModal" hidden><div class="modal-backdrop" onclick="closeAccountModal()"></div><section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="accountModalTitle"><div class="account-modal-head"><div><h2 id="accountModalTitle">账号管理</h2><p>系统管理员创建并分发使用者账号；密码只在创建时提交，不会在页面回显。</p></div><button class="drawer-close" type="button" onclick="closeAccountModal()" aria-label="关闭账号管理"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button></div><div class="modal-body" tabindex="0"><div class="account-list" id="accountList"></div><form class="account-create" id="accountCreateForm"><h3>创建账号</h3><div class="account-create-grid"><div class="field"><label for="newUsername">账号</label><input class="input" id="newUsername" minlength="3" required placeholder="使用者账号"></div><div class="field password-field"><label for="newPassword">初始密码</label><input class="input" id="newPassword" type="password" minlength="8" required placeholder="至少 8 个字符"><button class="password-toggle" type="button" aria-label="显示密码" aria-pressed="false" onclick="togglePasswordVisibility('newPassword',this)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></svg></button></div><div class="field"><label for="newRole">角色</label><select class="input" id="newRole"><option value="user">使用者</option><option value="admin">管理员</option></select></div><button class="submit account-create-submit" type="submit">创建账号</button></div><div class="form-status" id="accountStatus" aria-live="polite"></div></form></div></section></div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script src="/assets/fingerprint.js?v=20260916.1"></script>
<script>
const uiIconPaths={"gateway": "<path d=\"M4 19V5M4 19h16M8 14l4-5 4 3 4-7\"/>", "quality": "<path d=\"M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z\"/><path d=\"m8 12 3 3 5-6\"/>", "routing": "<path d=\"M4 7h6l4 10h6\"/><path d=\"M4 17h4l3-6\"/><circle cx=\"18\" cy=\"17\" r=\"2\"/>", "plus": "<path d=\"M12 5v14M5 12h14\"/>", "close": "<path d=\"m6 6 12 12M6 18 18 6\"/>"};
function uiIcon(name){return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${uiIconPaths[name]||uiIconPaths.plus}</svg>`}

const $=id=>document.getElementById(id);let selected=null,detailTask=null,mode='smoke',tasks=[],comparisons=[],refreshing=false,refreshSeq=0;let modelFetchState='idle',modelSource='none',modelList=[],modelRequestSeq=0;const detailState={id:null,scrollTop:0};
const statusNames={draft:'草稿',running:'运行中',stopping:'正在停止',waiting:'排队中',finished:'已完成',finished_with_errors:'完成但有错误',stopped:'已停止',error:'失败',expired:'结果已过期',archived:'已归档'};
const modeTemplates={smoke:`<div class="inline"><div class="field"><label for="rpm">计划调度速率（RPM）</label><input class="input" id="rpm" type="number" min="1" value="600"><div class="muted-help">这是配置的期望调度速率，不代表请求已实际启动或完成。</div></div><div class="field"><label for="iterations">计划请求条数</label><input class="input" id="iterations" type="number" min="1" value="50"></div></div>`,ramp:`<div class="field"><label for="ramps">各档计划调度速率（RPM）</label><input class="input" id="ramps" value="600,1200,2400"><div class="muted-help">逗号分隔；每档按配置速率尝试调度，受并发与队列背压影响。</div></div><div class="field"><label for="duration">每档计划调度时长（秒）</label><input class="input" id="duration" type="number" min="1" value="60"></div>`,soak:`<div class="inline"><div class="field"><label for="rpm">计划调度速率（RPM）</label><input class="input" id="rpm" type="number" min="1" value="600"></div><div class="field"><label for="duration">计划调度时长（秒）</label><input class="input" id="duration" type="number" min="1" value="1800"></div></div>`};
let createKind='gateway-single', activeModule='gateway', activeView='single', selectedComparison=null, selectedQuality=null, selectedRouting=null, qualityTests=[];
const gatewayFormIds=['name','url','api_key','modelSelect','modelManualWrap','profile','concurrency','capture_raw'];
function gatewayFormNodes(){const nodes=[];for(const id of gatewayFormIds){const el=$(id);if(!el)continue;const root=el.closest('.field')||el.closest('details')||el.closest('.tabs')||el.closest('#modeFields')||el;if(!nodes.includes(root))nodes.push(root)}const mode=$('modeFields');if(mode&&!nodes.includes(mode))nodes.push(mode);return nodes}
function isGatewayCreate(){return createKind.startsWith('gateway-')}function isQualityCreate(){return createKind.startsWith('quality-')}function isRoutingCreate(){return createKind==='routing-compare'}function isCompareCreate(){return createKind.endsWith('-compare')&&!isRoutingCreate()}
function setCreateKind(next,forceRender=false){
 const allowed=['gateway-single','gateway-compare','quality-single','quality-compare','routing-compare'];
 const normalized=allowed.includes(next)?next:'gateway-single',changed=createKind!==normalized;createKind=normalized;
 const quality=isQualityCreate(),routing=isRoutingCreate(),comparison=isCompareCreate(),single=isGatewayCreate()&&!comparison;
 document.querySelectorAll('.gateway-single-only,.gateway-single-name,.gateway-single-pressure,.single-only').forEach(node=>node.hidden=!single);
 document.querySelectorAll('.gateway-connection-field').forEach(node=>node.hidden=!isGatewayCreate());
 document.querySelectorAll('.gateway-single-advanced').forEach(node=>node.hidden=quality||routing);
 document.querySelectorAll('.routing-only').forEach(node=>node.hidden=!routing);
 $('comparisonFields').hidden=!(quality||(isGatewayCreate()&&comparison));
 if(changed||forceRender){if(routing)renderRoutingFields(true);else if(quality)renderQualityFields(comparison);else if(comparison)renderComparisonFields()}
 const title=routing?'新建路由对照':quality?(comparison?'新建资源对照':'新建单资源评测'):(comparison?'新建压测对照':'新建压测');
 $('templatePicker').hidden=quality||routing||comparison;
 $('createTitle').textContent=title;
 $('createSubtitle').textContent=routing?'比较固定路由与智能路由的成功率、延迟和缓存表现。':quality?'使用同一题集评估模型能力，逐题查看得分与证据。':comparison?'共用一个网关，比较不同压力参数下的表现。':'配置网关连接和压力参数，开始后查看实时结果。';
 $('submit').textContent=routing?'开始路由对照':quality?(comparison?'开始资源对照':'开始质量评测'):(comparison?'开始压测对照':'创建并开始压测');
}
function renderComparisonFields(){if(!$('comparisonFields'))return;$('comparisonFields').innerHTML='<div class="comparison-form-scope"><div class="comparison-hint"><b>对照实验配置</b><span>公共连接信息在上方填写一次；压力变量只在实验组内设置。实验名称用于标识整条对照实验，组名只用于区分基准组和对比组。</span></div><div class="field"><label for="comparisonName">实验名称</label><input class="input" id="comparisonName" placeholder="例如：并发 40 与 100 的压力对照"></div><div class="field"><div class="field-label-row"><label>实验组参数</label><button class="text-btn" type="button" onclick="addComparisonGroup()">＋ 添加对比组</button></div><div class="comparison-fields" id="groupFields"></div><div class="field-hint">每组的并发上限、计划调度 RPM（每分钟请求数）和请求条数都会生效；建议每次只改变一个压力变量。</div></div><div class="adv-toggle comparison-capture"><div class="switch-copy"><div class="switch-title">记录全部实验组原始数据</div><div class="switch-txt">开启后，各组每条请求的输入与输出都会写入 JSONL（逐行 JSON）文件。</div></div><label class="switch"><input type="checkbox" id="comparisonCapture"><span class="slider"></span></label></div></div>';$('groupFields').innerHTML='';[['基准组','baseline',40],['对比组 1','comparison',100]].forEach((x,i)=>addComparisonGroup(x[0],x[1],x[2],i+1))}
function addComparisonGroup(name='对比组',role='comparison',concurrency=100,index=null){const list=$('groupFields');if(!list||list.children.length>=4)return;const n=index||list.children.length+1;const row=document.createElement('div');row.className='group-row';row.dataset.groupIndex=n;row.innerHTML=`<div class="field group-cell group-name-cell"><label>组名</label><input class="input group-name" value="${esc(name)}"></div><div class="field group-cell"><label>角色</label><select class="input group-role"><option value="baseline" ${role==='baseline'?'selected':''}>基准组</option><option value="comparison" ${role!=='baseline'?'selected':''}>对比组</option></select></div><div class="field group-cell"><label>并发上限</label><input class="input group-concurrency" type="number" min="1" max="2000" value="${concurrency}"></div><div class="field group-cell"><label>计划 RPM</label><input class="input group-rpm" type="number" min="1" value="600"></div><div class="field group-cell"><label>请求数</label><input class="input group-iterations" type="number" min="1" value="20"></div><button class="icon-btn group-remove" type="button" title="移除实验组" aria-label="移除实验组" onclick="this.closest('.group-row').remove();normalizeComparisonRoles()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button>`;list.appendChild(row);normalizeComparisonRoles()}
function normalizeComparisonRoles(){const rows=[...document.querySelectorAll('.group-row')];if(!rows.length)return;const bases=rows.filter(r=>r.querySelector('.group-role')?.value==='baseline');if(!bases.length)rows[0].querySelector('.group-role').value='baseline';if(bases.length>1)bases.slice(1).forEach(r=>r.querySelector('.group-role').value='comparison');rows.forEach((r,i)=>{const input=r.querySelector('.group-name');if(input&&!input.value.trim())input.value=r.querySelector('.group-role')?.value==='baseline'?'基准组':`对比组 ${i}`})}
function renderQualityFields(compare=false){
 const host=$('comparisonFields');
 host.innerHTML=`<div class="comparison-hint">${compare?'对两个资源使用同一题集；也可复用同题集的历史参考结果。':'对单个资源评测能力、格式遵循与一致性。'}</div><div class="field"><label for="qualityName">评测名称</label><input class="input" id="qualityName" placeholder="例如：模型能力基线"></div><div class="field"><label><input type="checkbox" id="qualityCapture"> 保存每道题的原始输入与输出</label><div class="field-hint">默认关闭；开启后可在详情中逐题核对。</div></div><div class="quality-suite-note" id="qualitySuiteSummary">正在读取题集…</div><div id="qualityResourceFields"></div>`;
 $('qualityResourceFields').innerHTML=qualityResourceTemplate(0)+(compare?'<div class="field" id="qualityReuseField"><label for="qualityReuse">参考资源来源</label><select class="input" id="qualityReuse"><option value="">填写参考资源，本次一起评测</option></select><div class="field-hint">复用历史结果时，只请求资源 A；参考结果不重新运行。</div><div id="qualityReusePreview"></div></div>'+qualityResourceTemplate(1):'');
 loadQualitySuiteSummary(compare);if(compare)loadQualityReferenceRecords();
}

let currentQualitySuite=null;
async function loadQualitySuiteSummary(compare){
 try{
  const d=await api('/quality-suite');currentQualitySuite=d;const box=$('qualitySuiteSummary');if(!box)return;
  const m=d.manifest,reused=Boolean($('qualityReuse')?.value),count=reused?1:compare?2:1;
  box.innerHTML=`<b>${esc(m.title)}</b><p>${m.case_count} 题 · 本次请求 ${count} 个资源 · 合计 ${m.case_count*count} 次请求${reused?'；参考侧复用历史结果':''}</p><div class="suite-actions"><button class="btn" type="button" onclick="showQualitySuite()">查看全部题目、答案与规则</button><button class="btn" type="button" onclick="showQualityMethodology()">评测方法与官方参考</button></div><details class="suite-details"><summary>执行方式与评分说明</summary><p>${m.categories.length} 个维度；输入 ${m.input_chars?.min??'—'}～${m.input_chars?.max??'—'} 字符／题。每题请求一次，不自动重试；可停止后续题目。</p><p>${reused?'参考资源不重新请求，使用其已保存的同题集证据。':compare?'同题配对，逐题交替两侧的请求顺序。':'单资源按题序执行，侧重能力诊断。'}</p><p>请求可能计入上游费用；字符数不等同于令牌数。</p></details>`;
 }catch(e){if($('qualitySuiteSummary'))$('qualitySuiteSummary').textContent='题集读取失败：'+e.message}
}
const modalStack=[];
function bytesText(n){const v=Number(n);if(!Number.isFinite(v)||v<0)return '—';const units=['B','KB','MB','GB','TB'];let i=0,x=v;while(x>=1024&&i<units.length-1){x/=1024;i++}return `${x.toFixed(i===0?0:1)} ${units[i]}`}
function openStorageModal(){openModal('storageModal');loadStorage()}
function closeStorageModal(){closeModal('storageModal')}
async function loadStorage(){const host=$('storageBody');if(!host)return;host.innerHTML='<div class="field-hint">正在读取本机结果文件占用…</div>';try{const d=await api('/storage',{timeout:20000});const largest=d.largest?`${esc(d.largest.name)}（${bytesText(d.largest.size)}）`:'—';host.innerHTML=`${d.warn?`<div class="notice">结果文件已占用 ${bytesText(d.total_bytes)}，建议清理不再需要的记录或孤立文件。</div>`:''}<div class="summary report-metrics"><div class="report-metric"><div class="metric-label">结果文件合计</div><div class="metric-value">${bytesText(d.total_bytes)}<small> ${num(d.artifact_count,0)} 个</small></div><div class="metric-note">仅统计 run_*.jsonl 与 quality_run_*.json</div></div><div class="report-metric"><div class="metric-label">已被记录引用</div><div class="metric-value">${bytesText(d.referenced_bytes)}</div><div class="metric-note">删除对应任务记录才会一并删除</div></div><div class="report-metric"><div class="metric-label">孤立文件</div><div class="metric-value">${bytesText(d.orphan_bytes)}<small> ${num(d.orphan_count,0)} 个</small></div><div class="metric-note">没有任何记录引用，可安全清理</div></div><div class="report-metric"><div class="metric-label">最大单个文件</div><div class="metric-value" style="font-size:14px">${largest}</div><div class="metric-note">用于判断单次任务的结果规模</div></div></div><p class="report-section-sub">清理只删除“没有任何记录引用”的孤立文件；被引用的结果文件必须通过删除对应任务记录来删除，这样“删文件”和“删记录”始终是同一个可审计动作。</p><div class="report-footer-actions"><button class="btn" type="button" onclick="loadStorage()">刷新</button><button class="btn danger" type="button" onclick="cleanupStorage()" ${d.orphan_count?'':'disabled'}>清理孤立文件</button></div>${d.orphans?.length?`<div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>孤立文件</th><th>大小</th><th>最后修改</th></tr></thead><tbody>${d.orphans.map(x=>`<tr><td>${esc(x.name)}</td><td>${bytesText(x.size)}</td><td>${new Date(x.mtime*1000).toLocaleString('zh-CN',{dateStyle:'medium',timeStyle:'short'})}</td></tr>`).join('')}</tbody></table></div>`:''}`}catch(e){host.innerHTML=`<div class="auth-error">${esc(e.message)}</div>`}}
async function cleanupStorage(){if(!confirm('确认清理孤立结果文件？这些文件没有任何任务记录引用，删除后无法恢复。'))return;try{const d=await api('/storage/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({}) ,timeout:60000});toast(`已清理 ${num((d.removed||[]).length,0)} 个文件，释放 ${bytesText(d.freed_bytes)}`);loadStorage()}catch(e){toast(e.message)}}
/* ---------- 测试方案模板 ---------- */
const TEMPLATE_CATEGORIES={smoke:'冒烟',ramp:'阶梯',soak:'长跑'};
let templateCache=null,activeTemplateId=null;
const COMPARE_TEMPLATE_MODES=['smoke','ramp','soak'];
async function loadTemplates(){const host=$('templatePickerBody');if(!host)return;if(templateCache){renderTemplatePicker(templateCache);return}host.innerHTML='<div class="field-hint">正在读取模板…</div>';try{const d=await api('/templates');templateCache=d.templates||[];renderTemplatePicker(templateCache)}catch(e){templateCache=null;host.innerHTML=`<div class="field-hint">暂时无法读取模板：${esc(e.message)}</div>`}}
function renderTemplatePicker(items){const host=$('templatePickerBody');if(!host)return;const usable=(items||[]).filter(x=>COMPARE_TEMPLATE_MODES.includes(x.config?.mode));if(!usable.length){host.innerHTML='<div class="field-hint">暂无可用模板</div>';return}host.innerHTML=usable.map(t=>{const c=t.config||{};const shape=c.mode==='ramp'?`阶梯 ${esc(c.ramps||'')} RPM · 每档 ${esc(c.duration||'')} 秒`:`${num(c.rpm)} RPM · ${c.mode==='soak'?`${esc(c.duration||'')} 秒（约 ${duration(c.duration)}）`:`${num(c.iterations)} 条`}`;return `<button class="template-chip${activeTemplateId===t.id?' active':''}" type="button" data-template="${esc(t.id)}"><b>${esc(t.name)}${t.builtin?'':' · 自建'}</b><small>${esc(t.summary||'')}</small><em>并发 ${num(c.concurrency)} · ${shape}</em></button>`}).join('');host.querySelectorAll('[data-template]').forEach(node=>node.onclick=()=>applyTemplate(node.dataset.template))}
function pickTemplate(id){const items=(templateCache||[]).filter(x=>COMPARE_TEMPLATE_MODES.includes(x.config?.mode));return items.find(x=>x.id===id)||null}
async function applyTemplate(id){if(!templateCache)await loadTemplates();const t=pickTemplate(id);if(!t)return;activeTemplateId=t.id;const c=t.config||{};if(isCompareCreate()){const first=document.querySelector('#groupFields .group-row');if(first){const inputs=first.querySelectorAll('input');if(inputs[0])inputs[0].value=c.concurrency??inputs[0].value}}else{paintMode(c.mode);applyDynamic(c);for(const k of ['concurrency','connect_timeout','read_timeout','profile'])if($(k)&&c[k]!=null)$(k).value=c[k];if($('name')&&!$('name').value.trim())$('name').value=t.name}if($('formStatus')){$('formStatus').className='form-status';$('formStatus').textContent=`已套用模板「${t.name}」的压力参数；URL、密钥和模型保持你填写的值不变。`}renderTemplatePicker(templateCache);schedulePreflight()}
function toggleTemplatePicker(){const body=$('templatePickerBody'),btn=$('templatePickerToggle');if(!body||!btn)return;const next=body.hidden;body.hidden=!next;btn.setAttribute('aria-expanded',String(!next));btn.textContent=next?'收起':'展开'}
$('templatePickerToggle')?.addEventListener('click',toggleTemplatePicker)
/* ---------- 运行诊断 ---------- */
function openDiagnostics(){openModal('diagnosticsModal');loadDiagnostics()}
function closeDiagnostics(){closeModal('diagnosticsModal')}
function diagLimit(){const v=Number($('diagLimit')?.value||50);return Number.isFinite(v)?Math.max(10,Math.min(500,Math.round(v))):50}
function diagTime(ts){const v=Number(ts);return Number.isFinite(v)&&v>0?new Date(v*1000).toLocaleTimeString('zh-CN',{hour12:false}):'—'}
function diagList(items,render,emptyText){if(!items||!items.length)return `<div class="diag-empty">${esc(emptyText)}</div>`;return `<ul class="diag-list">${items.map(render).join('')}</ul>`}
function renderSchedulerBlock(s){if(!s)return '<div class="diag-empty">无法读取调度状态。</div>';const running=(s.running_tasks||[]).map(t=>t.name||t.id);const waiting=(s.waiting_tasks||[]).map(t=>t.name||t.id);return `<div class="diag-block"><h3>调度器</h3><p class="diag-sub">并发上限决定同时能跑几个任务；排队中的任务按创建顺序进入。</p><div class="diag-kv"><span>并发上限 ${num(s.limit,0)}</span><span>运行中 ${num(s.active,0)}</span><span>排队 ${num(waiting.length,0)}</span></div><div class="diag-list" style="margin-top:9px">${running.length?running.map(n=>`<li><b>运行中</b><span>${esc(n)}</span></li>`).join(''):'<div class="diag-empty">当前没有运行中的任务。</div>'}${waiting.length?waiting.map(n=>`<li><b>排队中</b><span>${esc(n)}</span></li>`).join(''):''}</div></div>`}
function renderEventBlock(events){return `<div class="diag-block"><h3>最近事件</h3><p class="diag-sub">只记录「发生了什么」，请求正文与密钥不会写入事件日志。最新在上。</p>${diagList(events,e=>`<li><b>${esc(e.event||'—')}</b><span>${esc(e.rid||'-')}${e.status?` · HTTP ${esc(e.status)}`:''}${e.elapsed_ms!=null?` · ${num(e.elapsed_ms,0)} ms`:''}${e.path?` · ${esc(e.path)}`:''}${e.task?` · ${esc(e.task)}`:''}</span><time>${diagTime(e.ts)}</time></li>`,'暂无事件记录')}</div>`}
function renderAuditBlock(records){return `<div class="diag-block"><h3>操作审计</h3><p class="diag-sub">谁在什么时候做了什么。失败的操作也会保留，便于回溯。</p>${diagList((records||[]).slice().reverse(),r=>`<li><b>${esc(r.action||'—')}</b><span>${esc(r.actor||'anonymous')}${r.target?` · ${esc(r.target)}`:''}${r.ok===false?' · 失败':''}${r.detail?` · ${esc(r.detail)}`:''}</span><time>${diagTime(r.ts)}</time></li>`,'暂无审计记录')}</div>`}
function renderEventStatsBlock(stats){if(!stats)return '';const by=stats.by_event||{};const keys=Object.keys(by);return `<div class="diag-block"><h3>事件统计</h3><p class="diag-sub">按事件名累计，用于快速判断服务是否在正常工作。</p><div class="diag-kv">${keys.length?keys.map(k=>`<span>${esc(k)} ${num(by[k],0)}</span>`).join(''):'<span>暂无事件</span>'}</div>${stats.degraded?'<div class="diag-empty" style="margin-top:9px">事件日志写入曾失败，已降级为不影响业务；请检查磁盘权限与剩余空间。</div>':''}</div>`}
async function loadDiagnostics(){const host=$('diagnosticsBody');if(!host)return;host.innerHTML='<div class="field-hint">正在读取调度状态、事件与审计记录…</div>';const limit=diagLimit();const results=await Promise.all([api('/scheduler').catch(e=>({__error:e.message})),api(`/events?limit=${limit}`).catch(e=>({__error:e.message})),api(`/audit?limit=${limit}`).catch(e=>({__error:e.message}))]);const [scheduler,events,audit]=results;const errors=[scheduler,events,audit].filter(x=>x&&x.__error).map(x=>x.__error);host.innerHTML=`${errors.length?`<div class="diag-block"><h3>部分读取失败</h3><p class="diag-sub">${esc([...new Set(errors)].join('；'))}</p></div>`:''}${renderSchedulerBlock(scheduler.__error?null:scheduler)}${renderEventBlock(events.events||[])}${renderEventStatsBlock(events.stats)}${renderAuditBlock(audit.records||[])}`}
function artifactNotice(record){const a=record?.artifact||{},state=a.state||record?.artifact_state;if(state==='expired'||state==='missing'){return `<div class="notice risk">${esc(a.expired_reason||record?.artifact_expired_reason||'结果文件已失效，可能已被删除或改写，报告里的证据无法再逐条核对。')}</div>`}if(state==='deleted'){return '<div class="notice">结果文件已删除；页面仍保留这次运行的历史摘要，但不能再下载原始结果。</div>'}return ''}
function openModal(id){const modal=$(id);if(!modal)return;if(!modalStack.some(x=>x.id===id))modalStack.push({id,opener:document.activeElement});modal.hidden=false;modal.style.zIndex=String(120+modalStack.findIndex(x=>x.id===id)*2);document.body.classList.add('modal-open');const body=modal.querySelector('.modal-body');if(body)body.scrollTop=0;modal.querySelector('.modal-close,.drawer-close')?.focus({preventScroll:true})}
function closeModal(id){const modal=$(id);if(!modal||modal.hidden)return;const index=modalStack.findIndex(x=>x.id===id),entry=index>=0?modalStack.splice(index,1)[0]:null;modal.hidden=true;document.body.classList.toggle('modal-open',modalStack.length>0);if(entry?.opener?.isConnected)entry.opener.focus({preventScroll:true})}
document.addEventListener('keydown',e=>{const top=modalStack[modalStack.length-1];if(!top)return;const modal=$(top.id);if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closeModal(top.id);return}if(e.key!=='Tab')return;const focusables=[...modal.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),summary,[tabindex="0"]')].filter(el=>el.getClientRects().length);if(!focusables.length)return;const first=focusables[0],last=focusables[focusables.length-1];if(!modal.contains(document.activeElement)){e.preventDefault();first.focus()}else if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}},true);
function sourceLink(source){let url='';try{const parsed=new URL(source.url);if(parsed.protocol==='https:')url=parsed.href}catch{}return url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(source.name)} ↗</a>`:esc(source.name)}
async function showQualityMethodology(context='suite'){
 const frozen=context==='report'?window.currentQualityDetail?.config?.suite_manifest?.methodology:currentQualitySuite?.manifest?.methodology;
 openModal('qualityMethodologyModal');$('qualityMethodologyBody').textContent='正在读取方法与来源…';
 try{const m=frozen||await api('/quality-methodology');if($('qualityMethodologyModal').hidden)return;const sources=m.sources||[];
 $('qualityMethodologyBody').innerHTML=`<div class="quality-suite-note"><strong>${esc(m.provenance)}</strong><p>方法版本 ${esc(m.version)} · 官方资料核对 ${esc(m.reviewed_at)}。${context==='report'&&!frozen?'这条历史任务未保存方法快照；以下仅是当前方法参考，不改变历史结论。':frozen?'展示与题集一起保存的方法快照。':'展示当前方法。'}</p></div><h3>怎样从测试走到结论</h3><ol class="methodology-steps">${(m.workflow||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ol><h3>分数怎么计算、怎样用于选择</h3><p>${esc(m.score_policy)}</p><p>${esc(m.decision_policy)}</p><h3>12个维度分别测什么</h3><div class="methodology-grid">${(m.dimensions||[]).map(d=>`<section class="methodology-card"><h3>${esc(d.label)}</h3><p>${esc(d.scope)}</p><p>方法参考：${(d.references||[]).map(id=>sources.find(x=>x.id===id)).filter(Boolean).map(sourceLink).join(' · ')}</p></section>`).join('')}</div><h3>官方评测框架与题集参考</h3><p>“借鉴方法”不等于“集成官方基准”。以下逐项列出已采用的部分和没有覆盖的部分。</p><div class="methodology-grid">${sources.map(x=>`<section class="methodology-card"><h3>${sourceLink(x)}</h3><span class="methodology-status">方法参考 · 非官方成绩</span><p><b>原项目重点：</b>${esc(x.focus)}</p><p><b>本项目采用：</b>${esc(x.adopted)}</p><p><b>未覆盖：</b>${esc(x.not_covered)}</p></section>`).join('')}</div><h3>引入官方数据前的要求</h3><p>${esc(m.import_policy)}</p>`
 }catch(e){$('qualityMethodologyBody').textContent='方法读取失败：'+e.message}
}
function showQualitySuite(){const d=currentQualitySuite;if(!d)return;const modal=$('qualitySuiteModal');window.qualitySuiteOpener=document.activeElement;openModal('qualitySuiteModal');$('qualitySuiteTitle').textContent=d.manifest.title;const groups=[...new Set(d.cases.map(c=>c.category))];$('qualitySuiteBody').innerHTML=`<p>${esc(d.manifest.description)}</p><p>${esc(d.manifest.scoring)}</p><p>原创建设题，不等同于公开标准榜单；查看答案便于审计，不向被测资源发送参考答案。</p><button class="btn" onclick="showQualityMethodology()">评测方法与官方参考</button><div class="quality-suite-cases">${groups.map(cat=>`<details><summary>${esc(categoryLabel(cat))} · ${d.cases.filter(c=>c.category===cat).length} 题</summary>${d.cases.filter(c=>c.category===cat).map(c=>`<details><summary>${esc(c.title)} · ${esc(c.difficulty)}${c.critical?' · 关键规则':''}</summary><p>输入长度 ${c.prompt.length} 字符。评分：${c.evaluator?.type==='verifiable_constraints'?'逐项可验证约束（任一未满足则整题不通过）':c.evaluator?.type==='json_exact'?'完整结构与值验证':'答案与指定格式验证'}。</p><pre class="evidence-pre">${esc(c.prompt)}</pre>${c.evaluator?.rules?`<ul>${c.evaluator.rules.map(rule=>`<li>${esc(rule.label)}</li>`).join('')}</ul>`:''}<b>参考答案${c.evaluator?.rules?'（一种合规示例）':''}</b><pre class="evidence-pre">${esc(c.reference_answer)}</pre><p>${esc(c.rationale)}</p></details>`).join('')}</details>`).join('')}</div>`;modal.querySelector('.modal-close').focus()}
function closeQualitySuite(){closeModal('qualitySuiteModal')}

function qualityResourceTemplate(index){const title=index===0?'资源 A（必填）':'资源 B（对照必填）';return `<section class="quality-resource" data-quality-index="${index}"><div class="card-title">${title}</div><div class="field"><label for="quality-name-${index}">资源名称</label><input id="quality-name-${index}" class="input quality-name" value="${index===0?'资源 A':'资源 B'}"></div><div class="field"><label for="quality-url-${index}">资源 URL（统一资源定位符）</label><input id="quality-url-${index}" class="input quality-url" placeholder="https://provider.example.com/v1"></div><div class="field"><label for="quality-key-${index}">API Key（应用程序编程接口密钥）</label><input id="quality-key-${index}" class="input quality-key" type="password" autocomplete="off" spellcheck="false" placeholder="已保存密钥可留空复用"><div class="field-hint quality-credential-hint">填写 URL 后检查是否已有已保存密钥；留空会自动复用，输入新密钥会覆盖。</div></div><div class="field"><label for="quality-model-${index}">模型名称</label><div class="quality-model-row"><select id="quality-model-${index}" class="input quality-model" disabled><option value="">先填写 URL 和 API Key，再获取模型</option></select><button class="icon-btn quality-model-refresh" type="button" onclick="refreshQualityModels(${index})">刷新模型</button></div><div class="field-hint quality-model-hint" aria-live="polite">模型列表尚未获取。</div></div></section>`}
let qualityModelSeq=0;async function refreshQualityModels(index){const row=document.querySelector(`.quality-resource[data-quality-index="${index}"]`),url=row?.querySelector('.quality-url')?.value.trim(),key=row?.querySelector('.quality-key')?.value.trim(),select=row?.querySelector('.quality-model'),button=row?.querySelector('.quality-model-refresh'),hint=row?.querySelector('.quality-model-hint'),wrap=row?.querySelector('.quality-model-row');if(!row||!select||!button||!hint)return;if(!url){hint.textContent='请先填写资源 URL。';hint.className='field-hint quality-model-hint error';return}const seq=++qualityModelSeq;row.dataset.modelSeq=String(seq);button.disabled=true;wrap.classList.remove('is-done','is-error');wrap.classList.add('is-loading');button.textContent='获取中…';hint.textContent='正在请求模型列表，请稍候…';hint.className='field-hint quality-model-hint';try{const d=await api('/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,api_key:key}),timeout:15000});if(String(seq)!==row.dataset.modelSeq)return;select.innerHTML=d.models?.length?d.models.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join(''):'<option value="">接口没有返回可用模型</option>';select.disabled=!d.models?.length;wrap.classList.remove('is-loading');wrap.classList.add(d.models?.length?'is-done':'is-error');button.textContent=d.models?.length?'已获取':'重试';hint.textContent=d.models?.length?`已获取 ${d.models.length} 个模型，已自动保存凭据。`:(d.error||'模型列表为空，请检查 URL、API Key 或网关兼容性。');hint.className=`field-hint quality-model-hint ${d.models?.length?'ok':'error'}`}catch(e){if(String(seq)!==row.dataset.modelSeq)return;wrap.classList.remove('is-loading');wrap.classList.add('is-error');button.textContent='重试';select.disabled=true;hint.textContent=e.message||'获取模型失败';hint.className='field-hint quality-model-hint error'}finally{if(String(seq)===row.dataset.modelSeq)button.disabled=false}}
let qualityCredentialSeq=0;async function checkQualityCredential(row){const url=String(row?.querySelector('.quality-url')?.value||'').trim(),hint=row?.querySelector('.quality-credential-hint');if(!hint)return;if(!url){hint.textContent='填写 URL 后检查已保存密钥；留空会自动复用，输入新密钥会覆盖。';hint.className='field-hint quality-credential-hint';return}const seq=++qualityCredentialSeq;row.dataset.credentialSeq=String(seq);hint.textContent='正在检查该 URL 的已保存密钥…';hint.className='field-hint quality-credential-hint';try{const d=await api('/credential-status?url='+encodeURIComponent(url));if(String(seq)!==row.dataset.credentialSeq)return;hint.textContent=d.saved?`已保存密钥 ${d.hint||''}；API Key 留空即可复用。输入新密钥会覆盖。`:'该 URL 尚未保存密钥；请输入 API Key。';hint.className=`field-hint quality-credential-hint ${d.saved?'ok':'error'}`}catch(e){if(String(seq)===row.dataset.credentialSeq){hint.textContent='暂时无法检查已保存密钥；提交时仍会再次校验。';hint.className='field-hint quality-credential-hint error'}}}
function addQualityResource(){const wrap=$('qualityResourceFields'),button=$('addQualityResource');if(!wrap||wrap.querySelectorAll('#qualityResourceFields .quality-resource').length>=2)return toast('最多支持两个资源实例');wrap.insertAdjacentHTML('beforeend',qualityResourceTemplate(1));if(button)button.disabled=true}
let qualityReferenceRecords=[];async function loadQualityReferenceRecords(){const select=$('qualityReuse');if(!select)return;try{const d=await api('/quality-reference-records');qualityReferenceRecords=d.records||[];select.innerHTML='<option value="">实时填写参考资源</option>'+qualityReferenceRecords.map(x=>`<option value="${esc(x.id)}">复用：${esc(x.name)} · ${esc(x.model||'未知模型')} · ${comparisonMetric(x.score_1000)}/1000 · ${x.case_count||0}题</option>`).join('');select.onchange=()=>{const reference=document.querySelector('[data-quality-index="1"]');if(reference)reference.hidden=Boolean(select.value);loadQualitySuiteSummary(!select.value);const x=qualityReferenceRecords.find(r=>r.id===select.value);$('qualityReusePreview').innerHTML=x?`<div class="reference-reuse-card"><b>已选择历史参考结果</b><span>${esc(x.name)} · ${esc(x.model||'未知模型')} · ${comparisonMetric(x.score_1000)}/1000</span><button type="button" class="text-btn" onclick="showQualityHistoryReference('${esc(x.id)}')">查看上次评测详情</button></div>`:''}}catch(e){select.innerHTML='<option value="">暂时无法读取历史参考结果</option>'}}function showQualityHistoryReference(id){const q=qualityTests.find(x=>x.id===id);if(!q)return;const report=q.report||{},r=(report.resources||[])[0]||{},m=q.config?.suite_manifest||{};$('qualityEvidenceTitle').textContent='历史参考评测详情';$('qualityEvidenceBody').innerHTML=`<div class="reference-detail"><h3>${esc(q.name||'历史质量评测')}</h3><p>模型：${esc(q.resources?.[0]?.model||'—')} · 题数：${q.case_count||0} · 题集指纹：${esc(m.fingerprint||'未记录')}</p><div class="report-metrics"><div class="report-metric"><div class="metric-label">质量分</div><div class="metric-value">${comparisonMetric(r.score_1000)}/1000</div></div><div class="report-metric"><div class="metric-label">通过题数</div><div class="metric-value">${r.passed_cases||0}/${r.total_cases||0}</div></div><div class="report-metric"><div class="metric-label">请求错误</div><div class="metric-value">${r.request_errors||0}</div></div></div><h4>维度分</h4><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>维度</th><th>得分</th><th>通过/计划</th></tr></thead><tbody>${(r.categories||[]).map(c=>`<tr><td>${esc(c.label)}</td><td>${comparisonMetric(c.score_1000)}/1000</td><td>${c.passed||0}/${c.planned||0}</td></tr>`).join('')}</tbody></table></div><h4>失败题</h4><ul>${(r.failure_evidence||[]).slice(0,12).map(f=>`<li>${esc(f.case_id)} · ${esc(f.title)}：${esc(f.reason)}</li>`).join('')||'<li>无失败题</li>'}</ul></div>`;openModal('qualityEvidenceModal')}
function readQuality(){const reuse=$('qualityReuse')?.value||'';const rows=[...document.querySelectorAll('#qualityResourceFields .quality-resource')];const selectedRows=reuse?rows.slice(0,1):rows;return {name:$('qualityName')?.value||'质量评测',reference_reuse_id:reuse,capture_raw:Boolean($('qualityCapture')?.checked),resources:selectedRows.map(row=>({name:row.querySelector('.quality-name').value.trim(),url:row.querySelector('.quality-url').value.trim(),api_key:row.querySelector('.quality-key').value.trim(),model:row.querySelector('.quality-model').value.trim(),is_reference:false}))}}
function readComparison(){normalizeComparisonRoles();const shared=readConfig();const name=$('comparisonName')?.value||'对照实验';shared.name=name;shared.mode='smoke';shared.rpm=600;shared.iterations=1;shared.concurrency=1;shared.capture_raw=Boolean($('comparisonCapture')?.checked);return {name,shared_config:shared,execution_policy:'serial',groups:[...document.querySelectorAll('.group-row')].map((row,i)=>({id:`group-${i+1}`,name:row.querySelector('.group-name').value.trim()||`实验组 ${i+1}`,role:row.querySelector('.group-role')?.value|| (i===0?'baseline':'comparison'),concurrency:row.querySelector('.group-concurrency').value,rpm:row.querySelector('.group-rpm').value,iterations:row.querySelector('.group-iterations').value}))}}
function qualityStatusLabel(status){return {draft:'草稿',stopped:'已停止',stopping:'正在停止',running:'评测中',finished:'已完成',finished_with_errors:'完成但有错误',error:'失败',expired:'结果已过期',waiting:'排队中'}[status]||status}
let preflightData=null,preflightSeq=0,preflightTimer=null,preflightAckRequested=false;
function preflightLevelLabel(level){return {risk:'高风险',warn:'需注意',info:'提示',ok:'未发现风险'}[level]||'预检'}function preflightSeconds(s){const v=Number(s);if(!Number.isFinite(v)||v<=0)return '—';if(v<60)return `${num(v,0)} 秒`;if(v<3600)return `${num(v/60,1)} 分钟`;return `${num(v/3600,1)} 小时`}
function preflightHtml(d){if(d.unavailable)return `<div class="preflight-head"><span class="preflight-badge">无法预检</span><span class="preflight-title">按当前配置无法完成预检</span></div><p class="preflight-note">${esc(d.unavailable)} 仍可提交，但提交前请自行确认目标环境、并发与耗时。</p>`;const findings=d.findings||[],resources=Number(d.resource_count)||1;const planned=`${num(d.planned_requests,0)} 次请求`;const cases=d.case_count?` · ${num(d.case_count,0)} 题 × ${resources} 资源`:'';return `<div class="preflight-head"><span class="preflight-badge">预计 ${esc(preflightLevelLabel(d.level))}</span><span class="preflight-title">这次运行大约会发生什么</span></div><div class="preflight-summary"><span>计划请求 <b>${planned}</b>${cases}</span><span>预计耗时 <b>${esc(preflightSeconds(d.min_seconds))}</b></span><span>结果文件 <b>${esc(bytesText(d.estimated_bytes))}</b></span><span>目标 <b>${esc((d.target||{}).host||'未知')}</b></span></div>${findings.length?`<ul class="preflight-findings">${findings.map(f=>`<li class="lv-${esc(f.level||'info')}"><b>${esc(f.title||'风险项')}</b>${esc(f.detail||'')}</li>`).join('')}</ul>`:''}<p class="preflight-note">${esc(d.note||'估算基于配置与经验单行长度，不是对上游真实性能的预测。')}</p>`}
function renderPreflight(d){const host=$('preflightCard');if(!host)return;preflightData=d;if(!d.requires_confirmation)preflightAckRequested=false;host.className=`preflight-card level-${d.unavailable?'info':d.level||'ok'}`;host.innerHTML=preflightHtml(d);host.hidden=false;if(d.requires_confirmation){const ack=document.createElement('label');ack.className='preflight-ack';ack.innerHTML=`<input type="checkbox" id="riskAck"><span>我已确认目标是生产/公网环境：这次运行会产生真实负载、可能计入上游费用并影响线上用户。</span>`;host.appendChild(ack);const box=$('riskAck');if(box){box.checked=Boolean(preflightAckRequested);box.onchange=()=>{preflightAckRequested=box.checked}}}}
async function refreshPreflight(){const host=$('preflightCard');if(!host)return;let draft;try{draft=isRoutingCreate()?readRouting():isQualityCreate()?readQuality():isCompareCreate()?readComparison():readConfig()}catch(e){host.hidden=true;return}const seq=++preflightSeq;try{const d=await api('/preflight',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:isRoutingCreate()?'routing':isQualityCreate()?'quality':'run',config:draft}),timeout:20000});if(seq!==preflightSeq||!$('config').classList.contains('mobile-open'))return;renderPreflight(d)}catch(e){if(seq===preflightSeq&&$('config').classList.contains('mobile-open')){host.className='preflight-card';host.innerHTML=`<p class="preflight-note">暂时无法预检：${esc(e.message)}</p>`;host.hidden=false}}}
function schedulePreflight(){if(!$('config')?.classList.contains('mobile-open'))return;clearTimeout(preflightTimer);preflightTimer=setTimeout(refreshPreflight,450)}
async function ensurePreflightAck(){const button=$('submit');if(!preflightData||!preflightData.requires_confirmation)return true;if($('riskAck')?.checked)return true;const ok=confirm(`${preflightData.target?.host||'该目标'} 疑似生产环境：${preflightData.target?.reason||'无法确认是否为测试环境'}\n\n这次运行会产生真实负载并可能计费。确认要在该目标上执行吗？`);if(!ok)return false;const box=$('riskAck');if(box)box.checked=true;preflightAckRequested=true;return true}
function comparisonMetric(v,suffix=''){return v==null?'—':num(v)+suffix}
function pctDelta(v,suffix=''){if(v==null)return '—';const n=Number(v);return `<span class="${n<0?'delta-risk':n>0?'delta-good':'delta-neutral'}">${n>0?'+':''}${num(n)}${suffix}</span>`}
function pctl(values,p){const a=values.filter(v=>Number.isFinite(Number(v))).map(Number).sort((a,b)=>a-b);return a.length?a[Math.max(0,Math.min(a.length-1,Math.ceil(a.length*p)-1))]:null}
function setRecordFilter(view){activeView=view==='compare'?'compare':'single';selected=null;detailTask=null;selectedComparison=null;selectedQuality=null;localStorage.removeItem('route_selected_task');updateWorkspaceChrome();renderWorkspace()}
function updateWorkspaceChrome(){
 document.querySelectorAll('.module-nav-item').forEach(x=>{const active=x.dataset.module===activeModule;x.classList.toggle('active',active);x.setAttribute('aria-current',active?'page':'false')});
 if(activeModule==='fingerprint'){window.FingerprintUI?.updateChrome();return}window.FingerprintUI?.hide();
 document.querySelectorAll('.subnav-item').forEach(x=>x.classList.toggle('active',x.closest('.workspace-subnav')?.id===`${activeModule}Subnav`&&x.dataset.view===activeView));
 $('gatewaySubnav').hidden=activeModule!=='gateway';$('qualitySubnav').hidden=activeModule!=='quality';$('routingSubnav').hidden=activeModule!=='routing';
 $('gatewaySingleCount').textContent=tasks.length;$('gatewayCompareCount').textContent=comparisons.length;
 $('qualitySingleCount').textContent=qualityTests.filter(x=>(x.resources||[]).length<=1).length;$('qualityCompareCount').textContent=qualityTests.filter(x=>(x.resources||[]).length>1).length;
 $('routingCount').textContent=routingComparisons.length;
 const quality=activeModule==='quality',routing=activeModule==='routing';
 $('crumb').textContent=routing?'固定路由与智能路由对照':quality?'质量评测':'网关压测';
 $('recordTitle').textContent=routing?'路由对照记录':quality?'评测记录':'压测记录';
 $('recordSubtitle').textContent=routing?'同一份请求清单交错打在两条路由上的黑盒对照':quality?'固定题集下的能力、格式遵循与一致性黑盒观测':'吞吐、成功率、延迟与压力窗口黑盒观测';
 $('recordCreateButton').innerHTML=uiIcon('plus')+'<span>'+(routing?'新建路由对照':quality?'新建评测':'新建压测')+'</span>';
 const emptyAction=$('empty')?.querySelector('.empty-action');
 if(emptyAction)emptyAction.innerHTML=uiIcon('plus')+'<span>'+(routing?'新建路由对照':quality?(activeView==='compare'?'新建资源对照':'新建单资源测试'):(activeView==='compare'?'新建压测对照':'新建压测'))+'</span>';document.querySelector('.empty-visual').innerHTML=uiIcon(routing?'routing':quality?'quality':'gateway');
 document.querySelector('.task-section-title').textContent=routing?'路由对照记录':quality?'评测记录':'压测记录';
}
function setModule(next){activeModule=['quality','routing','fingerprint'].includes(next)?next:'gateway';activeView=activeModule==='quality'?'compare':activeModule==='routing'?'compare':'single';selected=null;detailTask=null;selectedComparison=null;selectedQuality=null;selectedRouting=null;localStorage.removeItem('route_selected_task');updateWorkspaceChrome();renderWorkspace()}
function setWorkspaceView(view){setRecordFilter(view)}
function openGatewayCreate(view=activeView){activeModule='gateway';activeView=view==='compare'?'compare':'single';updateWorkspaceChrome();startNewTest(activeModule,activeView)}
function openQualityCreate(view=activeView){activeModule='quality';activeView=view==='single'?'single':'compare';updateWorkspaceChrome();startNewTest(activeModule,activeView)}
function openCreateForContext(){if(activeModule==='fingerprint')return window.FingerprintUI?.openCreate();return activeModule==='routing'?openRoutingCreate():activeModule==='quality'?openQualityCreate(activeView==='compare'?'compare':'single'):openGatewayCreate(activeView==='compare'?'compare':'single')}
function routingTierOptions(){return routingCorpus?.length?routingCorpus.map(x=>`<option value="${esc(x.tier)}">${num(x.tier,0)} 条 / 侧 · 合计 ${num(x.tier*2,0)} 条</option>`).join(''):'<option value="">测试集尚未加载</option>'}
function renderRoutingFields(reset=false){
 const tier=$('routingTier'),previous=tier.value;
 tier.innerHTML=routingTierOptions();tier.disabled=!routingCorpus?.length;
 if([...tier.options].some(x=>x.value===previous))tier.value=previous;
 const picked=routingCorpus?.find(x=>String(x.tier)===String(tier.value));
 $('routingTierHint').textContent=picked?`${picked.note||'同一清单用于两侧配对测试。'} 两侧合计 ${num(picked.tier*2,0)} 次请求。`:'测试集未加载，暂时无法开始。请重新加载后重试。';
 $('routingCorpusRetry').hidden=Boolean(picked);
 const panels=$('routingSidePanels');
 if(reset||!panels.children.length){panels.innerHTML=routingSideTemplate('baseline')+routingSideTemplate('candidate');setRoutingSide('baseline')}
 document.querySelectorAll('#routingSideTabs .tab').forEach(x=>x.onclick=()=>setRoutingSide(x.dataset.routingTab));
 updateRoutingSideStatus();
}
function routingSideTemplate(side){
 const baseline=side==='baseline',label=baseline?'固定路由':'智能路由',prefix=`routing-${side}`;
 return `<section class="quality-resource routing-resource" id="${prefix}-panel" data-routing-side="${side}" role="tabpanel" aria-labelledby="${baseline?'routingBaselineTab':'routingCandidateTab'}" ${baseline?'':'hidden'}><div class="field"><label for="${prefix}-name">路由名称</label><input id="${prefix}-name" class="input routing-side-name" value="${label}" autocomplete="off"></div><div class="field"><label for="${prefix}-url">网关地址 <span class="field-required">必填</span></label><input id="${prefix}-url" class="input routing-side-url" type="url" required spellcheck="false" autocomplete="off" placeholder="https://gateway.example.com/v1"><div class="field-hint">${baseline?'填写作为基准的固定路由入口。':'填写待对照的智能路由入口。'}</div></div><div class="field"><label for="${prefix}-key">API Key（接口密钥） <span class="field-required">必填</span></label><input id="${prefix}-key" class="input routing-side-key" type="password" required autocomplete="off" spellcheck="false" placeholder="填写该路由的密钥"><div class="field-hint">密钥仅用于本次请求，不写入测试记录。</div></div></section>`;
}
function setRoutingSide(side){
 if(!['baseline','candidate'].includes(side))return;
 document.querySelectorAll('#routingSideTabs .tab').forEach(x=>{const active=x.dataset.routingTab===side;x.classList.toggle('active',active);x.setAttribute('aria-selected',String(active));x.tabIndex=active?0:-1});
 document.querySelectorAll('#routingSidePanels [data-routing-side]').forEach(x=>x.hidden=x.dataset.routingSide!==side);
}
function updateRoutingSideStatus(){document.querySelectorAll('#routingSideTabs .tab').forEach(tab=>{const side=readRoutingSide(tab.dataset.routingTab),ready=Boolean(side.url&&side.api_key);tab.querySelector('.routing-side-status').textContent=ready?'已填写':'待填写';tab.classList.toggle('is-complete',ready)})}
function readRoutingSide(mode){const box=document.querySelector(`[data-routing-side="${mode}"]`);return{name:(box?.querySelector('.routing-side-name')?.value||'').trim()||(mode==='baseline'?'固定路由':'智能路由'),url:(box?.querySelector('.routing-side-url')?.value||'').trim(),api_key:(box?.querySelector('.routing-side-key')?.value||'').trim()}}
function readRouting(){
 const v=id=>$(id)?.value,shared={model:String(v('routingModel')||'').trim(),concurrency:Number(v('routingConcurrency')),rpm:Number(v('routingRpm')),stream:true,profile:'smoke',connect_timeout:Number(v('routingConnectTimeout')),read_timeout:Number(v('routingReadTimeout')),write_timeout:30,include_usage:true,capture_raw:Boolean($('routingCapture').checked)};
 return{name:String(v('routingName')||'').trim()||'路由对照',tier:Number(v('routingTier')),rpm:shared.rpm,baseline:{...shared,...readRoutingSide('baseline')},candidate:{...shared,...readRoutingSide('candidate')}};
}
function validateRoutingForm(){
 if(!routingCorpus?.length)throw new Error('测试集尚未加载，请点击“重新加载测试集”。');
 for(const input of $('routingFields').querySelectorAll('input[required],select[required]')){
  if(input.checkValidity()&&(!['text','password','url'].includes(input.type)||input.value.trim()))continue;
  const side=input.closest('[data-routing-side]')?.dataset.routingSide;if(side)setRoutingSide(side);
  const details=input.closest('details');if(details)details.open=true;
  input.focus();input.reportValidity();
  const label=$('routingFields').querySelector(`label[for="${input.id}"]`)?.textContent.replace('必填','').trim()||'配置';
  throw new Error(`${side==='baseline'?'固定路由：':side==='candidate'?'智能路由：':''}请检查${label}。`);
 }
}
function openRoutingCreate(){activeModule='routing';activeView='compare';updateWorkspaceChrome();startNewTest('routing','compare')}
function filteredGatewayRecords(){const records=[...tasks.map(x=>({...x,record_type:'single'})),...comparisons.map(x=>({...x,record_type:'compare'}))].sort((a,b)=>(b.created||0)-(a.created||0));return records.filter(x=>x.record_type===activeView)}
function filteredQualityRecords(){return qualityTests.filter(x=>activeView==='compare'?(x.resources||[]).length>1:(x.resources||[]).length<=1)}
function renderWorkspace(){
 if(activeModule==='fingerprint'){updateWorkspaceChrome();window.FingerprintUI?.render();return}
 updateWorkspaceChrome();$('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;
 $('gatewayList').hidden=activeModule!=='gateway';$('qualityList').hidden=activeModule!=='quality';$('routingList').hidden=activeModule!=='routing';
 if(activeModule==='routing'){
   updateRoutingList();
   const run=selectedRouting&&routingComparisons.find(x=>x.id===selectedRouting);
   if(run){$('modulePlaceholder').hidden=false;renderRoutingDetail(run);return}
   if(routingComparisons.length){renderWorkspaceEmpty('选择一条路由对照记录查看详情','一条记录就是「同一份清单交错打在两条路由上」；进入后看两侧的指标、差值和归因判定。','openRoutingCreate()')}
   else renderWorkspaceEmpty('当前暂无路由对照记录','创建一次路由对照后，智能路由相对固定路由的提升与下降都会出现在这里。','openRoutingCreate()');
   return;
 }
 if(activeModule==='gateway'){
   updateTaskList();
   const current=detailTask||(selected&&tasks.find(x=>x.id===selected));const comparison=selectedComparison&&comparisons.find(x=>x.id===selectedComparison);
   if(current){render(current);return} if(comparison){$('modulePlaceholder').hidden=false;renderComparisonDetail(comparison);return}
   const records=filteredGatewayRecords();
   if(records.length){renderWorkspaceEmpty('选择一条测试记录查看详情','记录类型只是筛选方式；对照实验本身是一条记录，进入后用标签页查看结论、基准组和对比组。','openCreateForContext()')}
   else showEmptyForWorkspace();
   return;
 }
 updateQualityList();const quality=selectedQuality&&qualityTests.find(x=>x.id===selectedQuality);
 if(quality){$('modulePlaceholder').hidden=false;renderQualityDetail(quality);return}
 if(filteredQualityRecords().length){renderWorkspaceEmpty('选择一条质量评测记录查看详情','单资源测试和资源对照现在统一作为质量测试记录；进入后查看质量报告和逐题证据。','openCreateForContext()')}
 else showEmptyForWorkspace();
}
function renderModule(){renderWorkspace()}
function selectComparison(id){comparisonTab='report';selectedComparison=id;selected=null;detailTask=null;selectedQuality=null;activeModule='gateway';updateWorkspaceChrome();updateTaskList();const c=comparisons.find(x=>x.id===id);if(c){$('modulePlaceholder').hidden=false;renderComparisonDetail(c)}}
function selectQuality(id){if(selectedQuality!==id)qualityTab='report';selectedQuality=id;selected=null;detailTask=null;selectedComparison=null;activeModule='quality';updateWorkspaceChrome();updateQualityList();const q=qualityTests.find(x=>x.id===id);if(q){$('modulePlaceholder').hidden=false;renderQualityDetail(q)}}
let comparisonTab='report', qualityTab='report', routingComparisons=[], routingCorpus=null, routingMetrics=[], routingTab='baseline';
function setComparisonTab(id,tab){comparisonTab=tab;const c=comparisons.find(x=>x.id===id);if(c)renderComparisonDetail(c)}
function setQualityTab(id,tab){qualityTab=tab;const q=qualityTests.find(x=>x.id===id);if(q)renderQualityDetail(q)}
function routingStatusLabel(status){return {draft:'草稿',stopped:'已停止',stopping:'正在停止',running:'运行中',finished:'已完成',error:'失败',waiting:'排队中'}[status]||status||'准备中'}
function selectRouting(id){if(selectedRouting!==id)routingTab='baseline';selectedRouting=id;selected=null;detailTask=null;selectedComparison=null;selectedQuality=null;activeModule='routing';activeView='compare';updateWorkspaceChrome();updateRoutingList();const run=routingComparisons.find(x=>x.id===id);if(run){$('modulePlaceholder').hidden=false;renderRoutingDetail(run)}}
function setRoutingTab(id,tab){routingTab=tab;const run=routingComparisons.find(x=>x.id===id);if(run)renderRoutingDetail(run)}
async function stopRouting(id){if(!confirm('停止后不再发出后续请求；已经开始的请求会完成并写入结果。部分结果不会生成完整结论。继续？'))return;try{await api('/routing-comparisons?stop='+encodeURIComponent(id),{method:'POST'});toast('已请求停止路由对照');refresh()}catch(e){toast(e.message)}}
async function deleteRouting(id){if(!confirm('确认删除这条路由对照记录？记录和两侧的结果文件会一起删除，删除后无法恢复。'))return;try{await api('/routing-comparisons?delete='+encodeURIComponent(id),{method:'POST'});selectedRouting=null;toast('路由对照记录已删除');refresh()}catch(e){toast(e.message)}}
function routingItem(run){const pct=run.status==='finished'?100:Math.round(Math.max(0,Math.min(1,Number(run.progress?.pct)||0))*100);const report=run.report||{},tier=Number(run.tier||0),successDelta=report.success?report.success.delta_pct:null;return `<button class="task comparison-record ${run.id===selectedRouting?'sel':''}" onclick="selectRouting('${esc(run.id)}')"><div class="task-top"><div class="task-name">${esc(run.name)}</div><span class="status ${esc(run.status)}">${routingStatusLabel(run.status)}</span></div><div class="task-meta"><span class="record-type comparison-type">路由对照</span> · ${num(tier,0)} 条 × 2 侧 · ${num(run.progress?.done,0)}/${num(Number(tier)*2,0)} 已完成</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="task-pct">${run.status==='finished'?(successDelta==null?'完成':`成功率差 ${comparisonMetric(successDelta)}%`):`${pct}%`}</span></div></button>`}
function updateRoutingList(){const list=$('routingList');if(!list)return;list.innerHTML=routingComparisons.length?routingComparisons.map(routingItem).join(''):listEmpty('暂无路由对照记录','创建一次路由对照后，同一份清单交错打在两条路由上的差别会出现在这里。','openRoutingCreate()')}
function routingRateCell(side,unit='命中/成功'){if(!side)return '<span class="metric-value">—</span>';const ci=side.ci_low_pct==null?'':`<small>95% 置信区间 ${num(side.ci_low_pct)}% ~ ${num(side.ci_high_pct)}%</small>`;return `<span class="metric-value">${comparisonMetric(side.pct)}%<small>${unit} ${num(side.successes,0)} / ${num(side.total,0)}</small></span>${ci}`}
function routingAttribution(block,label){if(!block)return '';const map={attributable:['good','可归因于路由'],not_attributable:['caution','不足以归因于路由'],insufficient_evidence:['','证据不足']};const [level,text]=map[block.attribution]||['','无法判定'];return `<div class="notice ${level}"><b>${esc(label)}：${esc(text)}</b>${block.note?`<span>${esc(block.note)}</span>`:''}${block.p_value!=null?`<span>Fisher 精确检验 p = ${esc(String(block.p_value))}</span>`:''}</div>`}
function routingMetricTable(run){const report=run.report||{},success=report.success||{},cache=report.cache||{},ttft=report.ttft||{},total=report.total_latency||{},base=report.baseline||{},cand=report.candidate||{};const sideNames={baseline:esc(run.sides?.baseline?.name||'固定路由'),candidate:esc(run.sides?.candidate?.name||'智能路由')};const rows=[['成功率',`${comparisonMetric(base.success?.pct)}%`,`${comparisonMetric(cand.success?.pct)}%`,success.delta_pct],['缓存命中率',`${comparisonMetric(base.cache?.pct)}%`,`${comparisonMetric(cand.cache?.pct)}%`,cache.delta_pct],['TTFT p95（首字耗时）',`${comparisonMetric(base.ttft_p95_ms)} ms`,`${comparisonMetric(cand.ttft_p95_ms)} ms`,ttft.delta_pct],['总耗时 p95',`${comparisonMetric(base.total_p95_ms)} ms`,`${comparisonMetric(cand.total_p95_ms)} ms`,total.delta_pct]];return `<section class="report-section"><h2 class="report-section-title">两侧指标对照</h2><p class="report-section-sub">差值 = 智能路由 − 固定路由；正数表示智能路由该指标更高。延迟越低越好，成功率与缓存命中率越高越好。</p><table class="routing-metric-table"><thead><tr><th>指标</th><th>${sideNames.baseline}（固定路由）</th><th>${sideNames.candidate}（智能路由）</th><th>差值</th></tr></thead><tbody>${rows.map(([name,a,b,delta])=>`<tr><th>${esc(name)}</th><td>${a}</td><td>${b}</td><td>${pctDelta(delta,'%')}</td></tr>`).join('')}</tbody></table><div class="routing-metric-cards"><article class="routing-metric-card"><div class="report-card-kicker">成功率</div><div class="routing-side-pair">${routingRateCell(success.baseline,'成功/总请求')}<span class="routing-side-arrow">→</span>${routingRateCell(success.candidate,'成功/总请求')}</div></article><article class="routing-metric-card"><div class="report-card-kicker">缓存命中率</div><div class="routing-side-pair">${routingRateCell(cache.baseline)}<span class="routing-side-arrow">→</span>${routingRateCell(cache.candidate)}</div></article></div>${routingAttribution(success,'成功率')}${routingAttribution(cache,'缓存命中率')}${cache.note?`<p class="report-note">${esc(cache.note)}</p>`:''}${(cache.baseline_no_usage||cache.candidate_no_usage)?`<p class="report-note">没有 usage 的请求：${sideNames.baseline} ${num(cache.baseline_no_usage,0)} 条 / ${sideNames.candidate} ${num(cache.candidate_no_usage,0)} 条；这些请求已从命中率分母中剔除，而不是按未命中处理。</p>`:''}</section>`}
function routingReliabilityHtml(run){const report=run.report||{},rel=report.reliability||{},mcnemar=rel.mcnemar||{};const support=report.success?.mcnemar_supports;const supportText=support===true?'McNemar 精确检验支持「两侧成功/失败确有差异」。':support===false?'McNemar 精确检验不支持差异：不一致的请求很可能只是抽样波动。':'配对样本不足 20 条或无成功/失败不一致的请求，不做配对显著性判断。';return `<section class="report-section"><h2 class="report-section-title">配对一致性与判定口径</h2><div class="method-grid"><div class="method-card"><b>配对方式</b><span>按 item_id 一一配对；只有一侧有结果的请求整条剔除，绝不当作另一侧的失败。</span><span>已配对 ${num(rel.paired_total,0)} 对；剔除 ${num(rel.unpaired_count,0)} 条。</span></div><div class="method-card"><b>McNemar 精确检验</b><span>不一致对数 ${num(mcnemar.discordant,0)}；p = ${mcnemar.p_value==null?'—':esc(String(mcnemar.p_value))}</span><span>${esc(supportText)}</span></div><div class="method-card"><b>执行方式</b><span>两侧交错发出：第 k 条请求的两侧同时到期，奇偶交替决定谁先入队。</span><span>${report.method?.confidence||'Wilson 95%'} · ${report.method?.test||'Fisher 精确检验 / McNemar 精确检验'}</span></div></div>${rel.note?`<p class="report-note">${esc(rel.note)}</p>`:''}${(rel.unpaired_item_ids||[]).length?`<p class="report-note">未配对示例：${(rel.unpaired_item_ids||[]).slice(0,5).map(esc).join('、')}${rel.unpaired_count>5?' 等':''}</p>`:''}</section>`}
function routingMethodHtml(run){const report=run.report||{},manifest=run.corpus||{};return `<section class="report-section"><h2 class="report-section-title">测试集、口径与结论可信范围</h2><div class="method-grid"><div class="method-card"><b>请求清单</b><span>${num(run.tier,0)} 条请求 · ${num(manifest.burst_count,0)} 段 · 每 ${num(manifest.burst_size,0)} 条一段共享长前缀</span><span>清单指纹：${esc(manifest.item_id_digest||'未记录')}（版本 ${esc(manifest.id||'—')}）</span></div><div class="method-card"><b>缓存判定口径</b><span>命中标准：usage.cached_tokens &gt; 0 即计为一次命中；命中率 = 命中数 / 能取到 usage 的请求数。</span><span>无法取到 usage 的请求不计入分母，也不用「未命中」冒充。</span></div><div class="method-card"><b>唯一变量</b><span>模型、并发、计划调度 RPM、超时、请求内容等两侧逐项相同，只有 URL 与密钥背后的路由不同。</span><span>模型：${esc(run.sides?.baseline?.model||'—')}</span></div></div><ul class="report-limitations">${(report.limitations||run.limits||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></section>`}
function routingReportHtml(run){return `${routingMetricTable(run)}${routingReliabilityHtml(run)}${routingMethodHtml(run)}`}
function routingSideFactsHtml(run,side){const key=side?.key||side;const sideCfg=run.sides?.[key]||{};const report=(run.report||{})[key]||{};const cache=key==='baseline'?(run.report||{}).cache?.baseline:(run.report||{}).cache?.candidate;const noUsage=key==='baseline'?(run.report||{}).cache?.baseline_no_usage:(run.report||{}).cache?.candidate_no_usage;return `<section class="report-section"><h2 class="report-section-title">${esc(sideCfg.name||key)} · 原始观测</h2><div class="fact-grid"><article class="fact-card"><div class="report-card-kicker">请求与成功率</div><dl class="fact-list"><div class="fact-row"><dt>已收到结果</dt><dd><b>${num(report.requests,0)}</b> 条</dd></div><div class="fact-row"><dt>成功</dt><dd><b>${num(report.success?.successes,0)}</b> 条<span class="fact-ratio">成功率 ${comparisonMetric(report.success?.pct)}%</span></dd></div><div class="fact-row"><dt>95% 置信区间</dt><dd>${report.success?.ci_low_pct==null?'—':`${num(report.success.ci_low_pct)}% ~ ${num(report.success.ci_high_pct)}%`}<small>Wilson 得分区间，样本越小区间越宽</small></dd></div></dl></article><article class="fact-card"><div class="report-card-kicker">缓存与延迟</div><dl class="fact-list"><div class="fact-row"><dt>缓存命中</dt><dd><b>${num(cache?.hits,0)}</b> / ${num(cache?.eligible,0)} 条<span class="fact-ratio">命中率 ${comparisonMetric(cache?.pct)}%</span></dd></div><div class="fact-row"><dt>无 usage</dt><dd><b>${num(noUsage,0)}</b> 条<small>不计入命中率分母</small></dd></div><div class="fact-row"><dt>TTFT p95 / 总耗时 p95</dt><dd><b>${comparisonMetric(report.ttft_p95_ms)}</b> ms / <b>${comparisonMetric(report.total_p95_ms)}</b> ms</dd></div></dl></article></div><div class="method-grid"><div class="method-card"><b>本次连接信息</b><span>${esc(sideCfg.url||'—')}</span><span>模型 ${esc(sideCfg.model||'—')} · 记录 ${num(sideCfg.records,0)} 条</span></div><div class="method-card"><b>并发与节奏</b><span>两侧并发 ${num(run.concurrency?.baseline,0)} / ${num(run.concurrency?.candidate,0)}；计划调度 RPM ${num(run.rpm,0)}</span><span>计划调度速率只描述期望节奏，不是实际完成吞吐。</span></div></div></section>`}
function renderRoutingDetail(run){window.currentRoutingDetail=run;const host=$('modulePlaceholder');host.dataset.routingId=run.id;host.dataset.routingTab=routingTab;$('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;host.hidden=false;const tabs=[['baseline',run.sides?.baseline?.name||'固定路由'],['candidate',run.sides?.candidate?.name||'智能路由'],['report','对照结论']];if(!tabs.some(x=>x[0]===routingTab))routingTab='baseline';const side=tabs.find(x=>x[0]===routingTab);const body=routingTab==='report'?routingReportHtml(run):routingSideFactsHtml(run,{key:routingTab,name:side?side[1]:''});const pct=run.status==='finished'?100:Math.round(Math.max(0,Math.min(1,Number(run.progress?.pct)||0))*100);const live=routingTab==='report'?`<section class="report-progress-card"><div class="report-card-kicker">执行进度</div><div class="report-progress-line"><strong>${pct}%</strong><span>${num(run.progress?.done,0)} / ${num(Number(run.tier||0)*2,0)} 条请求已返回 · 两侧各 ${num(run.tier||0)} 条</span></div><div class="report-progress-track"><i style="width:${pct}%"></i></div><p>${run.status==='running'?'正在交错发出两侧请求；完成前不把局部结果当作结论。':run.status==='finished'?'两侧请求已全部返回，以下结论基于本次完整执行窗口。':'本次运行未正常结束；以下数据只描述已发生的部分。'}</p></section>`:'';host.innerHTML=`<div class="detail-layout report-shell"><header class="report-head"><div><div class="report-kicker">路由对照 · 固定与智能路由</div><h1>${esc(run.name)}</h1><p>同一份请求清单、同一节奏、同一并发，交错打在两条路由上；唯一变量是 URL 与密钥背后的路由。</p><div class="report-meta"><span>${routingStatusLabel(run.status)}</span><span>${num(run.tier,0)} 条 × 2 侧</span><span>计划调度 ${num(run.rpm,0)} RPM</span><span>${run.config?.capture_raw?'已开启原始记录':'未开启原始记录'}</span></div></div><div class="report-status ${run.status==='finished'?'good':run.status==='error'?'risk':''}">${esc(routingStatusLabel(run.status))}<br><small>${esc(run.error||'按请求编号配对')}</small></div></header><nav class="report-tabs" role="tablist" aria-label="路由对照详情标签页">${tabs.map(([id,label])=>`<button class="report-tab ${routingTab===id?'active':''}" role="tab" aria-selected="${routingTab===id}" type="button" onclick="setRoutingTab('${esc(run.id)}','${esc(id)}')">${esc(label)}</button>`).join('')}</nav>${artifactNotice(run)}${live}${body}<div class="report-section report-footer-actions">${['waiting','running'].includes(run.status)?`<button class="btn danger" type="button" onclick="stopRouting('${esc(run.id)}')">停止后续请求</button>`:''}<button class="btn" type="button" onclick="setRecordFilter('compare')">返回记录</button><button class="btn danger" type="button" onclick="deleteRouting('${esc(run.id)}')" ${['waiting','running','stopping'].includes(run.status)?'disabled title="运行结束后才能删除"':''}>删除本地记录</button></div></div>`}
function reportStatus(status){return statusNames[status]||status||'准备中'}
function reportTag(level){return level==='risk'?'风险':level==='caution'?'关注':level==='good'?'正常':'待完成'}
function signedMetric(v,suffix=''){return v==null?'—':`${Number(v)>0?'+':''}${num(v)}${suffix}`}
function comparisonOverviewHtml(c){const rows=c.report?.groups||c.groups||[],report=c.report||{},baselineId=report.baseline_group||rows.find(x=>x.role==='baseline')?.id,baseline=rows.find(x=>x.id===baselineId)||{},complete=rows.filter(x=>['finished','finished_with_errors'].includes(x.status)).length,totalGroups=Number(c.total_groups||rows.length||0),planned=Number(c.planned_requests||rows.reduce((sum,x)=>sum+Number(x.iterations||0),0)),completed=Number(c.completed_requests!=null?c.completed_requests:rows.reduce((sum,x)=>sum+Number(x.completed_requests||x.summary?.done||0),0)),finished=c.status==='finished'||c.status==='finished_with_errors',pct=finished?100:planned?Math.round(Math.max(0,Math.min(1,completed/planned))*100):(totalGroups?Math.round(complete/totalGroups*100):0),current=c.current_group_name?`当前执行：${esc(c.current_group_name)}`:finished?'全部实验组已完成':c.status==='running'?'正在执行实验组':'等待执行';const hint=finished?'全部实验组已完成，以下结论基于本次完整执行窗口。':c.status==='running'?`当前已完成 ${num(completed,0)} / ${num(planned,0)} 条请求，等待剩余实验组完成。`:'实验尚未完成；当前只展示过程数据，不把局部结果包装成最终结论。';return `<section class="report-overview"><section class="report-progress-card"><div class="report-card-kicker">实验整体进度</div><div class="report-progress-line"><strong>${pct}%</strong><span>${num(completed,0)} / ${num(planned,0)} 条请求 · ${complete}/${totalGroups} 组完成</span></div><div class="report-progress-track"><i style="width:${pct}%"></i></div><p>${hint} · ${current}</p></section><section class="report-focus-card"><div class="report-card-kicker">本次参照</div><strong>${esc(baseline.name||'尚未生成基准值')}</strong><span>基准组 · ${num(baseline.concurrency,0)} 并发</span><small>完成吞吐 ${comparisonMetric(baseline.completed_rpm)} RPM · 成功率 ${comparisonMetric(baseline.success_pct)}%</small></section></section><div class="report-baseline-strip"><span>基准值是本次实验内部参照，不是绝对标准</span><b>完成吞吐 ${comparisonMetric(baseline.completed_rpm)} RPM</b><b>成功率 ${comparisonMetric(baseline.success_pct)}%</b><b>总耗时 p95 ${comparisonMetric(baseline.total_p95_ms)} ms</b></div>`}
function comparisonReportHtml(c, options={}){
 const report=c.report||{}, conclusion=report.conclusion||{}, decision=report.decision||{}, evidence=report.evidence||{}, comparison=report.comparability||{}, capacity=report.capacity||{}, rows=report.groups||c.groups||[], baseline=report.baseline_group||rows.find(x=>x.role==='baseline')?.id, comparisons=rows.filter(x=>x.id!==baseline);
 const complete=rows.filter(x=>['finished','finished_with_errors'].includes(x.status)).length;
 const validThroughput=rows.filter(x=>x.completed_rpm!=null).sort((a,b)=>Number(b.completed_rpm)-Number(a.completed_rpm));
 const highest=validThroughput[0]||{};
 const worstSuccess=comparisons.reduce((worst,x)=>x.delta_success_pct!=null&&(worst==null||Number(x.delta_success_pct)<Number(worst.delta_success_pct))?x:worst,null);
 const worstP95=comparisons.reduce((worst,x)=>x.delta_total_p95_ms!=null&&(worst==null||Number(x.delta_total_p95_ms)>Number(worst.delta_total_p95_ms))?x:worst,null);
 const signals=conclusion.signals||[], nextSteps=decision.next_steps||[];
 const decisionTitle=decision.label||'等待实验结果';
 const statusLabel=decision.status==='hold'?'hold':decision.status==='review'?'review':decision.status==='observe'?'observe':'pending';
 const compClass=comparison.level==='comparable'?'good':comparison.level==='partial'?'caution':'risk';
 const compLabel=comparison.label||'待判断';
 const pressureSignal=report.pressure_window_signal||report.capacity_inflection;
 const overview=options.withoutOverview?'':comparisonOverviewHtml(c);
 return `${overview}<section class="report-section report-decision-hero ${statusLabel}"><div class="decision-main"><div class="decision-eyebrow">结论先行 · 只回答当前证据能支持什么</div><h2>${esc(decisionTitle)}</h2><p class="decision-summary">${esc(conclusion.summary||'实验结果尚未完整，暂不能形成结论。')}</p><div class="decision-scope"><b>适用范围</b><span>${esc(decision.scope||'结论适用范围将在实验完成后给出。')}</span></div></div><div class="decision-badge ${conclusion.level||'neutral'}"><span>${reportTag(conclusion.level)}</span><small>${esc(conclusion.title||'等待实验结果')}</small></div><div class="decision-action-row"><div><span class="decision-action-label">建议动作</span><strong>${esc(decision.recommendation||'等待实验完成')}</strong></div><div class="decision-guardrail"><span>正式放量</span><b>不自动批准</b></div></div></section><section class="report-section report-readout-section"><div class="report-section-title-row"><div><h2 class="report-section-title">快速读报告</h2><p class="report-section-sub">先看证据边界，再看指标差异；客户端观测值不等于网关内部真实容量。</p></div><span class="evidence-badge">${esc(evidence.grade||'低')}证据</span></div><div class="report-readout-grid"><div class="readout-card"><span class="readout-label">实验可比性</span><strong class="readout-value ${compClass}">${esc(compLabel)}</strong><p>${esc(comparison.note||'未生成可比性说明。')}</p></div><div class="readout-card"><span class="readout-label">证据量</span><strong class="readout-value">${num(evidence.completed_requests,0)} <small>条完成请求</small></strong><p>最小实验组样本 ${num(evidence.min_group_sample,0)} 条；证据量级不是统计置信区间。</p></div><div class="readout-card"><span class="readout-label">执行顺序</span><strong class="readout-value">基准组优先</strong><p>随后按顺序串行执行，可能受到连接预热、缓存和服务状态时间差影响。</p></div><div class="readout-card"><span class="readout-label">实验进度</span><strong class="readout-value">${complete}/${rows.length||0} <small>组完成</small></strong><p>${complete===rows.length&&rows.length?'所有组均已完成，可查看最终窗口判断。':'所有组完成前，不生成最终对照结论。'}</p></div></div></section><section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">关键指标</h2><p class="report-section-sub">用实际完成结果描述本次窗口；“计划调度 RPM（每分钟请求数）”只代表投放意图。</p></div></div><div class="report-metrics report-metrics-emphasis"><div class="report-metric metric-primary"><div class="metric-label">客户端观测的稳定完成吞吐</div><div class="metric-value">${comparisonMetric(capacity.observed_max_stable_completed_rpm)}<small>RPM</small></div><div class="metric-note">${esc(capacity.observed_max_stable_group||'没有足够稳定样本')} · 不是网关容量承诺</div></div><div class="report-metric"><div class="metric-label">最高客户端观测完成吞吐</div><div class="metric-value">${comparisonMetric(highest.completed_rpm)}<small>RPM</small></div><div class="metric-note">${esc(highest.name||'没有已完成结果')}</div></div><div class="report-metric"><div class="metric-label">最差成功率变化</div><div class="metric-value">${signedMetric(worstSuccess?.delta_success_pct,'%')}</div><div class="metric-note">相对基准组；负值表示退化</div></div><div class="report-metric"><div class="metric-label">最大总耗时 p95 变化</div><div class="metric-value">${signedMetric(worstP95?.delta_total_p95_ms,' ms')}</div><div class="metric-note">正值表示长尾变慢</div></div></div></section><section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">关键发现</h2><p class="report-section-sub">异常信号是复测线索，不是单凭一次黑盒实验做出的根因结论。</p></div></div><div class="signal-list report-signal-list">${signals.length?signals.map(x=>`<div class="signal ${x.level||''}"><i class="signal-dot"></i><div><b>${esc(x.title||'')}</b><br>${esc(x.detail||'')}</div></div>`).join(''):`<div class="signal good"><i class="signal-dot"></i><div><b>当前窗口没有额外异常信号</b><br>这不代表长期稳定，也不代表可以直接扩大流量。</div></div>`}</div></section><section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">实验组结果</h2><p class="report-section-sub">基准组是同一条实验记录内的参照；每个对比组的差值均相对它计算。</p></div><span class="table-hint">${complete}/${rows.length||0} 组完成 · ${pressureSignal?'有压力窗口信号':'暂未发现压力窗口信号'}</span></div><div class="table-wrap report-wrap"><table class="report-table comparison-result-table"><thead><tr><th>实验组</th><th>并发</th><th>计划调度 RPM</th><th>客户端观测完成 RPM</th><th>计划兑现率</th><th>成功率</th><th>总耗时 p50 / p95</th><th>调度延迟 p95</th><th>失败/异常</th><th>相对基准</th></tr></thead><tbody>${rows.length?rows.map(x=>`<tr class="${x.id===baseline?'baseline-row':''}"><td><b>${esc(x.name||'实验组')}</b><small class="role-label">${x.id===baseline?'基准组':'对比组'} · ${x.status==='finished'?'已完成':x.status==='finished_with_errors'?'完成但有错误':x.status==='running'?'执行中':'待执行'}</small></td><td>${num(x.concurrency)}</td><td>${num(x.planned_rpm)}</td><td><b>${num(x.completed_rpm)}</b></td><td>${num(x.dispatch_fulfilment_pct)}%</td><td>${num(x.success_pct)}%</td><td>${num(x.total_p50_ms)} / ${num(x.total_p95_ms)} ms</td><td>${num(x.schedule_lag_p95_ms)} ms</td><td>${num(x.failure_count,0)}</td><td>${x.id===baseline?'—':`${pctDelta(x.delta_success_pct,'%')} · ${pctDelta(x.delta_completed_rpm,' RPM')} · ${pctDelta(x.delta_total_p95_ms,' ms')}`}</td></tr>`).join(''):`<tr><td colspan="10">暂无实验结果</td></tr>`}</tbody></table></div></section><section class="report-section next-step-section"><div class="report-section-title-row"><div><h2 class="report-section-title">下一步动作</h2><p class="report-section-sub">报告的作用是把观察结果转成可执行的复测和准入动作。</p></div></div><div class="next-step-grid">${(nextSteps.length?nextSteps:['等待产生足够数据后再决定']).map((x,i)=>`<div class="next-step"><span>${i+1}</span><p>${esc(x)}</p></div>`).join('')}</div></section>`;
}
function comparisonSupplementHtml(c){const r=c.report||{},e=r.evidence||{},m=r.method||{},signal=r.pressure_window_signal||r.capacity_inflection,rows=r.groups||[],comp=r.comparability||{};return `<div class="report-section report-supplement"><div class="report-section-title-row"><div><h2 class="report-section-title">证据、可比性与方法</h2><p class="report-section-sub">这部分说明结果如何产生、哪些差异可以比较，以及哪些结论明确不能从本次实验推出。</p></div><span class="evidence-badge">证据量级：${esc(e.grade||'低')}</span></div><div class="report-metrics"><div class="report-metric"><div class="metric-label">已完成请求</div><div class="metric-value">${num(e.completed_requests,0)}<small>条</small></div><div class="metric-note">仅统计已经收到最终结果的请求</div></div><div class="report-metric"><div class="metric-label">最小组样本</div><div class="metric-value">${num(e.min_group_sample,0)}<small>条</small></div><div class="metric-note">组间证据量越不均衡，横向比较越弱</div></div><div class="report-metric"><div class="metric-label">压力窗口信号</div><div class="metric-value">${signal?esc(signal.group_name||'已发现'):'暂未发现'}</div><div class="metric-note">${signal?esc((signal.reasons||[]).join('；')):'当前样本未满足启发式阈值'}</div></div><div class="report-metric"><div class="metric-label">基准组</div><div class="metric-value">${esc((rows.find(x=>x.id===r.baseline_group)||{}).name||'—')}</div><div class="metric-note">所有对比组均与该组比较</div></div></div><div class="method-grid report-method-grid"><div class="method-card"><b>实验可比性 · ${esc(comp.label||'待判断')}</b><span>${esc(comp.note||'未生成可比性说明。')}</span></div><div class="method-card"><b>计划调度 RPM（每分钟请求数）</b><span>客户端尝试投放的节奏，不等于请求已开始，更不等于网关实际完成能力。</span></div><div class="method-card"><b>客户端观测完成 RPM（每分钟请求数）</b><span>按已完成请求数 ÷ 实际运行时间计算，只描述本次客户端观测窗口。</span></div><div class="method-card"><b>串行顺序效应</b><span>${esc(m.order_effect||'基准组优先、对比组随后串行执行；结果可能受到时间顺序影响。')}</span></div></div><ul class="report-limitations"><li>${esc(e.note||'证据量级不等于统计置信区间。')}</li><li>当前执行是串行实验，不能代表多个压力组同时打入网关的混压场景。</li><li>压力窗口信号是启发式提示，不是容量上限、稳定承载量或正式放量依据。</li><li>黑盒结果只能描述客户端可观测差异，不能证明上游来源、官方身份或资源真伪。</li></ul></div>`}
const qualityCategoryLabels={"numerical_reasoning": "数值与应用计算", "logical_reasoning": "逻辑与约束推理", "structured_extraction": "结构化抽取", "instruction_following": "指令与格式遵循", "reading_comprehension": "阅读与流程理解", "grounded_answering": "依据与不确定性", "code_reasoning": "代码与查询理解", "data_analysis": "数据分析", "multilingual": "跨语言理解", "long_context": "长上下文检索与整合", "robustness": "噪声与边界输入", "safety_boundary": "给定安全规则执行", "basic_response": "基础响应", "math": "数学计算", "structured_json": "结构化输出", "long_text_consistency": "长文本一致性"};

function qualityProgress(q){const resources=q.resources||[],total=Number(q.planned_cases??(Number(q.case_count||0)*Number(q.total_resources||q.config?.resources?.length||resources.length))),done=Number(q.completed_cases??resources.reduce((n,r)=>n+(r.case_results||[]).filter(x=>x.score!=null&&typeof x.passed==='boolean').length,0));return {total,done,pct:total?Math.min(100,Math.floor(done*100/total)):0,resources:Number(q.total_resources||q.config?.resources?.length||resources.length)}}
function qualityProgressHtml(q){const p=qualityProgress(q),active=['running','waiting'].includes(q.status);let state=q.current_resource?`当前：${esc(q.current_resource)}${q.current_case?` · 第 ${num(q.current_case,0)} / ${num(q.case_count,0)} 题`:''} · ${q.phase==='scoring'?'正在评分':'等待响应'}`:active?'等待执行':p.done<p.total?'执行已结束，仍有题目未完成；不能生成完整质量判断。':'全部题目已评测；请求成功与评分通过请分别查看。';return `<section class="report-progress-card"><div class="report-card-kicker">题集执行进度</div><div class="report-progress-line"><strong>${p.pct}%</strong><span>${num(p.done,0)} / ${num(p.total,0)} 题已评测 · ${num(q.completed_resources,0)} / ${p.resources} 个资源完成</span></div><div class="report-progress-track" role="progressbar" aria-label="题集执行进度" aria-valuemin="0" aria-valuemax="${p.total}" aria-valuenow="${p.done}"><i style="width:${p.pct}%"></i></div><p>${state}</p><p>每题收到最终结果、完成评分并保存后才更新进度；请求错误也计为已评测，不代表通过。</p></section>`}
function qualityPctl(resource, ratio){const values=(resource?.case_results||[]).filter(x=>x.total_ms!=null).map(x=>Number(x.total_ms)).filter(Number.isFinite).sort((a,b)=>a-b);if(!values.length)return null;const index=Math.min(values.length-1,Math.max(0,Math.ceil(values.length*ratio)-1));return values[index]}
function qualityStats(resource){const s=resource?.summary||{},cases=resource?.case_results||[],errors=resource?.request_errors||[];const total=Number(resource?.planned_cases||s.total_cases||cases.length||0),passed=Number(s.passed_cases??cases.filter(x=>x.passed).length),completed=cases.filter(x=>x.score!=null&&typeof x.passed==='boolean').length;const timings=cases.filter(x=>x.total_ms!=null).map(x=>Number(x.total_ms)).filter(Number.isFinite);const average=timings.length?timings.reduce((a,b)=>a+b,0)/timings.length:null;return {total,passed,passRate:s.pass_rate==null?(total&&completed===total?passed/total:null):Number(s.pass_rate),score:s.percentage==null?(s.score==null?null:Number(s.score)*100):Number(s.percentage),completed,errors:errors.length,totalMsP50:qualityPctl(resource,.5),totalMsP95:qualityPctl(resource,.95),averageTotalMs:average}}
function qualityPercent(value){return value==null?null:Number(value)*100}

function qualityDelta(a,b){if(a==null||b==null||!Number.isFinite(Number(a))||!Number.isFinite(Number(b)))return null;return Number(b)-Number(a)}
function qualityConclusion(q){
 const backend=q.report||{};
 // 结论只从后端报告读取，避免前端重复实现阈值后与报告出现矛盾。
 if(backend.title)return {level:backend.level||'neutral',title:backend.title,summary:backend.summary||'暂无结论摘要。',scope:backend.decision?.scope||'结论范围待确认',action:backend.decision?.recommendation||'查看逐题证据并复测。'};
 const resources=q.resources||[],a=qualityStats(resources[0]);
 if(q.status!=='finished'&&q.status!=='finished_with_errors')return {level:'neutral',title:'测试尚未完成',summary:'题集尚未全部执行完，当前数据只能用于查看进度，不能形成最终质量判断。',scope:'进行中',action:'等待题集完成后再判断。'};
 return {level:a.errors?'risk':a.passRate>=.95?'good':'caution',title:a.errors?'资源存在请求链路错误':a.passRate>=.95?'固定题集下表现稳定':'固定题集下存在未通过题',summary:`通过 ${a.passed}/${a.total} 道题；这是能力、格式和完整性的黑盒观测，不是官方来源或长期稳定性证明。`,scope:a.errors?'可定位本次题集的问题':'可做初步能力判断',action:a.errors?'先查看请求错误，再用相同题集复测。':a.passRate>=.95?'可作为当前题集下的初步基线；如需承诺仍需扩大题集。':'进入逐题证据定位未通过原因，并复测确认。'};
}

function categoryLabel(category){return qualityCategoryLabels[category]||'未分类'}
function qualityFactsHtml(q){const facts=q.report?.facts||[];return `<section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">第一层 · 事实：这次实际发生了什么</h2><p class="report-section-sub">只列观测计数，不含判断；每个数字都能在下面的逐题证据里逐条核对。</p></div><span class="evidence-badge">原始计数</span></div><div class="fact-grid">${facts.map(f=>`<article class="fact-card"><div class="report-card-kicker">${esc(f.resource||'资源')}</div><p class="fact-headline">${esc(f.headline)}</p><dl class="fact-list">${(f.items||[]).map(x=>`<div class="fact-row"><dt>${esc(x.label)}</dt><dd><b>${num(x.value,0)}</b>${x.ratio!=null?`<span class="fact-ratio">占已测 ${comparisonMetric(x.ratio)}%</span>`:''}<small>${esc(x.note)}</small></dd></div>`).join('')}</dl></article>`).join('')}</div><ul class="report-limitations">${facts.map(f=>`<li>${esc(f.note)}</li>`).join('')}</ul></section>`}
function qualityLayersHtml(q){const report=q.report||{},layers=report.layers||{},conclusion=qualityConclusion(q),stats=layers.statistics||{},advice=layers.advice||{};return `<section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">第二层 · 判断：这些数字说明什么</h2><p class="report-section-sub">相对内部阈值和本次参照得出的判断，不是行业结论，也没有做显著性检验。</p></div><span class="evidence-badge">${esc(report.evidence?.grade||'证据不足')}</span></div><div class="conclusion-banner ${esc(conclusion.level)}"><strong>${esc(conclusion.title)}</strong><p>${esc(conclusion.summary)}</p></div><ul class="layer-notes">${(stats.notes||[]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li>等待完整证据后再给出判断。</li>'}</ul><p class="layer-basis">${esc(stats.basis||report.evidence?.scope||'')}</p></section><section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">第三层 · 建议：接下来做什么</h2><p class="report-section-sub">${esc(advice.boundary||'建议面向业务复核，不构成自动准入。')}</p></div></div><div class="conclusion-banner ${esc(conclusion.level)} advice"><strong>${esc(advice.summary||report.decision?.recommendation||'等待证据')}</strong></div><div class="next-step-grid">${(advice.steps||[report.decision?.recommendation||'等待证据']).map((x,i)=>`<div class="next-step"><span>${i+1}</span><p>${esc(x)}</p></div>`).join('')}</div></section>`}
function qualityScoreCard(r,index,q){const final=q.report?.evidence?.complete;return `<article class="quality-score-card ${r.health_level}"><div class="report-card-kicker">${esc(r.name||`资源 ${index+1}`)} · ${final?'内部评分':'阶段评分'}</div><div class="quality-score-number">${comparisonMetric(r.score_1000 ?? (r.score_pct!=null?r.score_pct*10:null))}<small> / 1000</small></div><p>${esc(r.health_title||'等待结果')}</p><div class="quality-score-facts"><span>通过率 <b>${comparisonMetric(r.pass_rate_pct)}%</b></span><span>请求成功 <b>${comparisonMetric(r.request_success_pct)}%</b></span><span>关键失败 <b>${r.critical_failures?.length||0} 题</b></span></div>${r.constraint_diagnostics?.checks?`<p class="quality-constraint-diagnostic">指令约束通过 ${r.constraint_diagnostics.passed_checks} / ${r.constraint_diagnostics.checks} · 辅助诊断，不计入主分</p>`:''}<small>项目内部 1000 分尺度，各维度等权、链路失败计零分；不是行业基准分。</small></article>`}
function qualityScoreSectionHtml(q){const resources=q.report?.resources||[];return `<section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">内部评分（参考项，不是主结论）</h2><p class="report-section-sub">分数是把上面的计数按固定阈值折算出来的，先看事实再看分数，避免被单个数字带偏。</p></div></div><div class="quality-score-grid">${resources.map((r,i)=>qualityScoreCard(r,i,q)).join('')}</div></section>`}
function qualityReportHtml(q){const report=q.report||{},resources=report.resources||[],paired=report.mode==='comparison',manifest=q.config?.suite_manifest||{},policy=report.policy||{};return `${qualityFactsHtml(q)}${qualityLayersHtml(q)}${qualityScoreSectionHtml(q)}${paired?qualityPairedHtml(q):qualityDiagnosisHtml(q)}${qualityDimensionHtml(q)}${qualityFailureHtml(q)}<section class="report-section quality-progress-secondary">${qualityProgressHtml(q)}</section><section class="report-section"><h2 class="report-section-title">题集、评分口径与结论可信范围</h2><button class="btn" onclick="showQualityMethodology('report')">评测方法与官方参考</button><p>${esc(report.execution?.label||'历史执行策略未记录')} · ${esc(report.execution?.description||'')}</p><div class="method-grid"><div class="method-card"><b>${esc(manifest.title||'历史冒烟题集')}</b><span>${q.case_count||0} 题 · ${(manifest.categories||[]).length||'未记录'} 个维度 · 版本 ${manifest.version||q.config?.suite_version||1}</span><span>题集指纹：${esc(manifest.fingerprint||'历史记录未保存')}</span></div><div class="method-card"><b>评分与内部参考线</b><span>${esc(manifest.scoring||'按历史题目规则评分')}</span><span>展示总分 /1000；内部阈值仍按百分比：总分 ≥ ${policy.total_score_min??85}；各维度 ≥ ${policy.category_score_min??70}；请求错误与关键失败均为零；足量覆盖后才进入业务复核。</span><span>${esc(policy.note||'内部参考阈值')}</span></div><div class="method-card"><b>${esc(report.evidence?.grade||'证据不足')}</b><span>${esc(report.evidence?.repeatability||'未验证重复性')}</span><span>成功响应得分与整体质量分分开，超时不从总体分母移除。</span></div></div><ul class="report-limitations">${(report.limitations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></section>`}
function evidenceButton(resourceIndex,caseId,label='查看证据'){return `<button class="raw-link" type="button" data-resource="${resourceIndex}" data-case="${esc(caseId)}" onclick="showQualityCaseById(Number(this.dataset.resource),this.dataset.case)">${esc(label)}</button>`}
function qualityDiagnosisHtml(q){const report=q.report||{},r=report.resources?.[0]||{},single=report.single||{};return `<section class="report-section"><h2 class="report-section-title">单资源诊断 · 是否值得进入业务复核</h2><div class="method-grid"><div class="method-card"><b>本轮优势维度</b><span>${esc((single.strengths||[]).join('、')||'暂无充分证据')}</span></div><div class="method-card"><b>薄弱维度</b><span>${esc((single.weaknesses||[]).join('、')||(report.evidence?.complete?'未发现低于内部维度阈值的项目':'等待完整结果'))}</span></div><div class="method-card"><b>筛选阻断项</b><span>${esc((r.gate_reasons||[]).join('；')||'本题集内未触发阻断项；仍需人工复核')}</span></div></div></section><section class="report-section"><h2 class="report-section-title">适用场景与使用边界</h2><p class="report-section-sub">仅由本轮相关维度推导；“可进入业务复核”不是生产可用承诺。</p><div class="quality-scenario-grid">${(r.scenarios||[]).map(x=>`<article class="method-card"><b>${esc(x.name)} · ${esc(x.status)}</b><span>${esc(x.reason)}</span><small>${esc(x.dimensions.join('、'))}</small></article>`).join('')}</div></section>`}
function qualityPairedHtml(q){const p=q.report?.pairwise||{};const rows=[...(p.regressions||[]).map(x=>({...x,label:'B 退化'})),...(p.gains||[]).map(x=>({...x,label:'B 改善'})),...(p.shared_failures||[]).filter(x=>x.delta===0).map(x=>({...x,label:'共同失败'}))];return `<section class="report-section"><h2 class="report-section-title">资源对照 · 选择依据</h2><div class="conclusion-banner ${p.comparable?'caution':'risk'}"><strong>${esc(p.selection||'等待配对证据')}</strong><p>${esc(p.method||'')}</p><p>${esc(p.repeatability||'')}</p>${p.same_endpoint_model?'<p>两侧地址与模型名相同：可能只是重复观测，不能确认是独立资源。</p>':''}</div><div class="report-metrics"><div class="report-metric"><div class="metric-label">配对质量分差 · B − A</div><div class="metric-value">${signedMetric(p.score_delta_pct,' 分')}</div><div class="metric-note">仅按 ${p.paired_cases||0} 道有效同题配对；非稳定性证明</div></div><div class="report-metric"><div class="metric-label">A 独胜 / B 独胜</div><div class="metric-value">${p.a_wins||0} / ${p.b_wins||0}<small> 题</small></div><div class="metric-note">平分 ${p.ties||0} 题，不等于两侧都通过</div></div><div class="report-metric"><div class="metric-label">共同未通过</div><div class="metric-value">${p.both_failed||0}<small> 题</small></div><div class="metric-note">共同缺陷不能被相对分差掩盖</div></div><div class="report-metric"><div class="metric-label">成功响应长尾耗时差</div><div class="metric-value">${signedMetric(p.p95_delta_ms,' 毫秒')}</div><div class="metric-note">95分位耗时，B − A；速度不计入质量分</div></div></div></section><section class="report-section"><h2 class="report-section-title">逐题改善、退化与共同缺陷</h2><p class="report-section-sub">按同一题和相同规则对齐；先看退化。两侧证据分别打开，不能只看净分差。</p><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>题目</th><th>变化</th><th>分差</th><th>A 证据</th><th>B 证据</th></tr></thead><tbody>${rows.length?rows.map(x=>`<tr><td>${esc(x.title)}<small class="role-label">${esc(x.case_id)}</small></td><td>${x.label}</td><td>${signedMetric(x.delta,' 分')}</td><td>${evidenceButton(0,x.case_id)}</td><td>${evidenceButton(1,x.case_id)}</td></tr>`).join(''):'<tr><td colspan="5">暂无差异或共同失败；未测完时不能推断等效。</td></tr>'}</tbody></table></div></section>`}
function qualityDimensionHtml(q){const resources=q.report?.resources||[],ids=[...new Set(resources.flatMap(r=>(r.categories||[]).map(x=>x.category)))];return `<section class="report-section"><h2 class="report-section-title">能力维度 · 定位分数背后的问题</h2><p class="report-section-sub">维度等权；内部参考线为 ${q.report?.policy?.category_score_min??70} 分，不是行业认证。阶段分仅使用已测题。</p><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>能力维度</th>${resources.map(r=>`<th>${esc(r.name)} · 分值</th><th>通过 / 已测 / 计划</th>`).join('')}${resources.length===2?'<th>B − A</th>':''}</tr></thead><tbody>${ids.map(id=>{const rows=resources.map(r=>r.categories.find(x=>x.category===id)||{});return `<tr><td>${esc(categoryLabel(id))}</td>${rows.map(r=>`<td class="${r.score!=null&&r.score<70?'delta-risk':''}">${comparisonMetric(r.score)} / 100</td><td>${r.passed||0} / ${r.completed||0} / ${r.planned||0}</td>`).join('')}${rows.length===2?`<td>${signedMetric(qualityDelta(rows[0].score,rows[1].score),' 分')}</td>`:''}</tr>`}).join('')}</tbody></table></div></section>`}
function qualityFailureHtml(q){const rows=(q.report?.resources||[]).flatMap((r,i)=>(r.failure_evidence||[]).map(x=>({...x,resourceIndex:i})));return `<section class="report-section"><h2 class="report-section-title">优先复核清单</h2><p class="report-section-sub">关键规则、链路错误优先；点击证据查看参考答案、评分依据与本次实际输出。</p><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>资源</th><th>题目</th><th>问题分类</th><th>证据</th></tr></thead><tbody>${rows.length?rows.map(x=>`<tr><td>${esc(x.resource_name)}</td><td>${esc(x.title)}<small class="role-label">${esc(x.case_id)}</small></td><td>${esc(x.reason)}</td><td>${evidenceButton(x.resourceIndex,x.case_id)}</td></tr>`).join(''):'<tr><td colspan="4">当前没有已记录失败；未完成时不构成通过结论。</td></tr>'}</tbody></table></div></section>`}

function qualityCaseTable(q,resource,resourceIndex){const cases=resource?.case_results||[],capture=Boolean(q.config?.capture_raw);return `<div class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">逐题证据</h2><p class="report-section-sub">每道题显示评分结果、响应完整性和延迟；点击“查看详情”查看本题完整证据。${capture?'已保存的原始输入输出也会在详情中展示。':'本次未开启原始输入输出记录，详情会明确标注无法补看正文。'}</p></div><span class="table-hint">${cases.length} 道题</span></div><div class="table-wrap report-wrap"><table class="report-table quality-report-table"><thead><tr><th>题目</th><th>类别</th><th>结果</th><th>得分</th><th>耗时</th><th>失败检查</th><th>响应预览</th><th>详情</th></tr></thead><tbody>${cases.length?cases.map((x,idx)=>`<tr><td><b>${esc(x.title||x.case_id||'—')}</b><small class="role-label">${esc(x.case_id||'')}</small></td><td><b>${esc(categoryLabel(x.category))}</b><small class="role-label">${esc(x.category||'—')}</small></td><td><span class="tag ${x.passed?'ok':'error'}">${x.passed?'通过':'未通过'}</span></td><td>${comparisonMetric(Number(x.score||0)*100)}%</td><td>${num(x.total_ms)} ms</td><td>${esc((x.failures||[]).join('、')||x.error||'—')}</td><td><div class="case-answer">${esc(x.response_preview||x.response||'—')}</div></td><td><button type="button" class="raw-link" onclick="showQualityCase(${Number(resourceIndex)},${idx})">查看详情</button></td></tr>`).join(''):'<tr><td colspan="8">暂无逐题结果</td></tr>'}</tbody></table></div></div>`}
function qualityCaseDeltaHtml(a,b){const left=new Map((a?.case_results||[]).map(x=>[x.case_id,x])),right=new Map((b?.case_results||[]).map(x=>[x.case_id,x])),ids=[...new Set([...left.keys(),...right.keys()])];if(!ids.length)return '';return `<div class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">逐题对照差异</h2><p class="report-section-sub">逐题对齐资源 A 与资源 B；差异用于定位复核对象，不直接等同于资源真伪结论。</p></div><span class="table-hint">${ids.length} 道题</span></div><div class="table-wrap report-wrap"><table class="report-table quality-report-table"><thead><tr><th>题目</th><th>类别</th><th>资源 A</th><th>资源 B</th><th>得分差</th><th>耗时差</th><th>差异说明</th></tr></thead><tbody>${ids.map(id=>{const x=left.get(id)||{},y=right.get(id)||{},scoreA=Number(x.score),scoreB=Number(y.score),timeA=x.total_ms==null?NaN:Number(x.total_ms),timeB=y.total_ms==null?NaN:Number(y.total_ms),scoreDiff=Number.isFinite(scoreA)&&Number.isFinite(scoreB)?(scoreB-scoreA)*100:null,timeDiff=Number.isFinite(timeA)&&Number.isFinite(timeB)?timeB-timeA:null;const changed=x.case_id&&y.case_id&&x.passed!==y.passed,slow=timeDiff!=null&&Math.abs(timeDiff)>=100,leftLabel=x.case_id?(x.passed?'通过':'未通过'):'未返回',rightLabel=y.case_id?(y.passed?'通过':'未通过'):'未返回';return `<tr><td><b>${esc(id)}</b></td><td><b>${esc(categoryLabel(x.category||y.category))}</b><small class="role-label">${esc(x.category||y.category||'—')}</small></td><td><span class="tag ${x.case_id?(x.passed?'ok':'error'):'muted'}">${leftLabel}</span></td><td><span class="tag ${y.case_id?(y.passed?'ok':'error'):'muted'}">${rightLabel}</span></td><td class="${scoreDiff!=null&&scoreDiff<0?'delta-risk':scoreDiff!=null&&scoreDiff>0?'delta-good':'delta-neutral'}">${signedMetric(scoreDiff,'%')}</td><td class="${timeDiff!=null&&timeDiff>0?'delta-risk':'delta-neutral'}">${signedMetric(timeDiff,' ms')}</td><td>${!x.case_id||!y.case_id?'等待两侧结果':changed?'通过结果不同':slow?(timeDiff>0?'资源 B 更慢':'资源 B 更快'):'结果基本一致'}</td></tr>`}).join('')}</tbody></table></div></div>`}
function qualityResourceHtml(q,r){const i=(q.resources||[]).indexOf(r),s=q.report?.resources?.[i]||{};return `<div class="group-detail"><div class="group-detail-head"><div><div class="report-kicker">资源实例 · 逐题明细</div><h2>${esc(r.name||'资源')}</h2><p>模型 ${esc(r.model||'—')} · 通过 ${s.passed_cases||0}/${s.total_cases||0} 题</p></div><span class="group-badge">${qualityStatusLabel(r.status||q.status)}</span></div><div class="quality-score-grid single">${qualityScoreCard(s,i,q)}</div><div class="report-metrics"><div class="report-metric"><div class="metric-label">成功响应中位耗时</div><div class="metric-value">${comparisonMetric(s.p50_ms)}<small> 毫秒</small></div><div class="metric-note">只统计完整成功响应，不计入质量分</div></div><div class="report-metric"><div class="metric-label">成功响应95分位耗时</div><div class="metric-value">${comparisonMetric(s.p95_ms)}<small> 毫秒</small></div><div class="metric-note">错误快返不能成为速度优势</div></div><div class="report-metric"><div class="metric-label">请求错误</div><div class="metric-value">${s.request_errors||0}</div><div class="metric-note">${esc((r.request_errors||[]).map(x=>x.error_type||'未知').filter((v,i,a)=>a.indexOf(v)===i).join(' · ')||'无')}</div></div></div>${r.error?`<div class="notice">${esc(r.error)}</div>`:''}${['running','waiting'].includes(q.status)?qualityProgressHtml(q):''}${qualityCaseTable(q,r,i)}</div>`}
function qualityScrollSnapshot(host){return {main:document.querySelector('.main')?.scrollTop||0,tables:[...host.querySelectorAll('.table-wrap')].map(el=>[el.scrollTop,el.scrollLeft])}}
function restoreQualityScroll(host,snapshot){if(!snapshot){const main=document.querySelector('.main');if(main)main.scrollTop=0;return;}host.querySelectorAll('.table-wrap').forEach((el,i)=>{el.scrollTop=snapshot.tables[i]?.[0]||0;el.scrollLeft=snapshot.tables[i]?.[1]||0});const main=document.querySelector('.main');if(main)main.scrollTop=snapshot.main}
function showQualityCaseById(resourceIndex,caseId){const r=window.currentQualityDetail?.resources?.[resourceIndex];showQualityCase(resourceIndex,(r?.case_results||[]).findIndex(c=>c.case_id===caseId))}
function showQualityCase(resourceIndex,caseIndex){const q=window.currentQualityDetail,r=q?.resources?.[resourceIndex],x=r?.case_results?.[caseIndex];if(!x)return;const modal=$('qualityEvidenceModal');window.qualityEvidenceOpener=document.activeElement;openModal('qualityEvidenceModal');$('qualityEvidenceTitle').textContent=`${r.name} · ${x.title||x.case_id}`;const capture=q.config?.capture_raw;const pre=value=>esc(typeof value==='string'?value:JSON.stringify(value??null,null,2));$('qualityEvidenceBody').innerHTML=`<div class="report-meta"><span>${esc(x.case_id)}</span><span>${esc(categoryLabel(x.category))} · ${esc(x.difficulty||'未标注')}</span><span>得分 ${num(Number(x.score||0)*100)} / 100</span><span>${x.passed?'通过':'未通过'}${x.critical?' · 关键规则':''}</span><span>状态码 ${x.status_code??'未记录'}</span>${x.execution_sequence?`<span>本任务第 ${x.execution_sequence} 次请求</span>`:''}${x.input_chars!=null?`<span>输入 ${x.input_chars} 字符</span>`:''}</div><h3>题目与输入</h3><pre class="evidence-pre">${pre(x.prompt||'历史记录未保存题目')}</pre><div class="quality-scenario-grid"><div><h3>参考答案${x.evaluator?.type==='verifiable_constraints'?'（一种合规示例）':''}</h3><pre class="evidence-pre">${pre(x.reference_answer??'历史记录未保存参考答案')}</pre></div><div><h3>判分依据</h3><p>${esc(x.rationale||'历史记录未保存解释')}</p><p>检查项：${esc((x.checks||[]).join('；')||'未记录')}</p><p>未通过：${esc((x.failures||[]).join('；')||'无')}</p></div></div><h3>实际输出${capture?'':' · 仅保存的预览'}</h3><pre class="evidence-pre">${pre(x.response??x.response_preview??'没有输出')}</pre><p>${capture?'已开启原始记录。':'本次未开启原始记录，只有预览；不能补造完整正文。'}</p>${x.error?`<p role="alert">请求错误：${esc(x.error)}</p>`:''}<p>完整响应：${x.completed?'是':'否'} · 总耗时 ${num(x.total_ms)} 毫秒</p>${capture?`<details><summary>查看实际请求与原始响应</summary><h3>请求正文</h3><pre class="evidence-pre">${pre(x.raw_input??'旧记录没有保存')}</pre><h3>原始响应</h3><pre class="evidence-pre">${pre(x.raw_output??'没有保存原始响应正文')}</pre></details>`:''}`;modal.querySelector('.modal-body').scrollTop=0;modal.querySelector('.modal-close').focus()}
function closeQualityEvidence(){closeModal('qualityEvidenceModal')}
function renderQualityDetail(q){window.currentQualityDetail=q;const host=$('modulePlaceholder'),same=host.dataset.qualityId===q.id&&host.dataset.qualityTab===qualityTab,scroll=same?qualityScrollSnapshot(host):null;host.dataset.qualityId=q.id;host.dataset.qualityTab=qualityTab;$('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;host.hidden=false;const resources=q.resources||[],tabs=[['report',resources.length===2?'资源选型对照':'单资源诊断'],...resources.map((r,i)=>[String(i),`${r.name||('资源 '+(i+1))} · 逐题`])];if(!tabs.some(x=>x[0]===qualityTab))qualityTab='report';const current=qualityTab==='report'?null:resources[Number(qualityTab)];const body=current?qualityResourceHtml(q,current):qualityReportHtml(q);const c=qualityConclusion(q),manifest=q.config?.suite_manifest||{};host.innerHTML=`<div class="detail-layout report-shell"><header class="report-head"><div><div class="report-kicker">质量评测 · ${resources.length===2?'双资源配对对照':'单资源能力诊断'}</div><h1>${esc(q.name)}</h1><p>${resources.length===2?'先分别检查两侧是否达标，再比较同题胜负、维度差异和退化；不把分数接近当作等效。':'判断当前资源的优势、短板、阻断项与适用场景；不把完成度当作质量。'}</p><div class="report-meta"><span>${qualityStatusLabel(q.status)}</span><span>${q.case_count||0} 题 / 资源</span><span>${manifest.title?esc(manifest.title):'历史小题集 · 证据不足'}</span><span>${q.config?.capture_raw?'已开启原始记录':'未开启原始记录'}</span></div></div><div class="report-status ${esc(c.level)}">${esc(c.title)}</div></header><nav class="report-tabs" role="tablist" aria-label="质量测试详情标签页">${tabs.map(([id,label])=>`<button class="report-tab ${qualityTab===id?'active':''}" role="tab" aria-selected="${qualityTab===id}" type="button" onclick="setQualityTab('${q.id}','${id}')">${esc(label)}</button>`).join('')}</nav>${artifactNotice(q)}${body}<div class="report-section report-footer-actions">${['waiting','running'].includes(q.status)?`<button class="btn danger" onclick="stopQuality('${q.id}')">停止后续题目</button>`:''}<button class="btn" onclick="location.href='/quality-report/${q.id}?format=markdown'">下载结论报告</button><button class="btn" onclick="location.href='/quality-report/${q.id}'">下载报告与证据</button><button class="btn" onclick="setRecordFilter('${resources.length===2?'compare':'single'}')">返回记录</button><button class="btn danger" onclick="deleteQuality('${q.id}')">删除本地记录</button></div></div>`;restoreQualityScroll(host,scroll)}
async function stopQuality(id){if(!confirm('停止后不再发出后续请求；正在执行的一题会完成并保存。部分结果不会生成完整结论。继续？'))return;try{await api('/quality-test/'+id+'?stop',{method:'POST'});toast('已请求停止后续题目');refresh()}catch(e){toast(e.message)}}

function comparisonGroupHtml(c,g){const s=g.summary||{},cnt=s.counters||{},done=Number(s.done||0),total=Number(g.iterations||s.total||0),success=s.success_pct==null?null:Number(s.success_pct),recent=s.recent||[],capture=Boolean(c.config?.capture_raw||s.config?.capture_raw),taskId=g.task_id||s.id;const status=statusNames[g.status]||g.status||'待执行';const groupPct=g.status==='finished'||g.status==='finished_with_errors'?100:(g.progress!=null?Math.round(Math.max(0,Math.min(1,Number(g.progress)||0))*100):(total?Math.round(Math.max(0,Math.min(1,done/total))*100):0));const failures=Object.entries({限流429:cnt.r429,服务端5xx:cnt.r5xx,客户端4xx:cnt.r4xx,超时:cnt.timeout,网络错误:cnt.network,响应不完整:cnt.protocol}).filter(([,v])=>Number(v)>0);return `<div class="group-detail">${artifactNotice(s)}<div class="group-detail-head"><div><div class="report-kicker">实验组 · ${g.role==='baseline'?'基准组':'对比组'}</div><h2>${esc(g.name||'实验组')}</h2><p>并发上限 ${num(g.concurrency)} · 计划调度 ${num(g.rpm)} RPM（每分钟请求数）· 计划 ${num(total,0)} 条请求</p></div><span class="group-badge ${g.status==='finished'||g.status==='finished_with_errors'?'good':g.status==='running'?'running':''}">${esc(status)}</span></div><div class="group-progress-card"><div class="group-progress-head"><span>本组执行进度</span><b>${groupPct}%</b></div><div class="group-progress-track"><i style="width:${groupPct}%"></i></div><p>${num(done,0)} / ${num(total,0)} 条请求已完成；完成度不代表速度，速度请看运行时长、完成吞吐和 p95。</p></div><div class="report-metrics"><div class="report-metric"><div class="metric-label">实际运行时长</div><div class="metric-value">${duration(Number(g.elapsed||s.elapsed||0))}</div><div class="metric-note">从本组开始到最后一个结果完成</div></div><div class="report-metric"><div class="metric-label">开始 / 完成</div><div class="metric-value" style="font-size:14px">${g.started_at?new Date(g.started_at*1000).toLocaleTimeString():'—'}<small> → ${g.finished_at?new Date(g.finished_at*1000).toLocaleTimeString():'—'}</small></div><div class="metric-note">真实时间戳，不由进度条推算</div></div><div class="report-metric"><div class="metric-label">完成进度</div><div class="metric-value">${num(done,0)}<small> / ${num(total,0)} 条</small></div><div class="metric-note">已收到最终结果的请求</div></div><div class="report-metric"><div class="metric-label">成功率</div><div class="metric-value">${success==null?'—':num(success)}<small>${success==null?'':'%'}</small></div><div class="metric-note">仅完整 2xx（成功）响应计为成功</div></div><div class="report-metric"><div class="metric-label">完成吞吐</div><div class="metric-value">${num(s.rpm?.avg)}<small> RPM</small></div><div class="metric-note">按实际完成结果和运行窗口计算</div></div><div class="report-metric"><div class="metric-label">总耗时 p50 / p95</div><div class="metric-value">${num(s.total_ms?.p50)}<small> / ${num(s.total_ms?.p95)} ms</small></div><div class="metric-note">长尾耗时需结合超时和失败分布判断</div></div></div>${failures.length?`<div class="signal-list">${failures.map(([k,v])=>`<div class="signal ${k==='超时'||k==='服务端5xx'?'risk':'caution'}"><span class="signal-dot"></span><span>${esc(k)}：${num(v,0)} 条</span></div>`).join('')}</div>`:`<div class="signal good"><span class="signal-dot"></span><span>本组未记录失败或异常。</span></div>`}<section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">请求结果</h2><p class="report-section-sub">页面展示最近 ${num(recent.length,0)} 条；完整结果仍保存在本次实验组的 JSONL（逐行结构化数据）文件中。${capture?'已开启原始输入输出记录，点击下方“查看”。':'本次未开启原始记录，无法补录历史正文。'}</p></div></div><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>请求编号</th><th>状态码</th><th>结果</th><th>首字耗时</th><th>总耗时</th><th>原因</th>${capture?'<th>原始记录</th>':''}</tr></thead><tbody>${recent.length?recent.map(r=>`<tr><td>${num(r.id,0)}</td><td>${num(r.status,0)}</td><td><span class="tag ${esc(r.cls||'muted')}">${esc(r.label||'—')}</span></td><td>${num(r.ttft)} ms</td><td>${num(r.total)} ms</td><td>${esc(r.reason||'—')}</td>${capture?`<td>${taskId&&Number.isInteger(r.id)?`<button type="button" class="raw-link" onclick="showRaw('${esc(taskId)}',${Number(r.id)})">查看</button>`:'暂不可用'}</td>`:''}</tr>`).join(''):`<tr><td colspan="${capture?7:6}">当前组尚无可展示的请求结果。</td></tr>`}</tbody></table></div></section></div>`}
function renderComparisonDetail(c){$('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=false;const tabs=[['report','结论报告'],...(c.groups||[]).map(g=>[g.id,(g.role==='baseline'?'基准组 · ':'对比组 · ')+(g.name||'实验组')])];if(!tabs.some(x=>x[0]===comparisonTab))comparisonTab='report';const currentGroup=(c.groups||[]).find(x=>x.id===comparisonTab);const report=c.report?.conclusion||{};const body=comparisonTab==='report'?comparisonOverviewHtml(c)+comparisonReportHtml(c,{withoutOverview:true})+(typeof comparisonSupplementHtml==='function'?comparisonSupplementHtml(c):''):currentGroup?comparisonGroupHtml(c,currentGroup):'';$('modulePlaceholder').innerHTML=`<div class="detail-layout report-shell"><header class="report-head"><div><div class="report-kicker">网关压测 · 对照实验记录</div><h1>${esc(c.name)}</h1><p>一条实验记录承载公共配置和全部压力组；在标签页中分别查看结论、基准组和对比组。</p><div class="report-meta"><span>${reportStatus(c.status)}</span><span>${num((c.groups||[]).length,0)} 个实验组</span><span>${c.execution_policy==='serial'?'串行执行':'并行执行'}</span><span>基准组：${esc((c.groups||[]).find(g=>g.role==='baseline')?.name||'—')}</span></div></div><div class="report-status ${report?.level||''}">${reportTag(report?.level)}<br><small>${esc(report?.title||'等待结果')}</small></div></header><nav class="report-tabs" role="tablist" aria-label="对照实验详情标签页">${tabs.map(([id,label])=>`<button class="report-tab ${comparisonTab===id?'active':''}" role="tab" aria-selected="${comparisonTab===id}" type="button" onclick="setComparisonTab('${c.id}','${esc(id)}')">${esc(label)}${id===report?.baseline_group?' · 当前基准':''}</button>`).join('')}</nav>${body}<div class="report-section report-footer-actions"><button class="btn" type="button" onclick="setRecordFilter('compare')">返回对照实验</button><button class="btn danger" type="button" onclick="deleteComparison('${c.id}')">删除本地记录</button></div></div>`}
function resetCreateForm(){activeTemplateId=null;const picker=$('templatePicker');if(picker){picker.hidden=isQualityCreate()||isRoutingCreate();const body=$('templatePickerBody'),btn=$('templatePickerToggle');if(body)body.hidden=false;if(btn){btn.setAttribute('aria-expanded','true');btn.textContent='收起'}}if(!isQualityCreate()&&!isRoutingCreate()&&templateCache)renderTemplatePicker(templateCache);setCreateKind(activeModule==='routing'?'routing-compare':activeModule==='quality'?(activeView==='compare'?'quality-compare':'quality-single'):(activeView==='compare'?'gateway-compare':'gateway-single'),true);clearTimeout(preflightTimer);preflightSeq++;preflightData=null;preflightAckRequested=false;const card=$('preflightCard');if(card){card.hidden=true;card.innerHTML=''}if($('name'))$('name').value='';if($('comparisonName'))$('comparisonName').value='';if($('api_key')){$('api_key').value='';$('api_key').type='password'}if($('toggleKey'))resetKeyButton(true);updateKeyInputHint();if($('copyKey'))$('copyKey').textContent='复制';if($('capture_raw'))$('capture_raw').checked=false;if($('comparisonCapture'))$('comparisonCapture').checked=false;setSavedModelOption('');if(isGatewayCreate()){setMode('smoke');applyDynamic({rpm:600,iterations:50,duration:60})}if(isQualityCreate()&&$('qualityResourceFields')&&!isCompareCreate())$('qualityResourceFields').innerHTML=qualityResourceTemplate(0)}
function selectDefaultModuleAfterCreate(kind,id){activeView=kind.includes('compare')?'compare':'single';if(kind==='routing-compare'){activeModule='routing';routingTab='baseline';selectedRouting=id;selected=null;selectedComparison=null;selectedQuality=null}else if(kind==='quality-single'||kind==='quality-compare'){activeModule='quality';qualityTab='report';selectedQuality=id;selected=null;selectedComparison=null}else if(kind==='gateway-compare'){activeModule='gateway';selectedComparison=id;selected=null;selectedQuality=null}else{activeModule='gateway';selected=id;selectedComparison=null;selectedQuality=null}updateWorkspaceChrome()}
document.querySelectorAll('.module-nav-item').forEach(x=>x.onclick=()=>setModule(x.dataset.module));document.querySelectorAll('.subnav-item').forEach(x=>x.onclick=()=>setWorkspaceView(x.dataset.view));
function togglePasswordVisibility(id,button){const input=$(id);if(!input||!button)return;const visible=input.type==='text';input.type=visible?'password':'text';button.setAttribute('aria-pressed',String(!visible));button.setAttribute('aria-label',visible?'显示密码':'隐藏密码');button.innerHTML=visible?'<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></svg>':'<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m3 3 18 18M10.6 6.2A10.7 10.7 0 0 1 12 6c6 0 9.5 6 9.5 6a18 18 0 0 1-3.1 3.7M6.2 6.8C3.8 8.5 2.5 12 2.5 12s3.5 6 9.5 6c1.2 0 2.3-.2 3.3-.6"/></svg>';}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function num(v,d='—'){return v==null||Number.isNaN(v)?d:Number(v).toLocaleString('zh-CN',{maximumFractionDigits:1})}function duration(sec){sec=Math.max(0,Math.floor(sec||0));if(sec<60)return sec+' 秒';if(sec<3600)return Math.floor(sec/60)+'分 '+sec%60+'秒';return Math.floor(sec/3600)+'时 '+Math.floor(sec%3600/60)+'分 '+sec%60+'秒'}function durationPrecise(sec){sec=Math.max(0,Number(sec||0));if(sec<1)return Math.max(.1,sec).toLocaleString('zh-CN',{maximumFractionDigits:1})+' 秒';return duration(sec)}function toast(t){const el=$('toast');if(!el){console.warn('[toast] missing #toast',t);return}el.textContent=t;el.classList.add('show');clearTimeout(window.__toastTimer);window.__toastTimer=setTimeout(()=>el.classList.remove('show'),2400)}
function paintMode(next){mode=next;document.querySelectorAll('.tab[data-mode]').forEach(x=>{x.classList.toggle('active',x.dataset.mode===mode);x.setAttribute('aria-selected',x.dataset.mode===mode)});$('modeFields').innerHTML=modeTemplates[mode]}
function setMode(next){const old=readDynamic();paintMode(next);applyDynamic(old)}document.querySelectorAll('.tab[data-mode]').forEach(x=>x.onclick=()=>setMode(x.dataset.mode));
function readDynamic(){const v=id=>$(id)?.value;return{rpm:v('rpm'),iterations:v('iterations'),ramps:v('ramps'),duration:v('duration')}}function applyDynamic(cfg={}){for(const k of ['rpm','iterations','ramps','duration'])if($(k)&&cfg[k]!=null)$(k).value=cfg[k]}
let currentUser=null,authenticated=false,authSetupRequired=false,refreshTimer=null,authFocusTimer=null;
function stopRefresh(){if(refreshTimer!==null)clearInterval(refreshTimer);refreshTimer=null;refreshSeq++;refreshing=false;}
function authGate(show,setup=false){
 const gate=$('authGate');if(!gate)return;
 const opening=show&&gate.hidden;
 if(authFocusTimer!==null){clearTimeout(authFocusTimer);authFocusTimer=null}
 gate.hidden=!show;
 $('authTitle').textContent=setup?'初始化管理员账号':'登录网关压测工作台';
 $('authHint').textContent=setup?'首次使用请创建系统管理员账号，之后由管理员创建并分发使用者账号。':'请输入系统管理员分发的账号。';
 $('authConfirmField').hidden=!setup;$('authConfirm').required=setup;
 $('authSubmit').textContent=setup?'创建管理员并进入':'登录';
 // 登录界面出现后停止受保护接口轮询，并作废旧请求，避免重复抢焦点或过期响应覆盖新会话。
 document.querySelector('.app').inert=show||$('config').classList.contains('mobile-open');
 if(show){window.FingerprintUI?.onAuthLost();toggleConfig(false);stopRefresh();for(const {id} of modalStack.splice(0))$(id).hidden=true;document.body.classList.remove('modal-open')}
 // 只在首次打开时自动聚焦；用户已选中表单控件时，延迟回调不得覆盖其选择。
 if(opening)authFocusTimer=setTimeout(()=>{authFocusTimer=null;if(!gate.hidden&&!gate.contains(document.activeElement))$('authUsername')?.focus({preventScroll:true})},0);
}
function updateAccountChrome(){const name=$('accountUser');if(name)name.textContent=currentUser?`${currentUser.username} · ${currentUser.role==='admin'?'管理员':'使用者'}`:'';const manage=$('accountManage');if(manage)manage.hidden=currentUser?.role!=='admin'}
async function initAuth(){try{const d=await fetch('/auth/status',{cache:'no-store'}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||'认证状态读取失败');return j});authSetupRequired=Boolean(d.setup_required);authenticated=Boolean(d.authenticated);currentUser=d.user||null;updateAccountChrome();if(!authenticated)authGate(true,authSetupRequired);else authGate(false);return authenticated}catch(e){authGate(true,false);$('authError').textContent=e.message||'无法读取登录状态';return false}}
async function logout(){try{await api('/auth/logout',{method:'POST'});authenticated=false;currentUser=null;updateAccountChrome();authGate(true,false)}catch(e){toast(e.message)}}
$('authForm').addEventListener('submit',async e=>{e.preventDefault();const button=$('authSubmit'),error=$('authError'),setup=authSetupRequired,username=$('authUsername').value.trim(),password=$('authPassword').value,confirm=$('authConfirm').value;error.textContent='';if(setup&&password!==confirm){error.textContent='两次输入的密码不一致';return}button.disabled=true;button.textContent=setup?'创建中…':'登录中…';try{const d=await fetch(setup?'/auth/setup':'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||'登录失败');return j});authenticated=true;currentUser=d.user||null;updateAccountChrome();authGate(false);$('authPassword').value='';$('authConfirm').value='';await bootApp()}catch(err){error.textContent=err.message||'操作失败'}finally{button.disabled=false;button.textContent=setup?'创建管理员并进入':'登录'}});
async function api(url,options={}){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),Number(options.timeout||8000));const requestOptions={...options,cache:'no-store',signal:controller.signal};delete requestOptions.timeout;try{const r=await fetch(url,requestOptions);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.error||`请求失败（${r.status}）`);return d}catch(e){if(e?.name==='AbortError')throw new Error('请求超时，请检查本地服务是否仍在运行');throw e}finally{clearTimeout(timer)}}function savedConfig(){try{const value=JSON.parse(localStorage.getItem('route_ui_config')||'{}');return value&&typeof value==='object'&&!Array.isArray(value)?value:{}}catch{return{}}}function parseBool(value,fallback=false){if(typeof value==='boolean')return value;if(typeof value==='number')return value===1;if(typeof value==='string'){const v=value.trim().toLowerCase();if(['true','1','yes','on'].includes(v))return true;if(['false','0','no','off',''].includes(v))return false}return fallback}function saveConfig(cfg){const safe={...cfg};delete safe.api_key;delete safe.resources;delete safe.shared_config;localStorage.setItem('route_ui_config',JSON.stringify(safe))}
function currentModel(){const select=$('modelSelect');return select&&!select.disabled&&select.value?select.value:($('model')?.value||'').trim()}
function setManualModel(enabled, value='', focus=false){const wrap=$('modelManualWrap'),input=$('model'),select=$('modelSelect'),fallback=$('modelFallback'),toggle=$('manualModelToggle');if(!wrap||!input)return;wrap.hidden=!enabled;if(select)select.disabled=Boolean(enabled&&modelFetchState!=='loading');if(fallback)fallback.hidden=Boolean(enabled);if(toggle)toggle.setAttribute('aria-expanded',String(enabled));if(enabled&&value!=null)input.value=value;if(focus&&enabled&&document.activeElement!==input)input.focus()}
function setModelHint(text, tone=''){const hint=$('modelListHint');if(!hint)return;hint.textContent=text;hint.classList.toggle('error',tone==='error');hint.classList.toggle('ok',tone==='ok')}
function setSavedModelOption(model){const select=$('modelSelect');const value=String(model||'').trim();if(!select)return;select.innerHTML=value?`<option value="${esc(value)}">已保存模型：${esc(value)}（未验证）</option>`:'<option value="">请先填写 URL 和 API Key，再获取模型</option>';select.value=value;modelFetchState=value?'stale':'idle';modelSource=value?'saved':'none';modelList=[];setManualModel(false);setModelHint(value?'已恢复上次使用的模型；点击“刷新模型”验证并获取最新列表。':'尚未获取模型列表。')}
function fillModelOptions(models, current=''){const select=$('modelSelect');if(!select)return;const clean=[...new Set((models||[]).map(x=>String(x||'').trim()).filter(Boolean))];modelList=clean;modelFetchState=clean.length?'success':'empty';modelSource='fetched';select.innerHTML=clean.length?'<option value="">请选择模型</option>'+clean.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join(''):'<option value="">未获取到可用模型</option>';const found=clean.includes(String(current||''));select.value=found?String(current):'';$('model').value=found?'':($('model').value||'');setManualModel(false);setModelHint(clean.length?`已成功获取 ${clean.length} 个模型，请从下拉菜单选择。`:'请求已完成，但网关没有返回可用模型；如该网关不支持 /models，可手动指定。',clean.length?'ok':'error')}
function clearModelOptions(message,current=''){const select=$('modelSelect');if(!select)return;const value=String(current||'').trim();modelList=[];modelFetchState='error';modelSource=value?'stale':'none';select.innerHTML=value?`<option value="${esc(value)}">当前模型：${esc(value)}（刷新失败，未验证）</option>`:'<option value="">刷新失败，暂无可选模型</option>';select.value=value;setManualModel(false);setModelHint(`${message||'刷新模型失败'}；请修正 URL 或 API Key 后重试。${value?'当前保留的是上次模型，未代表本次获取成功。':''}`,'error')}
async function loadConfig(){let cfg=savedConfig();try{const serverCfg=(await api('/config')).config||{};cfg={...cfg,...serverCfg}}catch{};if(cfg.mode)setMode(cfg.mode);else setMode('smoke');for(const k of ['name','url','concurrency','connect_timeout','read_timeout','profile'])if($(k)&&cfg[k]!=null)$(k).value=cfg[k];applyDynamic(cfg);$('capture_raw').checked=parseBool(cfg.capture_raw,false);setSavedModelOption(String(cfg.model||''));window.savedApiKey=Boolean(cfg.api_key_saved);window.savedApiKeyHint=String(cfg.api_key_hint||'');updateKeyInputHint();loadTemplates();loadRoutingCorpus()}
async function loadRoutingCorpus(){
 $('routingCorpusRetry').disabled=true;
 try{const d=await api('/routing-corpus');routingCorpus=d.tiers||[];routingMetrics=d.metrics||[]}catch(e){routingCorpus=null}
 finally{$('routingCorpusRetry').disabled=false}
 if(isRoutingCreate()){renderRoutingFields();schedulePreflight()}
}
function readConfig(){const v=id=>$(id)?.value;return{name:v('name'),url:v('url'),api_key:v('api_key'),model:currentModel(),mode,rpm:v('rpm')||600,iterations:v('iterations')||50,ramps:v('ramps')||'',duration:v('duration')||60,concurrency:v('concurrency')||20,connect_timeout:v('connect_timeout')||10,read_timeout:v('read_timeout')||60,write_timeout:30,profile:v('profile')||'smoke',stream:true,capture_raw:$('capture_raw').checked}}
const eyeOpen='<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.2 12s3.4-5.3 9.8-5.3 9.8 5.3 9.8 5.3-3.4 5.3-9.8 5.3S2.2 12 2.2 12Z"></path><circle cx="12" cy="12" r="2.7"></circle></svg>',eyeClosed='<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18"></path><path d="M10.6 6.9A10.9 10.9 0 0 1 12 6.7c6.4 0 9.8 5.3 9.8 5.3a18.6 18.6 0 0 1-3.1 3.4M6.2 6.9C3.7 8.5 2.2 12 2.2 12s3.4 5.3 9.8 5.3c.8 0 1.5-.1 2.2-.2"></path></svg>';function resetKeyButton(hidden=true){$('toggleKey').innerHTML=hidden?eyeOpen:eyeClosed;$('toggleKey').setAttribute('aria-pressed',String(!hidden));$('toggleKey').setAttribute('aria-label',hidden?'显示 API Key（应用程序编程接口密钥）':'隐藏 API Key（应用程序编程接口密钥）');$('toggleKey').title=$('toggleKey').getAttribute('aria-label')}
function scrollKeyToEnd(){const input=$('api_key');if(input)requestAnimationFrame(()=>{input.scrollLeft=input.scrollWidth})}
function updateKeyInputHint(){const input=$('api_key'),hint=$('keyInputHint'),n=input?.value?.length||0;if(!hint)return;if(n)hint.textContent=`已输入 ${n} 个字符；启动成功后会加密保存到 macOS Keychain（钥匙串）。`;else if(window.savedApiKey)hint.textContent=`已保存密钥 ${window.savedApiKeyHint||''}；留空将直接复用，输入新值会覆盖。`;else hint.textContent='尚未保存密钥；输入后会加密保存到 macOS Keychain（钥匙串）。'}
$('toggleKey').onclick=()=>{const input=$('api_key');const visible=input.type==='text';input.type=visible?'password':'text';resetKeyButton(visible);scrollKeyToEnd()};
$('api_key').addEventListener('focus',scrollKeyToEnd);$('api_key').addEventListener('input',()=>{scrollKeyToEnd();updateKeyInputHint()});$('api_key').addEventListener('paste',()=>setTimeout(updateKeyInputHint,0));
$('copyKey').onclick=async()=>{const value=$('api_key').value;if(!value)return toast('没有可复制的 API Key（应用程序编程接口密钥）');try{await navigator.clipboard.writeText(value);$('copyKey').textContent='已复制';setTimeout(()=>{$('copyKey').textContent='复制'},1200);toast('API Key 已复制到剪贴板')}catch{toast('浏览器未允许访问剪贴板，请手动复制')}};
$('manualModelToggle').onclick=()=>setManualModel(true,'',true);
let credentialStatusSeq=0;async function checkSavedCredential(){const url=String($('url')?.value||'').trim();if(!url){window.savedApiKey=false;window.savedApiKeyHint='';updateKeyInputHint();return}const seq=++credentialStatusSeq;try{const d=await api('/credential-status?url='+encodeURIComponent(url));if(seq!==credentialStatusSeq)return;window.savedApiKey=Boolean(d.saved);window.savedApiKeyHint=String(d.hint||'');updateKeyInputHint()}catch{if(seq===credentialStatusSeq){window.savedApiKey=false;window.savedApiKeyHint='';updateKeyInputHint()}}}function invalidateModelFetch(){if(modelFetchState==='loading')modelRequestSeq++;const current=currentModel();setSavedModelOption(current);if(current)setModelHint('URL 或 API Key 已变化，当前模型尚未针对新配置验证；请重新获取。','error');$('loadModels').textContent='刷新模型';$('loadModels').classList.remove('done');const row=$('loadModels').closest('.model-row');row?.classList.remove('is-loading','is-done','is-error');if($('api_key')?.value==='')checkSavedCredential()}
$('modelSelect').onchange=()=>{if($('modelSelect').value){$('model').value='';setManualModel(false)} };
$('url').addEventListener('input',invalidateModelFetch);$('api_key').addEventListener('input',invalidateModelFetch);document.addEventListener('input',e=>{if(e.target.matches('.quality-url'))checkQualityCredential(e.target.closest('.quality-resource'))});document.addEventListener('focusout',e=>{if(e.target.matches('.quality-url'))checkQualityCredential(e.target.closest('.quality-resource'))});
$('loadModels').onclick=async()=>{const cfg=readConfig(),button=$('loadModels'),row=button.closest('.model-row'),requestId=++modelRequestSeq;if(!cfg.url||(!cfg.api_key&&!window.savedApiKey)){setModelHint('请先填写 URL；API Key 可输入新值或复用已保存密钥。','error');return toast('请先填写 URL 和 API Key')};if(button.disabled)return;modelFetchState='loading';button.disabled=true;$('modelSelect').disabled=true;row.classList.add('is-loading');row.classList.remove('is-error','is-done');button.textContent='获取中…';button.setAttribute('aria-busy','true');setModelHint('正在请求网关的模型列表，请稍候…');try{const d=await api('/models',{method:'POST',timeout:35000,headers:{'Content-Type':'application/json'},body:JSON.stringify({url:cfg.url,api_key:cfg.api_key})});if(requestId===modelRequestSeq){fillModelOptions(d.models,cfg.model);if(d.saved){window.savedApiKey=true;window.savedApiKeyHint=String(d.hint||'');updateKeyInputHint()}row.classList.add(d.models?.length?'is-done':'is-error');button.textContent=d.models?.length?`已获取 ${d.models.length} 个`:'重新获取';toast(d.models?.length?`已获取 ${d.models.length} 个模型，请从下拉菜单选择`:'请求成功但没有返回可用模型，请检查网关的 /models 接口');}}catch(e){if(requestId===modelRequestSeq){clearModelOptions(e.message,cfg.model);row.classList.add('is-error');button.textContent='重新获取';toast(e.message)}}finally{button.disabled=false;button.removeAttribute('aria-busy');if(requestId===modelRequestSeq){$('modelSelect').disabled=false;setTimeout(()=>{row.classList.remove('is-loading','is-done','is-error');if(modelFetchState==='success')button.textContent='刷新模型'},1800)}else{row.classList.remove('is-loading')}}};
$('submit').onclick=async()=>{const button=$('submit'),status=$('formStatus');if(button.disabled)return;button.disabled=true;status.className='form-status';status.textContent='正在校验配置并启动测试…';try{if(isRoutingCreate())validateRoutingForm();const body=isRoutingCreate()?readRouting():isQualityCreate()?readQuality():isCompareCreate()?readComparison():readConfig();const endpoint=isRoutingCreate()?'/routing-comparisons':isQualityCreate()?'/quality-tests':isCompareCreate()?'/comparisons':'/tasks';if(endpoint!=='/comparisons'){await refreshPreflight();if(!(await ensurePreflightAck())){status.className='form-status';status.textContent='已取消：未确认在疑似生产环境中执行。';button.disabled=false;return}if(preflightData&&preflightData.requires_confirmation)body.risk_acknowledged=true}const d=await api(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(createKind==='gateway-single')saveConfig(body.shared_config||body);$('api_key').value='';$('api_key').type='password';resetKeyButton(true);updateKeyInputHint();document.querySelectorAll('.quality-key,.routing-side-key').forEach(input=>{input.value='';input.type='password'});const id=d.task_id||d.comparison_id||d.quality_test_id||d.routing_comparison_id;status.className='form-status ok';status.textContent=isRoutingCreate()?'路由对照已创建：两侧将按同一份清单交错发出，完成后可查看命中率等指标的差异。':isQualityCreate()?(isCompareCreate()?'资源对照已创建，正在执行同一题集。':'评测已创建，正在执行题集。'):(isCompareCreate()?'压测对照实验已创建，实验组将按顺序执行。':'压测任务已创建，已加入执行队列。');toast(status.textContent);toggleConfig(false);selectDefaultModuleAfterCreate(createKind,id);await refresh()}catch(e){status.className='form-status error';status.textContent=e.message;toast(e.message)}finally{button.disabled=false;setCreateKind(createKind,false)}};
function startNewTest(module=activeModule,view=activeView){
 activeModule=['gateway','quality','routing'].includes(module)?module:'gateway';activeView=view==='compare'?'compare':'single';
 const modelValue=currentModel(),keep={url:$('url').value,concurrency:$('concurrency').value,connect_timeout:$('connect_timeout').value,read_timeout:$('read_timeout').value};
 resetCreateForm();
 for(const [key,value] of Object.entries(keep))if($(key)&&value)$(key).value=value;
 setSavedModelOption(modelValue);
 if(isRoutingCreate()){$('routingName').value='';$('routingModel').value='';$('routingConcurrency').value='20';$('routingRpm').value='600';$('routingConnectTimeout').value='10';$('routingReadTimeout').value='60';$('routingCapture').checked=false}
 $('formStatus').className='form-status';$('formStatus').textContent='';
 toggleConfig(true);schedulePreflight();
}
function focusConfig(){openCreateForContext()}
let drawerOpener=null;
function toggleConfig(force){
 const panel=$('config'),open=force===undefined?!panel.classList.contains('mobile-open'):Boolean(force);
 if(open&&!panel.classList.contains('mobile-open'))drawerOpener=document.activeElement;
 panel.classList.toggle('mobile-open',open);panel.inert=!open;panel.setAttribute('aria-hidden',String(!open));
 $('drawerBackdrop').hidden=!open;document.body.classList.toggle('drawer-open',open);
 document.querySelector('.app').inert=open||!$('authGate').hidden;
 if(open){panel.querySelector('.drawer-body').scrollTop=0;$(isRoutingCreate()?'routingName':isQualityCreate()?'qualityName':isCompareCreate()?'comparisonName':'name')?.focus({preventScroll:true})}
 else{clearTimeout(preflightTimer);preflightSeq++;if(drawerOpener?.isConnected&&$('authGate').hidden)drawerOpener.focus({preventScroll:true})}
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('config')?.classList.contains('mobile-open')&&['qualitySuiteModal','qualityEvidenceModal','qualityMethodologyModal','rawModal','accountModal','storageModal','diagnosticsModal'].every(id=>$(id)?.hidden!==false))toggleConfig(false)})
document.addEventListener('input',e=>{if(e.target.closest('#routingSidePanels'))updateRoutingSideStatus()});
document.addEventListener('keydown',e=>{
 const routeTab=e.target.closest?.('#routingSideTabs [role="tab"]');
 if(routeTab&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?'baseline':e.key==='End'?'candidate':routeTab.dataset.routingTab==='baseline'?'candidate':'baseline';setRoutingSide(next);document.querySelector(`#routingSideTabs [data-routing-tab="${next}"]`).focus();return}
 if(e.key!=='Tab'||modalStack.length||!$('authGate').hidden||!$('config').classList.contains('mobile-open'))return;
 const nodes=[...$('config').querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),summary,a[href],[tabindex="0"]')].filter(x=>x.getClientRects().length&&x.tabIndex>=0),first=nodes[0],last=nodes[nodes.length-1];
 if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}
});
// 预检卡片跟随创建抽屉里的草稿：任何输入/选择变化后延迟重算，用户改了目标地址或题量立刻看到风险变化。
document.addEventListener('input',e=>{if($('config')?.contains(e.target)&&!e.target.closest('.quality-suite-picker'))schedulePreflight()});
document.addEventListener('change',e=>{if($('config')?.contains(e.target))schedulePreflight()});
function errorItems(d){const c=d.counters||{};return[['r429','限流 429'],['r5xx','服务端 5xx'],['r4xx','客户端 4xx'],['timeout','请求超时'],['network','网络错误'],['protocol','响应不完整']].map(([k,label])=>`<div class=\"error-item\"><b>${num(c[k],0)}</b><span>${label}</span></div>`).join('')}
function capacityNote(d){const target=Number(d.target_rpm)||0,estimate=Number(d.rpm?.capacity_estimate)||0;if(!target||!estimate)return '';const ratio=estimate/target;if(ratio<1)return `<div class=\"capacity-note warn\">按已完成请求的平均耗时估算，当前并发最多约 ${num(estimate)} RPM；低于计划调度 ${num(target)} RPM，完成吞吐不会达到计划值。</div>`;return `<div class=\"capacity-note\">按已完成请求的平均耗时估算，当前并发能力约 ${num(estimate)} RPM；计划调度 ${num(target)} RPM 在当前观测下可被追上。</div>`}
function taskItem(t){const pct=Math.round((t.progress||0)*100);return `<button class="task ${t.id===selected?'sel':''}" onclick="selectTask('${t.id}')"><div class="task-top"><div class="task-name">${esc(t.name)}</div><span class="status ${t.status}">${statusNames[t.status]||t.status}</span></div><div class="task-meta"><span class="record-type">单次压测</span> · ${esc(t.mode||'')} · ${num(t.concurrency)} 并发 · ${num(t.target_rpm)} RPM</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="task-pct">${pct}%</span></div></button>`}
function comparisonItem(c){const groupList=c.groups||[],groupTotal=groupList.length,groupDone=groupList.filter(g=>['finished','finished_with_errors'].includes(g.status)).length,groupProgress=groupList.reduce((sum,g)=>sum+Math.max(0,Math.min(1,Number(g.progress)||0)),0);const pct=c.status==='finished'||c.status==='finished_with_errors'?100:groupTotal?Math.round(groupProgress/groupTotal*100):c.status==='running'?1:0;const report=c.report||{},conclusion=report.conclusion||{};const done=(c.groups||[]).filter(g=>['finished','finished_with_errors'].includes(g.status)).length;const total=(c.groups||[]).length;const label=c.status==='finished'?'已完成':c.status==='finished_with_errors'?'有错误':c.status==='running'?'执行中':'准备中';return `<button class="task comparison-record ${c.id===selectedComparison?'sel':''}" onclick="selectComparison('${c.id}')"><div class="task-top"><div class="task-name">${esc(c.name)}</div><span class="status ${c.status}">${label}</span></div><div class="task-meta"><span class="record-type comparison-type">对照实验</span> · ${done}/${total||'—'} 组完成 · ${esc(report.evidence?.grade||'等待证据')}证据</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="task-pct">${conclusion.level?reportTag(conclusion.level):`${done}/${total||'—'}`}</span></div></button>`}
function qualityItem(q){const {pct}=qualityProgress(q);const compared=(q.resources||[]).length>1;return `<button class="task ${q.id===selectedQuality?'sel':''}" onclick="selectQuality('${q.id}')"><div class="task-top"><div class="task-name">${esc(q.name)}</div><span class="status ${q.status}">${qualityStatusLabel(q.status)}</span></div><div class="task-meta"><span class="record-type ${compared?'comparison-type':''}">${compared?'资源对照':'单资源测试'}</span> · ${q.case_count||0} 道题 · ${q.resources?.length||0} 个资源</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="task-pct">${q.status==='finished'?'完成':q.status==='finished_with_errors'?'有错误':pct+'%'}</span></div></button>`}
function listEmpty(title,hint,action){const label=activeModule==='quality'?(activeView==='compare'?'新建资源对照':'新建单资源测试'):(activeView==='compare'?'新建压测对照':'新建压测');return `<div class="task-empty"><b>${title}</b><span>${hint}</span></div>`}
function updateTaskList(){const list=$('gatewayList'),records=filteredGatewayRecords();list.innerHTML=records.length?records.map(x=>x.record_type==='compare'?comparisonItem(x):taskItem(x)).join(''):listEmpty(activeView==='compare'?'暂无对照实验记录':'暂无单次压测记录',activeView==='compare'?'对照实验是一条记录，进入后用横向标签切换基准组、对比组和结论报告。':'创建单次网关压测后，结果会保留在这里。',activeView==='compare'?"openGatewayCreate('compare')":"openGatewayCreate('single')")}
function updateComparisonList(){updateTaskList()}
function updateQualityList(){const list=$('qualityList'),filtered=filteredQualityRecords();list.innerHTML=filtered.length?filtered.map(qualityItem).join(''):listEmpty(activeView==='compare'?'暂无资源对照记录':'暂无单资源测试记录',activeView==='compare'?'创建包含资源 A 与资源 B 的质量测试；它仍然是一条实验记录。':'创建单资源质量测试后，结果会保留在这里。',activeView==='compare'?"openQualityCreate('compare')":"openQualityCreate('single')")}
function renderWorkspaceEmpty(title,hint,action){$('loadingState').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;$('empty').hidden=false;$('emptyTitle').textContent=title;$('emptyHint').textContent=hint;const button=$('empty').querySelector('.empty-action');if(button)button.setAttribute('onclick',action)}
function showEmptyForWorkspace(){const quality=activeModule==='quality',compare=activeView==='compare';renderWorkspaceEmpty(quality?(compare?'当前暂无资源对照记录':'当前暂无单资源测试记录'):(compare?'当前暂无对照实验记录':'当前暂无单次压测记录'),quality?(compare?'创建资源 A 与资源 B 的对照评测后，结果会出现在评测记录中。':'创建单资源评测后，结果会出现在评测记录中。'):(compare?'创建一个对照实验后，结果会出现在压测记录中。':'创建一次网关压测后，结果会出现在压测记录中。'),quality?(compare?"openQualityCreate('compare')":"openQualityCreate('single')"):(compare?"openGatewayCreate('compare')":"openGatewayCreate('single')"))}
function formatAccountDate(value){if(!value)return '—';try{return new Date(Number(value)*1000).toLocaleString('zh-CN',{dateStyle:'medium',timeStyle:'short'})}catch{return '—'}}
async function openAccountModal(){if(currentUser?.role!=='admin')return;const modal=$('accountModal');openModal('accountModal');$('accountStatus').textContent='';try{const d=await api('/admin/accounts');$('accountList').innerHTML=(d.accounts||[]).map(a=>`<div class="account-row"><b>${esc(a.username)}</b><span class="role-badge ${a.role==='admin'?'admin':''}">${a.role==='admin'?'管理员':'使用者'}</span><span class="created-at">${formatAccountDate(a.created_at)}</span><button class="account-delete" type="button" onclick="deleteAccount('${esc(a.username)}')" ${a.username===currentUser.username?'disabled':''}>删除</button></div>`).join('')||'<div class="field-hint">暂无账号。</div>'}catch(e){$('accountList').innerHTML=`<div class="auth-error">${esc(e.message)}</div>`}}
function closeAccountModal(){closeModal('accountModal')}
async function deleteAccount(username){if(!confirm(`确认删除账号“${username}”？该账号将立即无法登录。`))return;try{await api('/admin/accounts?delete='+encodeURIComponent(username),{method:'POST'});toast('账号已删除');openAccountModal()}catch(e){toast(e.message)}}
$('accountCreateForm').addEventListener('submit',async e=>{e.preventDefault();const status=$('accountStatus'),button=e.target.querySelector('button[type="submit"]');button.disabled=true;status.className='form-status';status.textContent='正在创建账号…';try{const d=await api('/admin/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('newUsername').value.trim(),password:$('newPassword').value,role:$('newRole').value})});status.className='form-status ok';status.textContent=`已创建账号 ${d.account?.username||''}，请安全分发初始密码。`;$('newUsername').value='';$('newPassword').value='';openAccountModal()}catch(err){status.className='form-status error';status.textContent=err.message}finally{button.disabled=false}});
function showLoading(){$('loadingState').hidden=false;$('empty').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true}
function showEmpty(){showEmptyForWorkspace()}
async function refresh(){if(!authenticated||refreshing)return;refreshing=true;const seq=++refreshSeq;try{const d=await api('/tasks');if(seq!==refreshSeq)return;tasks=d.tasks||[];comparisons=d.comparisons||[];qualityTests=d.quality_tests||[];routingComparisons=d.routing_comparisons||[];window.FingerprintUI?.setRuns(d.fingerprint_runs||[],d.fingerprint_storage_warnings||[]);$('health').classList.remove('conn-bad');$('healthText').textContent='服务正常';try{renderWorkspace()}catch(renderError){console.error('[render] failed',renderError);$('loadingState').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;$('empty').hidden=false;$('emptyTitle').textContent='详情显示异常';$('emptyHint').textContent='服务数据已读取，但页面渲染失败：'+renderError.message}}catch(e){if(seq!==refreshSeq)return;if(/请先登录|401|登录/.test(e.message||'')){authenticated=false;currentUser=null;updateAccountChrome();authGate(true,false);return}console.error('[refresh] failed',e);if(seq===refreshSeq){$('health').classList.add('conn-bad');$('healthText').textContent='连接失败';$('loadingState').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;$('empty').hidden=false;$('emptyTitle').textContent='测试记录加载失败';$('emptyHint').textContent=e.message||'无法读取测试记录，请确认本地服务已启动后重试。'}}finally{if(seq===refreshSeq)refreshing=false}}
function selectTask(id){selected=id;detailTask=null;selectedComparison=null;selectedQuality=null;activeModule='gateway';localStorage.setItem('route_selected_task',id);detailState.id=null;toggleConfig(false);updateWorkspaceChrome();renderWorkspace();refresh()}
async function openGroupTask(id){try{detailTask=await api('/task/'+id);selected=null;selectedComparison=null;selectedQuality=null;activeModule='gateway';detailState.id=null;toggleConfig(false);updateWorkspaceChrome();renderWorkspace()}catch(e){toast(e.message||'无法读取实验组明细')}}
function render(d){window.currentDetail=d;$('loadingState').hidden=true;$('empty').hidden=true;$('modulePlaceholder').hidden=true;$('detail').hidden=false;const detail=$('detail');const same=detailState.id===d.id;const table=detail.querySelector('.table-wrap');const tableScroll=table?.scrollTop||0;const main=document.querySelector('.main');const pageScroll=main.scrollTop;const pct=d.success_pct==null?'—':num(d.success_pct);const progress=Math.min(100,Math.max(0,(d.progress||0)*100));const actions=`${d.status==='running'||d.status==='waiting'?`<button class="btn danger" onclick="stopTask('${d.id}')">停止</button>`:''}${['finished','stopped','error'].includes(d.status)?`<button class="btn" onclick="retryTask('${d.id}')">重新测试</button><button class="btn" onclick="deleteTask('${d.id}')">删除</button>`:''}${d.out_path?`<button class="btn" onclick="location.href='/download/${d.id}'">下载 JSONL（逐行 JSON）</button>`:''}`;const actualLabel=d.status==='running'?'实时完成吞吐':'平均完成吞吐';const actualValue=d.status==='running'?d.rpm?.live:d.rpm?.avg;
detail.innerHTML=`<section class="overview-shell"><div class="hero"><div class="hero-main"><div class="eyebrow">${esc(statusNames[d.status]||d.status)} · ${esc(d.mode)}</div><div class="title-row"><h1>${esc(d.name)}</h1><span class="status ${d.status}">${esc(d.phase)}</span></div><div class="phase">${esc(d.model||'—')} · ${esc(d.target_url||'—')} · 实际运行 <span data-elapsed>${duration(d.elapsed)}</span>${d.planned_duration?` · 计划调度 ${durationPrecise(d.planned_duration)}`:''}</div></div><div class="hero-actions">${actions}</div></div>${artifactNotice(d)}${d.error?`<div class="notice">${esc(d.error)}</div>`:''}<section class="progress-card ${d.status}"><div class="progress-head"><div class="progress-copy"><strong>任务完成进度</strong><div>已收到 ${num(d.done,0)} 条结果 / 计划 ${num(d.total,0)} 条请求</div></div><div class="progress-num">${Math.round(progress)}<small>%</small></div></div><div class="track"><div class="fill" style="width:${progress}%"></div></div><div class="progress-foot"><span>已调度 ${num(d.scheduled,0)} · 已开始 ${num(d.started,0)} · 进行中 ${num(d.inflight,0)}</span><span>${d.status==='running'||d.status==='waiting'?`排队 ${num(d.pending,0)} · 计划剩余 ${num(d.remaining,0)}`:d.status==='finished'?'计划请求已全部完成':`未执行/未完成 ${num(d.dropped,0)}`}</span></div>${capacityNote(d)}</section><div class="summary"><section class="success-card"><div class="success-label">请求成功率</div><div class="success-number">${pct}<span>${d.success_pct==null?'':'%'}</span></div><div class="success-note">成功 ${num(d.counters?.ok,0)} / 已收到结果 ${num(d.done,0)}。仅完整的 2xx 响应计为成功。</div></section><div class="metric-grid"><div class="metric"><div class="metric-label">${actualLabel}</div><div class="metric-value">${num(actualValue)}<small>RPM</small></div></div><div class="metric"><div class="metric-label">计划调度速率</div><div class="metric-value">${num(d.target_rpm)}<small>RPM</small></div></div><div class="metric"><div class="metric-label">TTFT p95（首字耗时）</div><div class="metric-value">${num(d.ttft?.p95)}<small>ms</small></div></div><div class="metric"><div class="metric-label">调度延迟 p95</div><div class="metric-value">${num(d.schedule_lag?.p95)}<small>ms</small></div></div></div></div></section><div class="cards"><section class="card"><div class="card-head"><div><div class="card-title">失败分布</div><div class="card-sub">所有类别均来自客户端可观测结果</div></div></div><div class="error-grid">${errorItems(d)}</div></section><section class="card"><div class="card-head"><div><div class="card-title">延迟概览</div><div class="card-sub">真实正文首字与完整响应耗时</div></div></div><div class="metric-grid"><div class="metric"><div class="metric-label">TTFT p50</div><div class="metric-value">${num(d.ttft?.p50)}<small>ms</small></div></div><div class="metric"><div class="metric-label">TTFT p95</div><div class="metric-value">${num(d.ttft?.p95)}<small>ms</small></div></div><div class="metric"><div class="metric-label">总耗时 p50</div><div class="metric-value">${num(d.total_ms?.p50)}<small>ms</small></div></div><div class="metric"><div class="metric-label">总耗时 p95</div><div class="metric-value">${num(d.total_ms?.p95)}<small>ms</small></div></div></div></section><section class="card wide"><div class="card-head"><div><div class="card-title">完成吞吐趋势</div><div class="card-sub">计划调度速率与实际完成速率是两个不同口径</div></div><div class="chart-legend"><span><i style="background:#3975ef"></i>完成 RPM</span><span><i style="background:#9cb8e8"></i>计划调度 RPM</span><span><i style="background:#df5b63"></i>失败 RPM</span></div></div><div class="chart-wrap"><canvas id="chart"></canvas>${d.timeline?.length?'':'<div class="chart-empty">等待更多数据后绘制趋势</div>'}</div></section><section class="card wide"><div class="card-head"><div><div class="card-title">最近请求</div><div class="card-sub">页面显示最近 100 条；下载文件包含全部已完成请求${d.config?.capture_raw?'及原始输入/输出':''}</div></div><div class="path" title="${esc(d.out_path||'')}">${esc(d.out_path||'')}</div></div><div class="table-wrap"><table><thead><tr><th>ID</th><th>档位</th><th>状态码</th><th>结果</th><th>TTFT</th><th>总耗时</th><th>原因</th>${d.config?.capture_raw?'<th>原始记录</th>':''}</tr></thead><tbody>${(d.recent||[]).length?d.recent.map(r=>`<tr><td>${num(r.id,0)}</td><td>${num(r.step,0)}</td><td>${num(r.status,0)}</td><td><span class="tag ${r.cls}">${esc(r.label)}</span></td><td>${num(r.ttft)} ms</td><td>${num(r.total)} ms</td><td title="${esc(r.reason||'')}">${esc(r.reason||'—')}</td>${d.config?.capture_raw?`<td><button class="raw-link" onclick="showRaw('${d.id}',${Number(r.id)})">查看</button></td>`:''}</tr>`).join(''):`<tr><td colspan="${d.config?.capture_raw?8:7}">尚无请求结果</td></tr>`}</tbody></table></div></section></div>`;detailState.id=d.id;requestAnimationFrame(()=>{main.scrollTop=same?pageScroll:0;const next=detail.querySelector('.table-wrap');if(next)next.scrollTop=same?tableScroll:0;drawChart(d.timeline||[]);const chartWrap=detail.querySelector('.chart-wrap');if(chartWrap&&window.chartResizeObserver){window.chartResizeObserver.disconnect();window.chartResizeObserver.observe(chartWrap)}})}
async function showRaw(taskId,itemId){const modal=$('rawModal');openModal('rawModal');$('rawTitle').textContent=`请求 #${itemId} 原始输入与输出`;$('rawInput').textContent='加载中…';$('rawOutput').textContent='加载中…';try{const d=await api(`/task/${taskId}/record/${itemId}`);$('rawInput').textContent=JSON.stringify(d.raw_input??null,null,2);$('rawOutput').textContent=d.raw_output||'（没有响应正文，例如连接失败或超时）'}catch(e){$('rawInput').textContent='读取失败';$('rawOutput').textContent=e.message}}
function closeRaw(){closeModal('rawModal')}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeRaw();closeQualityEvidence();closeQualitySuite()}const tab=e.target.closest?.('.report-tab');if(!tab||!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;const tabs=[...tab.parentElement.querySelectorAll('.report-tab')];const index=tabs.indexOf(tab);const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;e.preventDefault();tabs[next].click();document.querySelectorAll('.report-tab')[next]?.focus()});
function drawChart(data){const c=$('chart');if(!c)return;const wrap=c.parentElement;if(!wrap)return;const rect=wrap.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(80,Math.floor(rect.width)),h=Math.max(150,Math.floor(rect.height));c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr);c.style.width='100%';c.style.height='100%';const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);if(!data.length)return;const points=data.map(v=>({...v,rpm:Number(v.rpm)||0,target_rpm:Number(v.target_rpm)||0,failed:(Number(v.r429)||0)+(Number(v.r5xx)||0)+(Number(v.r4xx)||0)+(Number(v.timeout)||0)+(Number(v.network)||0)+(Number(v.protocol)||0)}));const max=Math.max(60,...points.flatMap(v=>[v.rpm,v.target_rpm,v.failed*60]));x.strokeStyle='#e3e8ef';x.fillStyle='#8290a3';x.font='10px sans-serif';for(let i=0;i<=4;i++){const y=12+(h-40)*i/4;x.beginPath();x.moveTo(45,y);x.lineTo(w-14,y);x.stroke();x.fillText(Math.round(max*(1-i/4)),4,y+3)}const px=i=>45+(w-59)*(points.length===1?.5:i/(points.length-1)),py=v=>h-28-(h-40)*(v/max);function line(key,color,width=2,dash=[]){x.strokeStyle=color;x.lineWidth=width;x.setLineDash(dash);x.beginPath();points.forEach((v,i)=>i?x.lineTo(px(i),py(v[key])):x.moveTo(px(i),py(v[key])));x.stroke();x.setLineDash([]);if(points.length===1){x.fillStyle=color;x.beginPath();x.arc(px(0),py(points[0][key]),Math.max(3,width+1),0,Math.PI*2);x.fill()}}line('target_rpm','#9cb8e8',1.5,[5,5]);line('rpm','#3975ef',2.6);line('failed','#df5b63',1.5);x.fillStyle='#8290a3';x.fillText('开始',45,h-8);x.fillText(`${points.length-1} 秒`,Math.max(45,w-52),h-8)}
async function stopTask(id){if(!confirm('停止后将取消尚未开始的排队请求；已经开始的请求会继续完成并写入结果。是否继续？'))return;try{await api('/task/'+id+'?stop',{method:'POST'});toast('正在停止任务');refresh()}catch(e){toast(e.message)}}async function deleteTask(id){if(!confirm('确认删除任务？任务记录和它的结果文件会一起删除，删除后无法恢复，也无法再下载这次的结果。'))return;try{await api('/task/'+id+'?delete',{method:'POST'});showEmpty();refresh()}catch(e){toast(e.message)}}async function deleteComparison(id){if(!confirm('确认删除这条对照实验记录？实验记录和它各实验组的结果文件会一起删除，删除后无法恢复。'))return;try{await api('/comparisons?delete='+encodeURIComponent(id),{method:'POST'});selectedComparison=null;toast('对照实验记录已删除');refresh()}catch(e){toast(e.message)}}async function deleteQuality(id){if(!confirm('确认删除这条质量评测记录？评测记录和它的证据文件会一起删除，删除后无法恢复，报告也将无法复现。'))return;try{await api('/quality-tests?delete='+encodeURIComponent(id),{method:'POST'});selectedQuality=null;toast('质量评测记录已删除');refresh()}catch(e){toast(e.message)}}async function retryTask(id){const key=window.savedApiKey?'':prompt('没有已保存的 API Key，请输入密钥。');if(!window.savedApiKey&&!key)return;try{const d=await api('/task/'+id+'?retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})});selected=d.task_id;detailState.id=null;toast('已创建重测任务');refresh()}catch(e){toast(e.message)}}
Object.assign(window,{renderQualityDetail,renderComparisonDetail,render,selectQuality,selectComparison,refresh,deleteComparison,deleteQuality,openAccountModal,closeAccountModal,logout});
if(window.ResizeObserver){window.chartResizeObserver=new ResizeObserver(()=>{const d=tasks.find(x=>x.id===selected);if(d)drawChart(d.timeline||[])})}document.querySelector('.app')?.scrollTo(0,0);window.scrollTo(0,0);showLoading();async function bootApp(){stopRefresh();if(!authenticated)return;try{await loadConfig()}catch(e){console.error('[init] config failed',e)}if(!authenticated)return;await refresh();if(authenticated&&$('authGate').hidden)refreshTimer=setInterval(refresh,1500)};window.FingerprintUI?.init();(async function boot(){const ok=await initAuth();if(ok)await bootApp()})();setInterval(()=>{const d=window.currentDetail;if(d&&(['running','waiting'].includes(d.status))){const el=d.elapsed+(Date.now()-window.detailReceivedAt)/1000;document.querySelectorAll('[data-elapsed]').forEach(n=>n.textContent=duration(el))}document.querySelectorAll('[data-live-elapsed]').forEach(n=>{const task=tasks.find(t=>t.id===selected);if(task&&(['running','waiting'].includes(task.status)))n.textContent=duration(Number(n.dataset.base||0)+(Date.now()-Number(n.dataset.stamp||Date.now()))/1000)})},250);const oldRender=render;render=function(d){window.detailReceivedAt=Date.now();oldRender(d)};window.addEventListener('resize',()=>{const d=tasks.find(x=>x.id===selected);if(d)drawChart(d.timeline||[])})
</script></body></html>'''


if __name__ == "__main__":
    main()
