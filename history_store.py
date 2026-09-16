"""Transactional per-record index with legacy JSON import; immutable evidence stays outside."""
import json
import os
import sqlite3
import threading
from pathlib import Path

_LOCK = threading.RLock()
KINDS = ('tasks', 'comparisons', 'quality_tests', 'routing_comparisons')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


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

    def write(self, entries):
        with _LOCK:
            db = self._connect()
            try:
                with db:
                    for kind in KINDS:
                        snapshots = entries.get(kind, [])
                        existing = {row[0]: row[1] for row in db.execute('SELECT id,snapshot FROM records WHERE kind=?', (kind,))}
                        seen = set()
                        for snapshot in snapshots:
                            identifier = str(snapshot.get('id') or '')
                            if not identifier:
                                continue
                            seen.add(identifier)
                            snapshot = quality_index(snapshot) if kind == 'quality_tests' else snapshot
                            encoded = _encode(snapshot)
                            if existing.get(identifier) != encoded:
                                db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)', (kind, identifier, _encode(summary(snapshot)), encoded))
                        for identifier in existing.keys() - seen:
                            db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, identifier))
                    db.execute("INSERT OR REPLACE INTO metadata VALUES ('initialized','1')")
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
