"""运维基础件：并发闸门、结构化日志、审计日志。

为什么单独成模块：
- 压测工具本身会往被测服务打真实流量，「同时跑几个任务」必须是可配置、可观测的，
  不能靠“谁先提交谁先跑”隐式决定。
- 出问题时需要能回答“哪个请求、哪个任务、什么时刻、发生了什么”，
  所以日志按行写 JSON，每条带 request_id / task_id。
- 账号、清理、删除这类不可逆操作必须留痕，且留痕本身不能拖慢请求。

本模块不依赖 Web 层和任务调度，可单独做单元测试。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

# ---------------------------------------------------------------------------
# 并发闸门
# ---------------------------------------------------------------------------


class ConcurrencyLimiter:
    """全局在跑任务数上限。

    旧实现用 `current_id` 加队列，等价于「全局并发=1」：提交两个压测只能排队，
    而排队本身在长时间压测下会让第二个任务的启动时间完全不可预期。
    这里把它换成显式上限：默认仍然保守（1），但管理员可以调到 N。

    计数只统计「已经占住执行位」的任务；停止中的任务立刻释放名额，
    否则一个正在收尾的长任务会长时间堵住队列。
    """

    def __init__(self, limit=1):
        self._lock = threading.RLock()
        self._limit = max(1, int(limit or 1))
        self._active = {}  # task_id -> 占位时刻
        self._exclusive = None

    def limit(self):
        with self._lock:
            return self._limit

    def set_limit(self, limit):
        with self._lock:
            self._limit = max(1, int(limit or 1))
            return self._limit

    def acquire(self, task_id):
        """尝试为 task_id 占一个执行位。返回 True 表示可以立即执行。"""
        with self._lock:
            if self._exclusive is not None and self._exclusive != task_id:
                return False
            if task_id in self._active:
                return True
            if len(self._active) >= self._limit:
                return False
            self._active[task_id] = time.time()
            return True

    def acquire_exclusive(self, task_id):
        """Reserve the host until drain completes, independent of the task limit."""
        with self._lock:
            if self._active.keys() - {task_id} or self._exclusive not in (None, task_id):
                return False
            self._exclusive = task_id
            self._active[task_id] = time.time()
            return True

    def release(self, task_id):
        with self._lock:
            if self._exclusive == task_id:
                self._exclusive = None
            return self._active.pop(task_id, None) is not None

    def active(self):
        with self._lock:
            return sorted(self._active)

    def snapshot(self):
        with self._lock:
            return {"limit": self._limit, "active": len(self._active), "active_ids": sorted(self._active),
                    "exclusive_id": self._exclusive}


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

LOG_FILE = "events.jsonl"
# 日志行不落盘正文：只记录“发生了什么”，正文仍由结果文件承载。
_REDACT_KEYS = {"api_key", "apikey", "authorization", "raw_input", "raw_output", "response",
                "password", "passwd", "secret", "token", "access_token", "x_api_key"}


def _is_redacted(name):
    """字段名归一化后再判断，避免 `Authorization` / `x-api-key` 这类写法绕过脱敏。"""
    normalized = str(name).strip().lower().replace("-", "_")
    return normalized in _REDACT_KEYS or normalized.endswith("_token")


def _clean(value, depth=0):
    if depth > 4:
        return "[深度截断]"
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            clean[key] = "[已隐藏]" if _is_redacted(key) else _clean(item, depth + 1)
        return clean
    if isinstance(value, list):
        return [_clean(item, depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 500:
            return value[:500] + "…"
        return value
    return str(value)


class EventLog:
    """按行写 JSON 的事件日志。写失败只记一次警告，绝不打断压测。"""

    def __init__(self, path=None, *, max_bytes=32 * 1024 * 1024):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILE)
        self.lock = threading.Lock()
        self.max_bytes = max_bytes
        self._warned = False
        self._counts = {}

    def log(self, event, *, level="info", **fields):
        record = {"ts": time.time(), "level": level, "event": str(event)}
        # 整包交给 _clean：它按「字段名」判断敏感，逐个处理 value 会让顶层字段名
        # （例如 task.started 里误带的 api_key）绕过脱敏。
        # 注意循环里不要用 `key` 作变量名，那会遮蔽 record 的 "event"。
        record.update(_clean(dict(fields)))
        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            self._counts[record["event"]] = self._counts.get(record["event"], 0) + 1
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # 磁盘写不了不是压测失败的理由；只在第一次提醒，避免刷屏。
                self._warned = True
        return record

    def _rotate_if_needed(self):
        # 单文件超过上限就滚动一次；只保留上一份，避免长期运行吃满磁盘。
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def tail(self, limit=200):
        """读取最近的事件，供故障排查；只读，不修改文件。"""
        limit = max(1, min(2000, int(limit or 200)))
        try:
            with open(self.path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def stats(self):
        with self.lock:
            return {"path": os.path.basename(self.path), "by_event": dict(self._counts), "degraded": self._warned}


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

AUDIT_FILE = "audit.db"

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    detail TEXT,
    request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action);
"""


class AuditLog:
    """登录、删除、清理、账号管理等不可逆操作的留痕。

    以前这些都只写进普通日志，和业务事件混在一起，事后无法只查“谁删了什么”。
    改用 SQLite 单表，写入走事务 + WAL，读写在锁内串行，避免多线程写坏文件。

    审计失败绝不阻断主流程：拿不到审计记录，也好过用户操作直接报 500。
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), AUDIT_FILE)
        self.lock = threading.RLock()
        self._ready = False

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure(self, conn):
        if not self._ready:
            conn.executescript(AUDIT_SCHEMA)
            self._ready = True

    def record(self, action, *, actor=None, target=None, ok=True, detail=None, request_id=None):
        with self.lock:
            try:
                conn = self._connect()
            except sqlite3.Error:
                return None
            try:
                self._ensure(conn)
                cursor = conn.execute(
                    "INSERT INTO audit (ts, actor, action, target, ok, detail, request_id) VALUES (?,?,?,?,?,?,?)",
                    (time.time(), actor, str(action), target, 1 if ok else 0,
                     _clean(detail) if not isinstance(detail, str) else detail[:500], request_id),
                )
                conn.commit()
                return cursor.lastrowid
            except sqlite3.Error:
                return None
            finally:
                conn.close()

    def recent(self, limit=100):
        # limit=0 的语义是「至少给我一条」，而不是「用默认值」——这里显式区分 None 和 0。
        try:
            capped = max(1, min(1000, int(100 if limit is None else limit)))
        except (TypeError, ValueError):
            capped = 100
        with self.lock:
            try:
                conn = self._connect()
            except sqlite3.Error:
                return []
            try:
                self._ensure(conn)
                rows = conn.execute(
                    "SELECT ts, actor, action, target, ok, detail, request_id FROM audit ORDER BY id DESC LIMIT ?",
                    (capped,),
                ).fetchall()
            except sqlite3.Error:
                return []
            finally:
                conn.close()
        return [
            {"ts": row[0], "actor": row[1], "action": row[2], "target": row[3],
             "ok": bool(row[4]), "detail": row[5], "request_id": row[6]}
            for row in rows
        ]

    def count(self):
        with self.lock:
            try:
                conn = self._connect()
            except sqlite3.Error:
                return 0
            try:
                self._ensure(conn)
                return int(conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
            except sqlite3.Error:
                return 0
            finally:
                conn.close()


def new_request_id():
    return uuid.uuid4().hex[:12]
