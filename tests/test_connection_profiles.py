"""Named connections use temporary metadata and synthetic credential backends."""
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import route_test_ui as ui
from connection_profiles import ConnectionStore, ConnectionStorageError


class ConnectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.keys = {}
        self.path = Path(self.folder.name) / "connections.json"
        self.store = ConnectionStore(self.path, load_key=lambda ref: self.keys.get(ref, ""),
                                     save_key=self.save_key, delete_key=self.delete_key, available=lambda: True)
        self.url = "http://127.0.0.1:9001/v1"

    def save_key(self, ref, key):
        self.keys[ref] = key
        return True

    def delete_key(self, ref):
        self.keys.pop(ref, None)
        return True

    def create(self, name="连接甲", key="synthetic-alpha", url=None):
        return self.store.save(name=name, url=url or self.url, api_key=key)

    def test_roundtrip_separate_credentials_and_private_metadata(self):
        self.assertEqual(self.store.list(), [])
        profile = self.create()
        self.assertEqual(self.store.list(), [profile])
        self.assertEqual(self.store.resolve(profile["id"], self.url), "synthetic-alpha")
        self.assertEqual(self.store.reveal(profile["id"]), "synthetic-alpha")
        content = self.path.read_text()
        self.assertNotIn("synthetic-alpha", content)
        self.assertEqual(set(json.loads(content)[0]), {"id", "name", "url", "updated_at"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_same_url_keeps_distinct_keys_and_delete_is_independent(self):
        first, second = self.create(), self.create("连接乙", "synthetic-beta")
        self.keys[self.url] = "synthetic-ordinary"
        self.keys["routing:baseline:" + self.url] = "synthetic-route"
        self.store.delete(first["id"])
        self.assertEqual(self.store.resolve(second["id"], self.url), "synthetic-beta")
        self.assertEqual(self.keys[self.url], "synthetic-ordinary")
        self.assertEqual(self.keys["routing:baseline:" + self.url], "synthetic-route")
        self.assertEqual([row["id"] for row in self.store.list()], [second["id"]])

    def test_update_blank_retains_key_same_url_but_requires_key_new_url(self):
        first = self.create()
        changed = self.store.save(identifier=first["id"], name="新名称", url=self.url)
        self.assertEqual(changed["name"], "新名称")
        self.assertEqual(self.store.reveal(first["id"]), "synthetic-alpha")
        with self.assertRaisesRegex(ValueError, "更换地址"):
            self.store.save(identifier=first["id"], name="新名称", url=self.url + "/other")
        self.store.save(identifier=first["id"], name="新名称", url=self.url + "/other", api_key="synthetic-new")
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.store.resolve(first["id"], self.url)
        self.assertEqual(self.store.reveal(first["id"]), "synthetic-new")

    def test_duplicate_name_does_not_overwrite_and_missing_ids_are_rejected(self):
        first = self.create("Common")
        with self.assertRaisesRegex(ValueError, "名称已存在"):
            self.create(" common ", "synthetic-different")
        self.assertEqual(self.store.reveal(first["id"]), "synthetic-alpha")
        for identifier in ("../file", "0" * 24):
            with self.assertRaises(ValueError):
                self.store.reveal(identifier)

    def test_invalid_url_name_key_never_writes(self):
        for url in ("https://user:pass@example.invalid", "https://example.invalid/?key=x", "https://example.invalid/#x",
                    "https://example.invalid:bad", "https://example.invalid/\\x", "file:///tmp/x", "", "http://bad host"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.create(url=url) if url else self.store.save(name="name", url=url, api_key="synthetic-alpha")
        for name, key in (("", "key"), ("x" * 81, "key"), ("bad\nname", "key"), ("name", "bad\nkey"), ("synthetic-alpha", "synthetic-alpha")):
            with self.assertRaises(ValueError):
                self.store.save(name=name, url=self.url, api_key=key)
        self.assertEqual(self.keys, {})
        self.assertFalse(self.path.exists())

    def test_unavailable_or_failed_credentials_never_report_save_success(self):
        with patch.object(self.store, "available", return_value=False):
            with self.assertRaisesRegex(ConnectionStorageError, "不可用"):
                self.create()
        with patch.object(self.store, "save_key", return_value=False):
            with self.assertRaisesRegex(ConnectionStorageError, "写入.*失败"):
                self.create()
        self.assertFalse(self.path.exists())

    def test_failed_metadata_save_rolls_back_new_and_existing_keys(self):
        first = self.create()
        original = self.path.read_text()
        with patch.object(self.store, "_write", side_effect=ConnectionStorageError("synthetic failure")):
            with self.assertRaises(ConnectionStorageError):
                self.store.save(identifier=first["id"], name="新名称", url=self.url, api_key="synthetic-changed")
            with self.assertRaises(ConnectionStorageError):
                self.create("新增", "synthetic-new")
        self.assertEqual(self.keys, {self.store.reference(first["id"]): "synthetic-alpha"})
        self.assertEqual(self.path.read_text(), original)

    def test_delete_failure_keeps_metadata_and_restores_key_on_write_failure(self):
        first = self.create()
        with patch.object(self.store, "available", return_value=False):
            with self.assertRaisesRegex(ConnectionStorageError, "不可用"):
                self.store.delete(first["id"])
        with patch.object(self.store, "delete_key", return_value=False):
            with self.assertRaisesRegex(ConnectionStorageError, "删除失败"):
                self.store.delete(first["id"])
        with patch.object(self.store, "_write", side_effect=ConnectionStorageError("synthetic failure")):
            with self.assertRaises(ConnectionStorageError):
                self.store.delete(first["id"])
        self.assertEqual(self.store.reveal(first["id"]), "synthetic-alpha")

    def test_corrupt_file_is_not_overwritten(self):
        for content in ("{broken", "{}", '[{"api_key":"synthetic-bad"}]'):
            self.path.write_text(content)
            with self.assertRaises(ConnectionStorageError):
                self.create()
            self.assertEqual(self.path.read_text(), content)
        self.assertEqual(self.keys, {})

    def test_actual_write_failure_cleans_private_temporary_file(self):
        with patch("connection_profiles.os.replace", side_effect=OSError("synthetic disk error")):
            with self.assertRaises(ConnectionStorageError):
                self.create()
        self.assertEqual(list(self.path.parent.iterdir()), [])
        self.assertEqual(self.keys, {})

    def test_concurrent_creations_preserve_all_connections(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda index: self.create(f"连接{index}", f"synthetic-{index}"), range(12)))
        self.assertEqual(len(self.store.list()), 12)
        self.assertEqual(len({row["id"] for row in rows}), 12)


class NamedConnectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.keys = {}
        self.url = "http://127.0.0.1:9001/v1"
        self.store = ConnectionStore(Path(self.folder.name) / "connections.json",
                                     load_key=lambda ref: self.keys.get(ref, ""), save_key=self.save_key,
                                     delete_key=self.delete_key, available=lambda: ui._keychain_available())
        self.config_path = Path(self.folder.name) / "tasks.json"
        patches = [patch.object(ui, "CONNECTIONS", self.store), patch.object(ui, "TASKS_FILE", str(self.config_path)),
                   patch.object(ui, "_load_api_key", side_effect=lambda ref: self.keys.get(ref, "")),
                   patch.object(ui, "_save_api_key", side_effect=self.save_key),
                   patch.object(ui, "_delete_api_key", side_effect=self.delete_key),
                   patch.object(ui, "_keychain_available", return_value=True),
                   patch.object(ui, "_current_user", return_value={"role": "user"}),
                   patch.object(ui.Handler, "_note"), patch.object(ui.Handler, "_audit"), patch.object(ui, "persist_history")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def save_key(self, ref, key):
        self.keys[ref] = key
        return True

    def delete_key(self, ref):
        self.keys.pop(ref, None)
        return True

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST" if body is not None else "GET", path,
                           json.dumps(body) if body is not None else None,
                           {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        self.last_headers = dict(response.getheaders())
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def create(self, name="连接甲", key="synthetic-alpha"):
        status, body = self.request("/connections", {"name": name, "url": self.url, "api_key": key})
        self.assertEqual(status, 201, body)
        return body["connection"]

    def connection(self, profile=None, model="synthetic-model"):
        profile = profile or self.create()
        return {"connection_id": profile["id"], "url": self.url, "api_key": "", "model": model,
                "rpm": 120, "concurrency": 4, "iterations": 3}

    def test_crud_and_explicit_reveal_without_public_secret_leak(self):
        first = self.create()
        status, listing = self.request("/connections")
        self.assertEqual(status, 200)
        self.assertTrue(listing["storage_available"])
        self.assertEqual(listing["connections"], [first])
        self.assertNotIn("synthetic-alpha", json.dumps(listing))
        status, result = self.request("/connections", {"id": first["id"], "name": "新名称", "url": self.url})
        self.assertEqual(status, 200, result)
        status, result = self.request(f'/connections/{first["id"]}/reveal', {})
        self.assertEqual((status, result), (200, {"api_key": "synthetic-alpha"}))
        self.assertEqual(self.last_headers["Cache-Control"], "no-store")
        self.assertEqual(self.request(f'/connections/{first["id"]}/delete', {})[0], 200)
        self.assertEqual(self.request("/connections")[1]["connections"], [])

    def test_authentication_and_origin_for_list_save_reveal_delete(self):
        first = self.create()
        routes = [("/connections", None), ("/connections", {"name": "x", "url": self.url, "api_key": "key"}),
                  (f'/connections/{first["id"]}/reveal', {}), (f'/connections/{first["id"]}/delete', {})]
        with patch.object(ui, "_current_user", return_value=None):
            for path, body in routes:
                status, response = self.request(path, body)
                self.assertEqual(status, 401, response)
                self.assertNotIn("synthetic-alpha", json.dumps(response))
        for path, body in routes[1:]:
            self.assertEqual(self.request(path, body, {"Origin": "https://other.invalid"})[0], 403)
        self.assertEqual(len(self.store.list()), 1)

    def test_save_failures_and_validation_return_authored_errors(self):
        with patch.object(ui, "_keychain_available", return_value=False):
            status, response = self.request("/connections", {"name": "x", "url": self.url, "api_key": "synthetic-alpha"})
            self.assertEqual(status, 503)
            self.assertIn("未保存", response["error"])
            self.assertNotIn("synthetic-alpha", json.dumps(response))
        first = self.create()
        for body in ({"name": "连接甲", "url": self.url, "api_key": "synthetic-beta"},
                     {"id": first["id"], "name": "x", "url": self.url + "/new"},
                     {"name": "x", "url": self.url}):
            self.assertEqual(self.request("/connections", body)[0], 400)
        self.assertEqual(self.request("/connections/invalid/reveal", {})[0], 400)
        self.assertEqual(self.request("/connections/invalid/delete", {})[0], 400)
        self.assertEqual(self.request("/connections/invalid/other", {})[0], 404)

    def test_source_side_copies_existing_saved_connection_but_leaves_it_untouched(self):
        for side, name in (("", "普通"), ("baseline", "固定"), ("candidate", "智能")):
            reference = ui._routing_credential_ref(side, self.url)
            self.keys[reference] = "synthetic-" + name
            status, body = self.request("/connections", {"name": name, "url": self.url, "source_side": side})
            self.assertEqual(status, 201, body)
            self.assertEqual(self.store.reveal(body["connection"]["id"]), "synthetic-" + name)
            self.assertEqual(self.keys[reference], "synthetic-" + name)
        self.assertEqual(self.request("/connections", {"name": "bad", "url": self.url, "source_side": "other"})[0], 400)

    def test_regular_create_and_config_restore_use_bound_profile_only(self):
        connection = self.connection()
        self.keys[self.url] = "synthetic-ordinary"
        with patch.object(ui.MANAGER, "add", return_value=SimpleNamespace(id="synthetic-task")) as create:
            status, response = self.request("/tasks", connection)
        self.assertEqual(status, 201, response)
        self.assertEqual(create.call_args.args[0]["api_key"], "synthetic-alpha")
        self.assertEqual(self.keys[self.url], "synthetic-ordinary")
        config = self.request("/config")[1]["config"]
        self.assertEqual(config["connection_id"], connection["connection_id"])
        self.assertTrue(config["api_key_saved"])
        self.assertNotIn("synthetic-alpha", self.config_path.read_text())
        self.assertNotIn("synthetic-alpha", json.dumps(config))
        self.assertEqual(ui.validate_config(connection)["api_key"], "synthetic-alpha")
        self.assertNotIn("synthetic-alpha", json.dumps(ui._history_safe_snapshot(ui.validate_config(connection))))

    def test_profile_binding_rejects_missing_profile_and_exact_address_mismatch(self):
        connection = self.connection()
        for target in ({**connection, "url": self.url + "/"}, {**connection, "connection_id": "0" * 24}):
            for path, body in (("/tasks", target), ("/models", target), ("/preflight", {"config": target})):
                status, response = self.request(path, body)
                self.assertEqual(status, 400, response)
                self.assertNotIn("synthetic-alpha", json.dumps(response))
        self.store.delete(connection["connection_id"])
        self.assertFalse(ui.read_saved_config().get("api_key_saved", False))

    def test_resource_comparison_creates_with_profile_without_generic_write(self):
        connection = self.connection()
        body = {"shared_config": connection, "groups": [{"iterations": 3}, {"iterations": 3}]}
        with patch.object(ui.COMPARISONS, "create", return_value=SimpleNamespace(id="synthetic-comparison")) as create:
            status, response = self.request("/comparisons", body)
        self.assertEqual(status, 201, response)
        self.assertEqual(create.call_args.args[0]["shared_config"]["api_key"], "synthetic-alpha")
        self.assertEqual(ui.validate_comparison_config(body)["shared_config"]["connection_id"], connection["connection_id"])
        self.assertNotIn(self.url, self.keys)

    def change_connection(self, profile, mutation):
        if mutation == "delete":
            self.store.delete(profile["id"])
        else:
            self.store.save(identifier=profile["id"], name=profile["name"], url=self.url + "/new",
                            api_key="synthetic-new-address")

    def test_frozen_serial_comparison_groups_survive_connection_deletion_or_address_edit(self):
        for mutation in ("delete", "change_address"):
            with self.subTest(mutation=mutation):
                profile = self.create(name="对照冻结-" + mutation)
                connection = self.connection(profile)
                config = ui.validate_comparison_config({"shared_config": connection,
                                                        "groups": [{"iterations": 3}, {"iterations": 3}]})
                run = ui.ComparisonRun(config)
                manager = ui.TaskManager()
                created = []

                def add_group(raw, **kwargs):
                    # Exercise the real task validation/creation; only scheduling
                    # is stubbed so no requests are sent or output files opened.
                    task = manager.add(raw, **kwargs)
                    task.status = "finished"
                    created.append(task)
                    if len(created) == 1:
                        self.change_connection(profile, mutation)
                    return task

                with patch.object(manager, "_pump"), patch.object(ui.EVENTS, "log"), \
                        patch.object(ui.MANAGER, "add", side_effect=add_group):
                    ui.ComparisonManager()._execute(run)

                self.assertEqual(run.status, "finished")
                self.assertEqual(len(created), 2)
                for task in created:
                    self.assertEqual(task.config["api_key"], "synthetic-alpha")
                    self.assertEqual(task.config["url"], self.url)
                    self.assertNotIn("connection_id", task.config)
                    self.assertEqual(task.parent_comparison_id, run.id)
                self.assertEqual(run.config["shared_config"]["connection_id"], profile["id"])
                self.assertEqual(run.snap()["config"]["connection_id"], profile["id"])
                self.assertNotIn("synthetic-alpha", json.dumps(run.snap()))
                with self.assertRaises(ValueError):
                    ui.validate_config({**connection, "api_key": "synthetic-alpha"})
                self.assertNotIn(self.url, self.keys)

    def test_started_task_save_does_not_reparse_binding_after_concurrent_edit(self):
        for mutation in ("delete", "change_address"):
            with self.subTest(mutation=mutation):
                profile = self.create(name="任务冻结-" + mutation)
                connection = self.connection(profile)

                def create_task(raw):
                    self.assertEqual(ui.validate_config(raw)["api_key"], "synthetic-alpha")
                    self.change_connection(profile, mutation)
                    return SimpleNamespace(id="synthetic-frozen-task")

                with patch.object(ui.MANAGER, "add", side_effect=create_task):
                    status, response = self.request("/tasks", connection)
                self.assertEqual(status, 201, response)
                saved = json.loads(self.config_path.read_text())["last_config"]
                self.assertEqual(saved["connection_id"], profile["id"])
                self.assertEqual(saved["url"], self.url)
                self.assertNotIn("synthetic-alpha", self.config_path.read_text())
                self.assertNotIn(self.url, self.keys)
                # The external save endpoint still resolves and rejects stale
                # bindings; a submitted request cannot opt into the trusted path.
                self.assertEqual(self.request("/config", {**connection, "resolve_binding": False})[0], 400)

    def test_started_routing_save_does_not_reparse_binding_after_concurrent_edit(self):
        for mutation in ("delete", "change_address"):
            with self.subTest(mutation=mutation):
                profile = self.create(name="路由冻结-" + mutation)
                connection = self.connection(profile)
                body = {"tier": 1000, "rpm": 120, "baseline": dict(connection), "candidate": dict(connection)}

                def create_routing(raw):
                    frozen = ui.routing_compare.validate_config(raw)
                    self.assertEqual(frozen["candidate"]["api_key"], "synthetic-alpha")
                    self.change_connection(profile, mutation)
                    return SimpleNamespace(id="synthetic-frozen-routing")

                with patch.object(ui.ROUTING_COMPARISONS, "create", side_effect=create_routing):
                    status, response = self.request("/routing-comparisons", body)
                self.assertEqual(status, 201, response)
                saved = json.loads(self.config_path.read_text())
                for side in ("baseline", "candidate"):
                    self.assertEqual(saved["last_routing_config"][side]["connection_id"], profile["id"])
                    self.assertEqual(saved["last_routing_config"][side]["url"], self.url)
                    self.assertNotIn(ui._routing_credential_ref(side, self.url), self.keys)
                    self.assertEqual(self.request("/routing-config", {**connection, "side": side,
                                                                    "resolve_binding": False})[0], 400)
                self.assertEqual(saved["last_config"]["connection_id"], profile["id"])
                self.assertNotIn("synthetic-alpha", self.config_path.read_text())
                self.assertNotIn(self.url, self.keys)

    def test_routing_distinct_profiles_create_discover_autosave_restore(self):
        first, second = self.create(), self.create("连接乙", "synthetic-beta")
        sides = {"baseline": self.connection(first), "candidate": self.connection(second)}
        body = {"tier": 1000, "rpm": 120, **sides}
        with patch.object(ui.ROUTING_COMPARISONS, "create", return_value=SimpleNamespace(id="synthetic-routing")) as create:
            status, response = self.request("/routing-comparisons", body)
        self.assertEqual(status, 201, response)
        self.assertEqual(create.call_args.args[0]["baseline"]["api_key"], "synthetic-alpha")
        self.assertEqual(create.call_args.args[0]["candidate"]["api_key"], "synthetic-beta")
        restored = self.request("/routing-config")[1]["config"]
        for side, profile in (("baseline", first), ("candidate", second)):
            self.assertEqual(restored[side]["connection_id"], profile["id"])
            self.assertTrue(restored[side]["api_key_saved"])
            self.assertEqual(self.request("/routing-config", {**sides[side], "side": side})[0], 200)
            self.assertNotIn(ui._routing_credential_ref(side, self.url), self.keys)
        self.assertNotIn(self.url, self.keys)
        self.assertEqual(self.request("/preflight", {"kind": "routing", "config": body})[0], 200)
        with patch.object(ui, "fetch_models_detailed_sync", return_value={"ok": True, "models": ["synthetic-model"]}) as fetch:
            self.assertEqual(self.request("/routing-models", {**sides["candidate"], "side": "candidate"})[0], 200)
        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer synthetic-beta")
        self.assertNotIn("synthetic-beta", self.config_path.read_text())

    def test_regular_and_fingerprint_discovery_receive_selected_secret_without_persistence(self):
        connection = self.connection()
        with patch.object(ui, "fetch_models_detailed_sync", return_value={"ok": True, "models": ["synthetic-model"], "attempted": []}) as fetch:
            self.assertEqual(self.request("/models", connection)[0], 200)
        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer synthetic-alpha")
        with patch.object(ui, "discover_fingerprint_models", return_value={"models": ["synthetic-model"]}) as discover:
            status, response = self.request("/fingerprint/models", {key: connection[key] for key in ("url", "api_key", "connection_id")})
        self.assertEqual(status, 200, response)
        self.assertEqual(discover.call_args.args[0], {"url": self.url, "api_key": "synthetic-alpha"})
        self.assertNotIn(self.url, self.keys)
        with patch.object(ui, "fetch_models_detailed_sync", return_value={"ok": False, "error": "echo synthetic-alpha"}):
            status, response = self.request("/models", connection)
        self.assertEqual(status, 502)
        self.assertNotIn("synthetic-alpha", json.dumps(response))
        with patch.object(ui, "fetch_models_detailed_sync", return_value={"ok": True, "models": ["echo-synthetic-alpha", "safe-model"], "attempted": []}):
            status, response = self.request("/models", connection)
        self.assertEqual(response["models"], ["safe-model"])

    def test_quality_validate_and_creation_resolve_each_profile(self):
        first, second = self.create(), self.create("连接乙", "synthetic-beta")
        resources = [{key: target[key] for key in ("url", "model", "api_key", "connection_id")}
                     for target in (self.connection(first), self.connection(second))]
        body = {"resources": resources}
        validated = ui.validate_quality_config(body)
        self.assertEqual([row["api_key"] for row in validated["resources"]], ["synthetic-alpha", "synthetic-beta"])
        self.assertEqual([row["connection_id"] for row in ui._quality_public_config(validated)["resources"]], [first["id"], second["id"]])
        with patch.object(ui.QUALITY, "create", return_value=SimpleNamespace(id="synthetic-quality")) as create:
            status, response = self.request("/quality-tests", body)
        self.assertEqual(status, 201, response)
        self.assertEqual([row["api_key"] for row in create.call_args.args[0]["resources"]], ["synthetic-alpha", "synthetic-beta"])
        self.assertEqual(self.request("/preflight", {"kind": "quality", "config": body})[0], 200)
        manager = ui.QualityManager()
        with patch.object(ui, "QualityTask", return_value=SimpleNamespace(id="synthetic-managed-quality")), \
                patch.object(ui.threading, "Thread"), patch.object(ui, "_save_api_key") as save:
            manager.create(body)
        save.assert_not_called()

    def test_fingerprint_creation_resolves_endpoint_and_strips_binding_for_protocol(self):
        connection = self.connection()
        endpoint = {key: connection[key] for key in ("url", "model", "api_key", "connection_id")}
        with patch.object(ui.FINGERPRINTS, "create", return_value={"id": "synthetic-fingerprint", "mode": "quick"}) as create:
            status, response = self.request("/fingerprints", {"endpoint": endpoint, "mode": "quick"})
        self.assertEqual(status, 201, response)
        self.assertEqual(create.call_args.args[0]["endpoint"], {"url": self.url, "model": "synthetic-model", "api_key": "synthetic-alpha"})


if __name__ == "__main__":
    unittest.main()
