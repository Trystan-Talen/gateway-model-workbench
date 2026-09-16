"""Persistent, server-expiring sessions. Only hashes of bearer tokens are stored."""
import hashlib
import os
import sqlite3
import threading
import time


class SessionStore:
    def __init__(self, path, *, lifetime=604800, idle_timeout=43200, clock=time.time):
        self.path = str(path)
        self.lifetime = lifetime
        self.idle_timeout = idle_timeout
        self.clock = clock
        self.lock = threading.RLock()

    def _connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL, touched REAL NOT NULL)')
        return db

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def __setitem__(self, token, username):
        now = self.clock()
        with self.lock:
            db = self._connect()
            try:
                with db:
                    db.execute('DELETE FROM sessions WHERE expires<=? OR touched<=?', (now, now-self.idle_timeout))
                    db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)', (self._hash(token), username, now+self.lifetime, now))
            finally:
                db.close()

    def get(self, token, default=None):
        now = self.clock()
        with self.lock:
            db = self._connect()
            try:
                with db:
                    row = db.execute('SELECT username, expires, touched FROM sessions WHERE token_hash=?', (self._hash(token),)).fetchone()
                    if not row:
                        return default
                    if row[1] <= now or row[2] <= now-self.idle_timeout:
                        db.execute('DELETE FROM sessions WHERE token_hash=?', (self._hash(token),))
                        return default
                    # Polling must not fsync a session on every request.
                    if now-row[2] >= min(60, self.idle_timeout/2):
                        db.execute('UPDATE sessions SET touched=? WHERE token_hash=?', (now, self._hash(token)))
                    return row[0]
            finally:
                db.close()

    def pop(self, token, default=None):
        with self.lock:
            db = self._connect()
            try:
                with db:
                    row = db.execute('SELECT username FROM sessions WHERE token_hash=?', (self._hash(token),)).fetchone()
                    db.execute('DELETE FROM sessions WHERE token_hash=?', (self._hash(token),))
                    return row[0] if row else default
            finally:
                db.close()

    def revoke_user(self, username):
        with self.lock:
            db = self._connect()
            try:
                with db:
                    db.execute('DELETE FROM sessions WHERE username=?', (username,))
            finally:
                db.close()

    def clear(self):
        with self.lock:
            db = self._connect()
            try:
                with db:
                    db.execute('DELETE FROM sessions')
            finally:
                db.close()
