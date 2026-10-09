"""Repetition freezes source parameters and uses only synthetic current credentials."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import route_test_ui as ui
import fingerprint_protocol
import routing_compare
from connection_profiles import ConnectionStore
from fingerprint_service import FingerprintManager


class FakeManager:
    def __init__(self):
        self.sources = {}
        self.created = []

    def get(self, identifier):
        return self.sources.get(identifier)

    def get_reference(self, identifier):
        return {"id": identifier} if identifier == "original-reference" else None

    def create(self, config, **kwargs):
        self.created.append((copy.deepcopy(config), copy.deepcopy(kwargs)))
        return {"id": "new-fingerprint"} if "endpoint" in config else SimpleNamespace(id="new-record")

    add = create


class _RepeatFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.url = "http://127.0.0.1:9011/v1"
        self.keys = {self.url: "synthetic-current-key"}
        self.managers = {kind: FakeManager() for kind in ("task", "comparison", "routing", "quality", "fingerprint")}
        self.store = ConnectionStore(Path(self.folder.name) / "connections.json", load_key=lambda ref: self.keys.get(ref, ""),
                                     save_key=self.save_key, delete_key=lambda ref: bool(self.keys.pop(ref, None)), available=lambda: True)
        for name, value in (("BASE_DIR", self.folder.name), ("CONNECTIONS", self.store),
                            ("MANAGER", self.managers["task"]), ("COMPARISONS", self.managers["comparison"]),
                            ("ROUTING_COMPARISONS", self.managers["routing"]), ("QUALITY", self.managers["quality"]),
                            ("FINGERPRINTS", self.managers["fingerprint"])):
            patcher = patch.object(ui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("persist_history", "_save_api_key", "save_config", "save_routing_connection"):
            patcher = patch.object(ui, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(ui, "_load_api_key", side_effect=lambda ref: self.keys.get(ref, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_key(self, ref, key):
        self.keys[ref] = key
        return True

    def config(self, **changes):
        return ui.validate_config({"name": "原测试", "url": self.url, "api_key": "synthetic-original-key", "model": "synthetic-model",
                                   "rpm": 100, "concurrency": 100, "iterations": 37, "duration": 23, "mode": "smoke", **changes})

    def add_source(self, kind="task", **changes):
        config = self.config(**changes)
        if kind == "task":
            source = ui.Task(config, *ui.build_plan(config))
        elif kind == "comparison":
            source = ui.ComparisonRun(ui.validate_comparison_config({"name": "原对照", "shared_config": config, "groups": [
                {"id": "a", "name": "基准", "role": "baseline", "rpm": 75, "concurrency": 8, "iterations": 21},
                {"id": "b", "name": "对照", "role": "comparison", "rpm": 150, "concurrency": 16, "iterations": 42}]}))
        elif kind == "routing":
            cfg = routing_compare.validate_config({"name": "原路由", "tier": 1000, "rpm": 100, "baseline": config, "candidate": config})
            source = routing_compare.RouteComparison(cfg)
        elif kind == "quality":
            source = ui.QualityTask(ui.validate_quality_config({"name": "原质量", "resources": [{key: value for key, value in config.items()
                if key in {"url", "api_key", "model", "connect_timeout", "read_timeout", "write_timeout", "http2", "insecure"}}]}))
        else:
            source = {"id": "source", "name": "原指纹", "status": "finished", "mode": "verify", "source_note": "原参考说明", "trusted_source": False,
                      "reference_id": "original-reference", "endpoint": {"url": self.url, "model": "synthetic-model"},
                      "protocol": fingerprint_protocol.make_protocol({"preset": "quick", "samples_per_cell": 10, "concurrency": 2, "timeout_seconds": 5})}
        if not isinstance(source, dict):
            source.status = "finished"
            source.id = "source"
        self.managers[kind].sources["source"] = source
        return source

    def body(self, kind="task", **changes):
        return {"source_kind": kind, "source_id": "source", **changes}


class RepeatTests(_RepeatFixture):
    def test_task_reuses_original_parameters_and_current_key_without_source_mutation(self):
        source = self.add_source()
        before = copy.deepcopy(source.config)
        preview = ui.prepare_repeat(self.body())
        self.assertEqual(preview["preflight"]["planned_requests"], 37)
        self.assertEqual(preview["connections"][0], {"slot": "main", "label": "网关连接", "url": self.url, "model": "synthetic-model", "saved": True})
        self.assertNotIn("synthetic-", json.dumps(preview).replace("synthetic-model", ""))
        first = ui.create_repeat(self.body())
        ui.create_repeat(self.body())
        self.assertEqual(first["task_id"], "new-record")
        copied = self.managers["task"].created[0][0]
        self.assertEqual(copied["api_key"], "synthetic-current-key")
        self.assertEqual(copied["rpm"], 100)
        self.assertEqual(copied["iterations"], 37)
        self.assertEqual(copied["name"], "原测试 · 再来一次")
        self.assertEqual(before, source.config)
        self.assertEqual(len(self.managers["task"].created), 2)
        self._save_api_key.assert_not_called()
        self.save_config.assert_not_called()
        self.save_routing_connection.assert_not_called()

    def test_missing_key_does_not_reuse_original_in_memory_key(self):
        self.add_source()
        self.keys.clear()
        self.assertFalse(ui.prepare_repeat(self.body())["connections"][0]["saved"])
        with self.assertRaisesRegex(ui.RepeatError, "缺少可用密钥"):
            ui.create_repeat(self.body())
        self.assertEqual(self.managers["task"].created, [])
        ui.create_repeat(self.body(keys={"main": "synthetic-replacement"}))
        self.assertEqual(self.managers["task"].created[0][0]["api_key"], "synthetic-replacement")
        self.assertEqual(self.keys, {})

    def test_profile_deleted_or_changed_address_has_no_url_fallback_but_explicit_key_can_replace(self):
        for changed_address in (False, True):
            with self.subTest(changed_address=changed_address):
                source = self.add_source()
                profile = self.store.save(name="常用" + str(changed_address), url=self.url, api_key="synthetic-profile")
                source.config["connection_id"] = profile["id"]
                self.assertTrue(ui.prepare_repeat(self.body())["connections"][0]["saved"])
                if changed_address:
                    self.store.save(identifier=profile["id"], name=profile["name"], url=self.url + "/changed", api_key="synthetic-changed")
                else:
                    self.store.delete(profile["id"])
                row = ui.prepare_repeat(self.body())["connections"][0]
                self.assertFalse(row["saved"])
                self.assertIn("issue", row)
                with self.assertRaises(ui.RepeatError):
                    ui.create_repeat(self.body())
                ui.create_repeat(self.body(keys={"main": "synthetic-explicit"}))
                copied = self.managers["task"].created[-1][0]
                self.assertNotIn("connection_id", copied)
                self.assertEqual(copied["url"], self.url)
                self.assertEqual(source.config["connection_id"], profile["id"])

    def test_profile_uses_its_current_key_and_keeps_id(self):
        source = self.add_source()
        profile = self.store.save(name="常用", url=self.url, api_key="synthetic-profile")
        source.config["connection_id"] = profile["id"]
        self.store.save(identifier=profile["id"], name="常用", url=self.url, api_key="synthetic-profile-new")
        ui.create_repeat(self.body())
        copied = self.managers["task"].created[-1][0]
        self.assertEqual(copied["connection_id"], profile["id"])
        self.assertEqual(copied["api_key"], "synthetic-profile-new")

    def test_production_risk_requires_new_true_acknowledgement(self):
        self.add_source(url="https://synthetic-production.invalid/v1")
        self.keys["https://synthetic-production.invalid/v1"] = "synthetic-current"
        self.assertTrue(ui.prepare_repeat(self.body())["preflight"]["requires_confirmation"])
        for value in (None, False, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ui.RepeatError, "确认"):
                ui.create_repeat(self.body(risk_acknowledged=value))
        self.assertEqual(self.managers["task"].created, [])
        ui.create_repeat(self.body(risk_acknowledged=True))

    def test_comparison_preserves_groups_and_aggregates_real_plan(self):
        source = self.add_source("comparison")
        preview = ui.prepare_repeat(self.body("comparison"))
        self.assertEqual(preview["preflight"]["planned_requests"], 63)
        self.assertEqual(preview["preflight"]["min_seconds"], 33.6)
        ui.create_repeat(self.body("comparison"))
        copied = self.managers["comparison"].created[0][0]
        self.assertEqual(copied["groups"], source.config["groups"])
        self.assertEqual(copied["execution_policy"], "serial")

    def test_comparison_cannot_bypass_production_risk(self):
        self.add_source("comparison", url="https://synthetic-production.invalid/v1")
        self.keys["https://synthetic-production.invalid/v1"] = "synthetic-current"
        with self.assertRaisesRegex(ui.RepeatError, "确认"):
            ui.create_repeat(self.body("comparison"))
        self.assertEqual(self.managers["comparison"].created, [])

    def test_routing_same_url_resolves_independent_side_keys_and_new_frozen_history(self):
        source = self.add_source("routing")
        for side in ("baseline", "candidate"):
            self.keys[ui._routing_credential_ref(side, self.url)] = "synthetic-" + side
        ui.create_repeat(self.body("routing"))
        copied = self.managers["routing"].created[0][0]
        self.assertEqual(copied["baseline"]["api_key"], "synthetic-baseline")
        self.assertEqual(copied["candidate"]["api_key"], "synthetic-candidate")
        snapshot = ui._routing_history_snapshot(source.snap())
        self.assertNotIn("synthetic-original-key", json.dumps(snapshot))
        restored = routing_compare.RouteComparison.from_snapshot(snapshot)
        self.managers["routing"].sources["source"] = restored
        ui.create_repeat(self.body("routing"))
        self.assertEqual(self.managers["routing"].created[1][0], copied)

    def test_routing_old_incomplete_history_and_changed_corpus_are_rejected(self):
        source = self.add_source("routing")
        source.repeat_corpus = {"fingerprint": "obsolete"}
        with self.assertRaisesRegex(ui.RepeatError, "清单"):
            ui.prepare_repeat(self.body("routing"))
        source = self.add_source("routing")
        snapshot = ui._routing_history_snapshot(source.snap())
        snapshot.pop("repeat_config")
        self.managers["routing"].sources["source"] = routing_compare.RouteComparison.from_snapshot(snapshot)
        with self.assertRaisesRegex(ui.RepeatError, "冻结"):
            ui.prepare_repeat(self.body("routing"))

    def test_routing_preflight_uses_original_raw_capture_option(self):
        self.add_source("routing", capture_raw=True)
        preview = ui.prepare_repeat(self.body("routing"))["preflight"]
        self.assertTrue(preview["capture_raw"])
        self.assertEqual(preview["estimated_bytes"], 2000 * 2600)

    def test_quality_preserves_exact_suite_even_when_file_cannot_be_loaded(self):
        source = self.add_source("quality")
        with patch.object(ui, "load_suite", side_effect=ValueError("synthetic removed file")):
            ui.create_repeat(self.body("quality"))
        copied, options = self.managers["quality"].created[0]
        self.assertEqual(copied["_suite_cases"], source.config["_suite_cases"])
        self.assertEqual(copied["suite_manifest"], source.config["suite_manifest"])
        self.assertEqual(copied["resources"][0]["api_key"], "synthetic-current-key")
        self.assertFalse(options["persist_credentials"])
        self.assertEqual(options["frozen_config"], copied)

    def test_quality_old_history_requires_matching_suite_fingerprint(self):
        source = self.add_source("quality")
        source.config.pop("_suite_cases")
        ui.prepare_repeat(self.body("quality"))
        source.config["suite_manifest"]["fingerprint"] = "obsolete"
        with self.assertRaisesRegex(ui.RepeatError, "题集"):
            ui.prepare_repeat(self.body("quality"))

    def test_quality_historical_reference_is_reused_without_credential_or_source_lookup(self):
        source = self.add_source("quality")
        source.config["reference_reuse_id"] = "deleted-original-reference"
        source.config["resources"].append({"name": "历史参考", "url": "https://reference.invalid", "model": "reference-model",
                                            "historical_reference": True, "is_reference": True, "reference_task_id": "deleted-original-reference"})
        source.results.append({"name": "历史参考", "case_results": [{"score": 1} for _ in source.config["_suite_cases"]], "summary": {"score": 1}})
        preview = ui.prepare_repeat(self.body("quality"))
        self.assertEqual(len(preview["connections"]), 1)
        ui.create_repeat(self.body("quality"))
        copied, options = self.managers["quality"].created[0]
        self.assertEqual(copied["_historical_reference_results"], source.results[1])
        self.assertEqual(copied["reference_reuse_id"], "deleted-original-reference")
        self.assertEqual(options["frozen_config"]["resources"][1]["historical_reference"], True)

    def test_quality_checks_candidate_production_risk_as_well_as_local_base(self):
        source = self.add_source("quality")
        resource = copy.deepcopy(source.config["resources"][0])
        resource["url"] = "https://production-candidate.invalid/v1"
        source.config["resources"].append(resource)
        self.keys[resource["url"]] = "synthetic-candidate"
        self.assertTrue(ui.prepare_repeat(self.body("quality"))["preflight"]["requires_confirmation"])
        with self.assertRaisesRegex(ui.RepeatError, "确认"):
            ui.create_repeat(self.body("quality"))

    def test_fingerprint_retains_protocol_and_original_reference_but_requires_new_cost_ack(self):
        source = self.add_source("fingerprint")
        preview = ui.prepare_repeat(self.body("fingerprint"))
        self.assertEqual(preview["preflight"]["planned_requests"], 40)
        self.assertTrue(preview["preflight"]["requires_confirmation"])
        with self.assertRaisesRegex(ui.RepeatError, "确认"):
            ui.create_repeat(self.body("fingerprint"))
        result = ui.create_repeat(self.body("fingerprint", risk_acknowledged=True))
        self.assertEqual(result["fingerprint_id"], "new-fingerprint")
        copied, options = self.managers["fingerprint"].created[0]
        self.assertEqual(copied["reference_id"], "original-reference")
        self.assertEqual(options["frozen_protocol"], source["protocol"])
        self.assertEqual(copied["endpoint"]["api_key"], "synthetic-current-key")

    def test_fingerprint_profile_binding_uses_original_unexpanded_url(self):
        source = self.add_source("fingerprint")
        profile = self.store.save(name="指纹常用", url=self.url, api_key="synthetic-fp-profile")
        source["endpoint"]["url"] = self.url + "/chat/completions"
        source["connection_binding"] = {"url": self.url, "connection_id": profile["id"]}
        ui.create_repeat(self.body("fingerprint", risk_acknowledged=True))
        copied, options = self.managers["fingerprint"].created[0]
        self.assertEqual(copied["endpoint"]["api_key"], "synthetic-fp-profile")
        self.assertEqual(copied["endpoint"]["url"], self.url + "/chat/completions")
        self.assertEqual(options["connection_binding"], source["connection_binding"])

    def test_fingerprint_explicitly_unauthenticated_source_stays_unauthenticated(self):
        source = self.add_source("fingerprint")
        source["key_required"] = False
        row = ui.prepare_repeat(self.body("fingerprint"))["connections"][0]
        self.assertFalse(row["saved"])
        self.assertTrue(row["optional"])
        ui.create_repeat(self.body("fingerprint", risk_acknowledged=True))
        self.assertEqual(self.managers["fingerprint"].created[0][0]["endpoint"]["api_key"], "")

    def test_fingerprint_changed_protocol_or_deleted_reference_is_rejected(self):
        source = self.add_source("fingerprint")
        source["protocol"]["samples_per_cell"] = 11
        with self.assertRaisesRegex(ui.RepeatError, "协议"):
            ui.prepare_repeat(self.body("fingerprint"))
        source = self.add_source("fingerprint")
        source["reference_id"] = "deleted-reference"
        with self.assertRaisesRegex(ui.RepeatError, "参考"):
            ui.prepare_repeat(self.body("fingerprint"))

    def test_active_unknown_and_internal_sources_are_not_created(self):
        source = self.add_source()
        for state in ("draft", "waiting", "running", "stopping"):
            source.status = state
            with self.assertRaises(ui.RepeatError) as caught:
                ui.create_repeat(self.body())
            self.assertEqual(caught.exception.status, 409)
        source.status = "finished"
        source.parent_comparison_id = "parent"
        with self.assertRaisesRegex(ui.RepeatError, "内部任务"):
            ui.prepare_repeat(self.body())
        for kind in ("unknown", [], {}, None):
            with self.subTest(kind=kind), self.assertRaises(ui.RepeatError):
                ui.prepare_repeat(self.body(source_kind=kind))
        with self.assertRaises(ui.RepeatError) as caught:
            ui.prepare_repeat(self.body(source_id="missing"))
        self.assertEqual(caught.exception.status, 404)

    def test_all_terminal_task_states_can_repeat(self):
        source = self.add_source()
        for state in ("finished", "finished_with_errors", "stopped", "error", "expired"):
            source.status = state
            ui.create_repeat(self.body())
        self.assertEqual(len(self.managers["task"].created), 5)

    def test_history_without_original_fields_or_ramps_is_not_guessed(self):
        source = self.add_source(mode="ramp", ramps="60,120")
        source.history_only = True
        source._history_snapshot = {"config": copy.deepcopy(source.public_config)}
        source._history_snapshot["config"].pop("ramps")
        with self.assertRaisesRegex(ui.RepeatError, "冻结"):
            ui.prepare_repeat(self.body())
        source._history_snapshot = {"config": copy.deepcopy(source.public_config), "repeat_unavailable": "旧结果没有原参数"}
        with self.assertRaisesRegex(ui.RepeatError, "旧结果"):
            ui.prepare_repeat(self.body())

    def test_missing_frozen_groups_or_resources_are_rejected_clearly(self):
        for kind, field in (("comparison", "groups"), ("quality", "resources")):
            for value in ([], [{}], {}, "incomplete"):
                with self.subTest(kind=kind, value=value):
                    source = self.add_source(kind)
                    source.config[field] = value
                    for operation in (ui.prepare_repeat, ui.create_repeat):
                        with self.assertRaisesRegex(ui.RepeatError, "冻结") as caught:
                            operation(self.body(kind))
                        self.assertEqual(caught.exception.status, 400)
                    self.assertEqual(self.managers[kind].created, [])

    def test_ramp_and_soak_preflight_match_actual_scheduling_plan(self):
        for options in ({"mode": "ramp", "ramps": "60,120", "duration": 10}, {"mode": "soak", "duration": 30, "rpm": 120}):
            source = self.add_source(**options)
            steps, count = ui.build_plan(source.config)
            preview = ui.prepare_repeat(self.body())["preflight"]
            self.assertEqual(preview["planned_requests"], count)
            self.assertEqual(preview["min_seconds"], sum(step["duration"] for step in steps))

    def test_malformed_keys_do_not_create(self):
        self.add_source()
        for keys in ({"unknown": "synthetic-secret"}, {"main": []}, {"main": "secret\nheader"}, []):
            with self.subTest(keys=keys), self.assertRaises(ui.RepeatError):
                ui.create_repeat(self.body(keys=keys))
        self.assertEqual(self.managers["task"].created, [])

    def test_quality_manager_frozen_creation_never_saves_replacement_credentials(self):
        source = self.add_source("quality")
        manager = ui.QualityManager()
        with patch.object(threading.Thread, "start"):
            copied = manager.create({}, frozen_config=source.config, persist_credentials=False)
        self.assertEqual(copied.config["_suite_cases"], source.config["_suite_cases"])
        self.assertIn("suite_cases", copied.public_config)
        self.assertNotIn("synthetic-original-key", json.dumps(copied.public_config))
        self._save_api_key.assert_not_called()


class RepeatHTTPTests(_RepeatFixture):
    def setUp(self):
        super().setUp()
        for name, replacement in (("_current_user", lambda handler: {"role": "user"} if handler.headers.get("X-Test-Auth") == "yes" else None),):
            patcher = patch.object(ui, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("_note", "_audit"):
            patcher = patch.object(ui.Handler, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, data, *, authenticated=True, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-Test-Auth"] = "yes"
        if origin:
            headers["Origin"] = origin
        connection.request("POST", path, json.dumps(data), headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        status = response.status
        connection.close()
        return status, result

    def test_http_repeat_routes_require_login_and_same_origin(self):
        self.add_source()
        for path in ("/repeats", "/repeats/prepare"):
            self.assertEqual(self.request(path, self.body(), authenticated=False)[0], 401)
            self.assertEqual(self.request(path, self.body(), origin="https://other.invalid")[0], 403)
        self.assertEqual(self.managers["task"].created, [])

    def test_http_prepare_never_returns_key_and_create_returns_new_identity(self):
        self.add_source()
        status, preview = self.request("/repeats/prepare", self.body())
        self.assertEqual(status, 200)
        self.assertTrue(preview["connections"][0]["saved"])
        self.assertNotIn("synthetic-current-key", json.dumps(preview))
        self.assertNotIn("api_key", json.dumps(preview))
        status, created = self.request("/repeats", self.body())
        self.assertEqual(status, 201)
        self.assertEqual(created["task_id"], "new-record")
        self.assertNotIn("synthetic-current-key", json.dumps(created))

    def test_http_missing_active_and_malformed_sources_have_correct_status(self):
        source = self.add_source()
        self.assertEqual(self.request("/repeats/prepare", self.body(source_id="missing"))[0], 404)
        source.status = "running"
        self.assertEqual(self.request("/repeats", self.body())[0], 409)
        self.assertEqual(self.request("/repeats/prepare", self.body(source_kind=[]))[0], 400)
        self.assertEqual(self.managers["task"].created, [])

    def test_legacy_retry_uses_same_new_risk_and_lifecycle_gate(self):
        source = self.add_source(url="https://production.invalid/v1")
        self.keys[source.config["url"]] = "synthetic-current"
        self.assertEqual(self.request("/task/source?retry", {})[0], 400)
        self.assertEqual(self.request("/task/source?retry", {"risk_acknowledged": True})[0], 201)
        source.status = "running"
        self.assertEqual(self.request("/task/source?retry", {"risk_acknowledged": True})[0], 409)


class FingerprintFrozenMetadataTests(unittest.TestCase):
    def test_service_keeps_exact_internal_protocol_and_public_binding_across_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = FingerprintManager(folder)
            protocol = fingerprint_protocol.make_protocol({"preset": "quick", "samples_per_cell": 10, "concurrency": 2, "timeout_seconds": 5})
            binding = {"url": "http://127.0.0.1:9001/v1", "connection_id": "a" * 24}
            config = {"mode": "selfcheck", "name": "合成指纹", "risk_acknowledged": True,
                      "endpoint": {"url": binding["url"], "model": "synthetic-model", "api_key": "synthetic-frozen-key"}}
            with patch.object(threading.Thread, "start"):
                run = manager.create(config, frozen_protocol=protocol, connection_binding=binding)
            self.assertEqual(run["protocol"], protocol)
            self.assertEqual(run["connection_binding"], binding)
            self.assertTrue(run["key_required"])
            self.assertNotIn("synthetic-frozen-key", json.dumps(run))
            # The synthetic persistence check deliberately did not start a
            # scheduler. There is therefore no live thread to join on close.
            manager._thread = None
            manager.close()
            restored = FingerprintManager(folder)
            try:
                snapshot = restored.get(run["id"])
                self.assertEqual(snapshot["connection_binding"], binding)
                self.assertEqual(snapshot["protocol"], protocol)
                self.assertTrue(snapshot["key_required"])
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
