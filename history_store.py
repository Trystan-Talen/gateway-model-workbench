"""Transactional per-record index with legacy JSON import; immutable evidence stays outside."""
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

_LOCK = threading.RLock()
KINDS = ('tasks', 'comparisons', 'quality_tests', 'routing_comparisons')
# 已提交内容的摘要，按数据库路径缓存：运行中每秒写一次索引时，只比较摘要、只写变化的记录，
# 不再把全部快照正文从数据库读回来逐条对比。只在事务提交成功后更新。
_COMMITTED = {}


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _digest(encoded):
    return hashlib.sha256(encoded.encode('utf-8')).digest()


def summary(snapshot):
    keys = ('id', 'name', 'status', 'created', 'started_at', 'finished_at', 'done', 'total', 'progress', 'model', 'target_url', 'case_count', 'completed_cases', 'planned_cases', 'success_pct', 'out_path')
    return {key: snapshot[key] for key in keys if key in snapshot}


def quality_index(snapshot):
    # Frozen config is necessary for accurate restart. Per-case evidence and report
    # are restored from quality_run_<id>.json, never replicated in the index.
    clean = {k: v for k, v in snapshot.items() if k not in ('resources', 'report')}
    clean['resources'] = [{k: v for k, v in resource.items() if k not in ('case_results', 'request_errors')}
                          for resource in snapshot.get('resources', [])]
    return clean


class HistoryStore:
    def __init__(self, legacy_path):
        self.legacy = Path(legacy_path)
        self.path = self.legacy.with_suffix('.sqlite3')

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, summary TEXT NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(kind,id))')
        db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        return db

    def _committed(self, db):
        # 库内随机代号标识“这一份数据库”：文件被删除重建时代号变化，不沿用旧摘要。
        # 缓存只在本进程内有效；恢复备份需先停服务，因此不会读到过期摘要。
        row = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
        if row is None:
            with db:
                db.execute("INSERT INTO metadata VALUES ('generation',?)", (uuid.uuid4().hex,))
            row = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
        key = str(self.path)
        cached = _COMMITTED.get(key)
        if cached is None or cached[0] != row[0]:
            state = {kind: {} for kind in KINDS}
            for kind, identifier, encoded in db.execute('SELECT kind,id,snapshot FROM records'):
                if kind in state:
                    state[kind][identifier] = _digest(encoded)
            cached = _COMMITTED[key] = (row[0], state)
        return cached[1]

    def write(self, entries):
        with _LOCK:
            db = self._connect()
            try:
                committed = self._committed(db)
                pending = {}
                with db:
                    for kind in KINDS:
                        snapshots = entries.get(kind, [])
                        existing = committed[kind]
                        current = {}
                        for snapshot in snapshots:
                            identifier = str(snapshot.get('id') or '')
                            if not identifier:
                                continue
                            snapshot = quality_index(snapshot) if kind == 'quality_tests' else snapshot
                            encoded = _encode(snapshot)
                            current[identifier] = _digest(encoded)
                            if existing.get(identifier) != current[identifier]:
                                db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)', (kind, identifier, _encode(summary(snapshot)), encoded))
                        for identifier in existing.keys() - current.keys():
                            db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, identifier))
                        pending[kind] = current
                    db.execute("INSERT OR REPLACE INTO metadata VALUES ('initialized','1')")
                committed.update(pending)
            except Exception:
                # 事务已回滚：丢弃缓存，下次从数据库重建，避免摘要与磁盘不一致。
                _COMMITTED.pop(str(self.path), None)
                raise
            finally:
                db.close()

    def read(self):
        with _LOCK:
            db = self._connect()
            try:
                initialized = db.execute("SELECT value FROM metadata WHERE key='initialized'").fetchone()
                rows = db.execute('SELECT kind,snapshot FROM records').fetchall()
            finally:
                db.close()
            if not initialized and self.legacy.exists():
                data = json.loads(self.legacy.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('历史索引格式错误')
                self.write(data)
                return self.read()
            result = {kind: [] for kind in KINDS}
            for kind, encoded in rows:
                result[kind].append(json.loads(encoded))
            return result
