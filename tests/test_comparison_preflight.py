"""Comparison draft estimates use shared connections and every serial group."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import route_test_ui as ui
from connection_profiles import ConnectionStore


class ComparisonPreflightTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.keys = {}
        self.store = ConnectionStore(
            Path(self.folder.name) / "connections.json",
            load_key=lambda ref: self.keys.get(ref, ""),
            save_key=lambda ref, key: self.keys.update({ref: key}) or True,
            delete_key=lambda ref: bool(self.keys.pop(ref, None)),
            available=lambda: True,
        )
        for name, value in (("CONNECTIONS", self.store), ("_load_api_key", lambda ref: "")):
            patcher = patch.object(ui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def draft(self, **shared):
        return {
            "name": "合成对照",
            "shared_config": {
                "url": "http://127.0.0.1:9011/v1", "model": "synthetic-model",
                "profile": "cache", "stream": False, "read_timeout": 2,
                **shared,
            },
            "groups": [
                {"id": "a", "role": "baseline", "rpm": 75, "concurrency": 8, "iterations": 21},
                {"id": "b", "role": "comparison", "rpm": 150, "concurrency": 16, "iterations": 42},
            ],
        }

    def post(self, draft):
        handler = ui.Handler.__new__(ui.Handler)
        handler.path = "/preflight"
        handler.headers = {}
        captured = {}
        with patch.object(ui, "_current_user", return_value={"role": "admin"}), \
                patch.object(ui.Handler, "_begin"), patch.object(ui.Handler, "_note"), \
                patch.object(ui.Handler, "_body", return_value={"kind": "comparison", "config": draft}), \
                patch.object(ui.Handler, "_json", lambda self, data, status=200, headers=None:
                             captured.update(status=status, payload=data)), \
                patch.object(ui.COMPARISONS, "create") as create, \
                patch.object(ui, "save_config") as save:
            handler.do_POST()
        create.assert_not_called()
        save.assert_not_called()
        self.assertEqual(captured["status"], 200, captured)
        return captured["payload"]

    def test_endpoint_sums_each_groups_requests_duration_and_disk_without_a_key(self):
        draft = self.draft()
        original = copy.deepcopy(draft)
        result = self.post(draft)
        self.assertNotIn("unavailable", result)
        self.assertEqual(result["planned_requests"], 63)
        self.assertEqual(result["min_seconds"], 33.6)
        self.assertEqual(result["estimated_bytes"], 63 * 420)
        self.assertEqual(result["target"]["host"], "127.0.0.1")
        self.assertFalse(result["requires_confirmation"])
        self.assertEqual(draft, original)
        # Actual creation still requires a usable credential.
        with self.assertRaisesRegex(ValueError, "API Key"):
            ui.validate_comparison_config(draft)

    def test_raw_capture_estimate_covers_all_four_groups(self):
        draft = self.draft(capture_raw=True)
        draft["groups"].extend([
            {"id": "c", "rpm": 60, "iterations": 10},
            {"id": "d", "rpm": 300, "iterations": 25},
        ])
        result = self.post(draft)
        self.assertEqual(result["planned_requests"], 98)
        self.assertEqual(result["min_seconds"], 48.6)
        self.assertEqual(result["estimated_bytes"], 98 * 2600)
        self.assertEqual(sum(f["code"] == "capture_raw" for f in result["findings"]), 1)

    def test_candidate_only_pressure_risks_are_not_lost(self):
        draft = self.draft()
        draft["groups"][1].update(rpm=6000, concurrency=1)
        result = self.post(draft)
        self.assertEqual(result["level"], "warn")
        codes = {item["code"] for item in result["findings"]}
        self.assertTrue({"rpm_high", "concurrency_below_plan"}.issubset(codes))
        self.assertFalse(result["requires_confirmation"])

    def test_public_target_confirmation_is_shared_and_not_duplicated(self):
        result = self.post(self.draft(url="https://synthetic-production.invalid/v1"))
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["level"], "risk")
        self.assertEqual(sum(f["code"] == "production_target" for f in result["findings"]), 1)

    def test_partial_or_invalid_groups_degrade_instead_of_returning_a_run_estimate(self):
        cases = ({}, {"shared_config": {}}, {**self.draft(), "groups": []},
                 {**self.draft(), "groups": [{"rpm": 0}, {}]},
                 {**self.draft(), "groups": [{"role": "baseline"}, {"role": "baseline"}]})
        for draft in cases:
            with self.subTest(draft=draft):
                result = self.post(draft)
                self.assertIn("unavailable", result)
                self.assertNotIn("planned_requests", result)
                self.assertFalse(result["requires_confirmation"])
        result = self.post({**self.draft(), "groups": [{"rpm": 0}, {}]})
        self.assertIn("实验组 1", result["unavailable"])
        self.assertNotIn("请填写网关 URL", result["unavailable"])

    def test_named_shared_connection_is_resolved_without_leaking_or_saving_secret(self):
        profile = self.store.save(name="合成连接", url="http://127.0.0.1:9011/v1",
                                  api_key="synthetic-preflight-secret")
        draft = self.draft(connection_id=profile["id"], api_key="")
        result = self.post(draft)
        self.assertEqual(result["planned_requests"], 63)
        self.assertNotIn("synthetic-preflight-secret", json.dumps(result))
        self.assertEqual(draft["shared_config"]["api_key"], "")
        self.assertEqual(self.store.reveal(profile["id"]), "synthetic-preflight-secret")

    def test_repeat_and_creation_use_the_same_comparison_estimate(self):
        draft = self.draft(api_key="synthetic-only", capture_raw=True)
        validated = ui.validate_comparison_config(draft)
        self.assertEqual(self.post(draft), ui._repeat_preflight("comparison", validated))


if __name__ == "__main__":
    unittest.main()
