"""Server deployment behaviour: proxy-aware client IP, guarded first-run setup, incremental index writes."""
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import history_store
import route_test_ui as ui
from history_store import HistoryStore
from session_store import SessionStore


class FakeHandler:
    def __init__(self, peer, forwarded=None):
        self.client_address = (peer, 40000)
        self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}


class ClientIpTests(unittest.TestCase):
    def test_forwarded_header_is_ignored_without_trusted_proxy(self):
        with mock.patch.dict(os.environ, {"ROUTE_TEST_TRUSTED_PROXIES": ""}):
            self.assertEqual(ui._client_ip(FakeHandler("198.51.100.4", "10.0.0.1")), "198.51.100.4")

    def test_trusted_proxy_resolves_rightmost_untrusted_hop(self):
        with mock.patch.dict(os.environ, {"ROUTE_TEST_TRUSTED_PROXIES": "127.0.0.1"}):
            # The left entry is client-supplied and may be forged; the proxy appended the real peer.
            handler = FakeHandler("127.0.0.1", "127.0.0.1, 203.0.113.9")
            self.assertEqual(ui._client_ip(handler), "203.0.113.9")
            self.assertEqual(ui._login_rate_key(handler), "203.0.113.9")
            self.assertFalse(ui._is_loopback_client(handler))

    def test_untrusted_peer_cannot_claim_loopback(self):
        with mock.patch.dict(os.environ, {"ROUTE_TEST_TRUSTED_PROXIES": "127.0.0.1"}):
            self.assertFalse(ui._is_loopback_client(FakeHandler("203.0.113.9", "127.0.0.1")))

    def test_login_limit_is_per_real_client_behind_proxy(self):
        with ui.LOGIN_RATE_LOCK:
            ui.LOGIN_FAILURES.clear()
        self.addCleanup(ui.LOGIN_FAILURES.clear)
        with mock.patch.dict(os.environ, {"ROUTE_TEST_TRUSTED_PROXIES": "127.0.0.1"}):
            attacker = FakeHandler("127.0.0.1", "203.0.113.9")
            for _ in range(ui.LOGIN_RATE_LIMIT):
                ui._record_login_failure(attacker, now=1000.0)
            self.assertIsNotNone(ui._login_rate_status(attacker, now=1000.0))
            self.assertIsNone(ui._login_rate_status(FakeHandler("127.0.0.1", "203.0.113.10"), now=1000.0))


class InitialAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        accounts = os.path.join(self.temp.name, "accounts.json")
        patches = [mock.patch.object(ui, "ACCOUNTS_FILE", accounts),
                   mock.patch.object(ui, "_ACCOUNTS_CACHE", {"signature": None, "accounts": []}),
                   mock.patch.object(ui, "SESSIONS", SessionStore(os.path.join(self.temp.name, "sessions.sqlite3"))),
                   mock.patch.object(ui.Handler, "_audit", lambda *args, **kwargs: None),
                   mock.patch.object(ui.Handler, "_note", lambda *args, **kwargs: None),
                   mock.patch.dict(os.environ, {"ROUTE_TEST_TRUSTED_PROXIES": "127.0.0.1"})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(stop)
        return server.server_port

    def _setup(self, port, forwarded=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if forwarded:
            headers["X-Forwarded-For"] = forwarded
        conn.request("POST", "/auth/setup", json.dumps({"username": "admin", "password": "long-enough"}), headers)
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        return response.status, body

    def test_remote_web_setup_is_rejected_and_creates_nothing(self):
        status, body = self._setup(self._serve(), forwarded="203.0.113.9")
        self.assertEqual(status, 403)
        self.assertIn("tools/create_admin.py", body["error"])
        self.assertTrue(ui._auth_setup_required())

    def test_local_web_setup_still_works(self):
        status, body = self._setup(self._serve())
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "admin")
        self.assertFalse(ui._auth_setup_required())

    def test_create_initial_admin_validates_and_refuses_second_admin(self):
        with self.assertRaises(ValueError):
            ui.create_initial_admin("ad", "long-enough")
        with self.assertRaises(ValueError):
            ui.create_initial_admin("admin", "short")
        account = ui.create_initial_admin(" admin ", "long-enough")
        self.assertEqual(account["username"], "admin")
        self.assertTrue(ui._check_password("long-enough", ui._read_accounts()[0]))
        with self.assertRaises(PermissionError):
            ui.create_initial_admin("other", "long-enough")


class IncrementalHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = HistoryStore(Path(folder.name) / "index.json")

    def _statements(self, entries):
        seen = []
        real_connect = self.store._connect
        def traced():
            db = real_connect()
            db.set_trace_callback(seen.append)
            return db
        with mock.patch.object(self.store, "_connect", traced):
            self.store.write(entries)
        return [sql for sql in seen if sql.startswith(("INSERT OR REPLACE INTO records", "DELETE FROM records", "SELECT kind,id,snapshot"))]

    def test_only_changed_records_are_written_and_snapshots_are_not_reread(self):
        self.store.write({"tasks": [{"id": "a", "done": 1}, {"id": "b", "done": 1}]})
        statements = self._statements({"tasks": [{"id": "a", "done": 1}, {"id": "b", "done": 2}]})
        self.assertEqual(len(statements), 1)
        self.assertIn("'b'", statements[0])
        self.assertEqual({item["id"]: item["done"] for item in self.store.read()["tasks"]}, {"a": 1, "b": 2})

    def test_removed_records_are_deleted(self):
        self.store.write({"tasks": [{"id": "a"}, {"id": "b"}]})
        self.store.write({"tasks": [{"id": "a"}]})
        self.assertEqual([item["id"] for item in self.store.read()["tasks"]], ["a"])

    def test_recreated_database_is_rewritten_instead_of_trusting_stale_cache(self):
        self.store.write({"tasks": [{"id": "a"}]})
        os.remove(self.store.path)
        for suffix in ("-wal", "-shm"):
            Path(str(self.store.path) + suffix).unlink(missing_ok=True)
        self.store.write({"tasks": [{"id": "a"}]})
        self.assertEqual([item["id"] for item in self.store.read()["tasks"]], ["a"])

    def test_failed_commit_drops_cache(self):
        self.store.write({"tasks": [{"id": "a"}]})
        with mock.patch.object(history_store, "_encode", side_effect=[sqlite3.OperationalError("disk full")]):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.write({"tasks": [{"id": "a", "done": 1}]})
        self.store.write({"tasks": [{"id": "a", "done": 1}]})
        self.assertEqual(self.store.read()["tasks"][0]["done"], 1)


if __name__ == "__main__":
    unittest.main()
