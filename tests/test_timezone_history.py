"""Historical time views use Singapore time without rewriting original evidence."""
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fingerprint_service as fingerprint
import route_test_ui as ui
import routing_compare as routing
from tpm_service import FrozenRun, TpmManager
from tests.test_fingerprint_protocol import artifact as fingerprint_artifact


UTC_START = "2026-10-15T01:02:03.123Z"
SG_START = "2026-10-15T09:02:03.123+08:00"
UTC_END = "2026-10-15T01:02:04.456Z"
SG_END = "2026-10-15T09:02:04.456+08:00"


def request_time():
    return {"version": 1, "clock": "client", "timezone": "UTC", "source": "recorded",
            "started_at": UTC_START, "ended_at": UTC_END, "total_ms": 1200.5,
            "first_token_at": "2026-10-15T01:02:03.456", "ttft_ms": None}


def task_time():
    return {"version": 1, "clock": "client", "timezone": "UTC", "source": "recorded",
            "created_at": UTC_START, "started_at": UTC_START, "ended_at": UTC_END,
            "elapsed_ms": 1200.5}


def request_record(identifier=1):
    return {"item_id": identifier, "ts": 1792026123123, "completed": True, "status_code": 200,
            "total_ms": 1200.5, "timing": request_time(), "attempts": [{"timing": request_time()}]}


class TimezoneHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def check_request(self, timing):
        self.assertEqual(timing["timezone"], "Asia/Singapore")
        self.assertEqual(timing["started_at"], SG_START)
        self.assertEqual(timing["ended_at"], SG_END)
        self.assertEqual(timing["total_ms"], 1200.5)
        self.assertIsNone(timing["first_token_at"])
        self.assertEqual(datetime.fromisoformat(timing["started_at"]).timestamp(),
                         datetime.fromisoformat(UTC_START.replace("Z", "+00:00")).timestamp())

    def test_gateway_history_normalizes_recent_attempts_without_mutating_input_or_file(self):
        source = self.folder / "run_old.jsonl"
        row = request_record()
        source.write_text(json.dumps(row) + "\n")
        original_file = source.read_bytes()
        snapshot = {"id": "gateway-old", "name": "历史", "created": 1234.5, "status": "finished",
                    "timing": task_time(), "recent": [row], "total": 1, "done": 1,
                    "config": {"name": "历史", "mode": "smoke", "timing": {"started_at": UTC_START}}}
        original = copy.deepcopy(snapshot)
        with patch.object(ui, "BASE_DIR", str(self.folder)):
            result = ui._restore_task_snapshot(snapshot, str(source)).snap()
        self.assertEqual(result["timing"]["started_at"], SG_START)
        self.check_request(result["recent"][0]["timing"])
        self.check_request(result["recent"][0]["attempts"][0]["timing"])
        self.assertEqual(result["created"], 1234.5)
        self.assertEqual(result["recent"][0]["ts"], row["ts"])
        self.assertEqual(snapshot, original)
        self.assertEqual(source.read_bytes(), original_file)

    def test_comparison_and_quality_restore_nested_time_views_only(self):
        quality = {"id": "quality-old", "name": "历史质量", "created": 1234.5, "status": "finished",
                   "timing": task_time(), "config": {"resources": []}, "case_count": 0,
                   "resources": [{"name": "资源", "summary": {"created_at": UTC_START},
                                  "case_results": [{"timing": request_time(), "attempts": [{"timing": request_time()}]}]}]}
        source = self.folder / "quality_run_quality-old.json"
        source.write_text(json.dumps(quality))
        source_bytes = source.read_bytes()
        comparison = {"id": "comparison-old", "name": "历史对照", "created": 1234.5,
                      "status": "finished", "timing": task_time(),
                      "groups": [{"id": "group-old", "timing": task_time(),
                                  "summary": {"timing": task_time(), "recent": [request_record()]}}]}
        entries = {"tasks": [], "comparisons": [comparison], "quality_tests": [quality]}
        original = copy.deepcopy(entries)
        with patch.object(ui, "BASE_DIR", str(self.folder)), patch.object(ui.HistoryStore, "read", return_value=entries), \
             patch.object(ui, "read_saved_config", return_value={}), patch.object(ui.TPM, "restore"), \
             patch.dict(ui.MANAGER.tasks, {}, clear=True), patch.dict(ui.COMPARISONS.runs, {}, clear=True), \
             patch.dict(ui.QUALITY.tasks, {}, clear=True):
            ui.restore_history()
            result = ui.COMPARISONS.get("comparison-old").snap()
            self.assertEqual(result["groups"][0]["timing"]["started_at"], SG_START)
            self.check_request(result["groups"][0]["summary"]["recent"][0]["attempts"][0]["timing"])
            restored = ui.QUALITY.get("quality-old")
            self.assertEqual(restored.snap(include_details=False)["timing"]["started_at"], SG_START)
            self.assertEqual(restored.results[0]["summary"]["created_at"], SG_START)
            self.check_request(restored.results[0]["case_results"][0]["timing"])
        self.assertEqual(entries, original)
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_routing_history_report_and_request_page_keep_same_instants(self):
        side = {"name": "合成", "url": "https://mock.invalid/v1", "model": "mock", "records": 1}
        snapshot = {"id": "routing-old", "status": "finished", "created": 1234.5, "tier": 1000,
                    "timing": task_time(), "concurrency": {"baseline": 1, "candidate": 1},
                    "sides": {"baseline": side, "candidate": side},
                    "report": {"timing": task_time(), "stability": {}, "evidence": [{"timing": request_time()}]}}
        with patch.object(routing, "_results_dir", return_value=str(self.folder)):
            run = routing.RouteComparison.from_snapshot(snapshot)
            source = Path(run.out_paths["baseline"])
            source.write_text(json.dumps(request_record()) + "\n")
            original_file = source.read_bytes()
            self.check_request(run.snap()["report"]["evidence"][0]["timing"])
            page = run.request_page("baseline")
            self.check_request(page["items"][0]["timing"])
            self.check_request(page["items"][0]["attempts"][0]["timing"])
            self.assertEqual(source.read_bytes(), original_file)
        self.assertEqual(snapshot["timing"]["started_at"], UTC_START)

    def test_tpm_nested_calibration_report_and_structured_export_convert_history(self):
        identifier = "a" * 32
        row = {**request_record(), "id": identifier + "-1", "started_at": 1234.5}
        snapshot = {"id": identifier, "status": "finished", "created": 1234.5, "config": {},
                    "timing": task_time(), "recent_requests": [row], "request_count": 1,
                    "report": {"evidence": [{"timing": request_time()}]},
                    "calibration": {"samples": [{"timing": request_time()}]}}
        original = copy.deepcopy(snapshot)
        manager = TpmManager(self.folder, loop=lambda: None, acquire=lambda _: True,
                             release=lambda _: None, persist=lambda: True, resolve=lambda c: c)
        manager.runs[identifier] = FrozenRun(snapshot)
        source = self.folder / ("tpm_run_" + identifier + ".jsonl")
        source.write_text(json.dumps(row) + "\n")
        source_bytes = source.read_bytes()
        result = manager.export(identifier)
        self.check_request(result["calibration"]["samples"][0]["timing"])
        self.check_request(result["report"]["evidence"][0]["timing"])
        self.check_request(result["requests"][0]["attempts"][0]["timing"])
        self.assertEqual(result["requests"][0]["started_at"], 1234.5)
        self.assertEqual(snapshot, original)
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_fingerprint_new_save_and_old_reference_export_use_singapore_dates(self):
        manager = fingerprint.FingerprintManager(self.folder)
        self.addCleanup(manager.close)
        data = fingerprint_artifact(collected_at=UTC_START)
        data["timing"] = task_time()
        original = copy.deepcopy(data)
        reference = manager.import_reference(data, "合成参考", "", False)
        self.assertEqual(reference["artifact"]["collected_at"], SG_START)
        self.assertEqual(reference["artifact"]["timing"]["created_at"], SG_START)
        self.assertTrue(reference["created"].endswith("+08:00"))
        self.assertEqual(reference["artifact"]["protocol"], data["protocol"])
        self.assertEqual(data, original)
        path = manager._reference_path(reference["id"])
        legacy = json.loads(path.read_text())
        legacy["created"] = UTC_START
        legacy["artifact"]["collected_at"] = UTC_START
        legacy["artifact"]["timing"] = task_time()
        path.write_text(json.dumps(legacy))
        original_bytes = path.read_bytes()
        restored = fingerprint.FingerprintManager(self.folder)
        self.addCleanup(restored.close)
        result = restored.get_reference(reference["id"])
        self.assertEqual(result["created"], SG_START)
        self.assertEqual(result["artifact"]["collected_at"], SG_START)
        self.assertEqual(result["artifact"]["protocol"], data["protocol"])
        self.assertEqual(path.read_bytes(), original_bytes)
        run_id = "b" * 32
        restored._runs[run_id] = {"id": run_id, "status": "finished"}
        samples = restored._samples_path(run_id)
        row = {"index": 0, "cell_id": "synthetic", "started_at": UTC_START,
               "finished_at": UTC_END, "timing": request_time(), "raw": UTC_START}
        samples.write_text(json.dumps(row) + "\n")
        sample_bytes = samples.read_bytes()
        observed = restored.samples(run_id)["samples"][0]
        self.check_request(observed["timing"])
        self.assertEqual(observed["started_at"], SG_START)
        self.assertEqual(observed["finished_at"], SG_END)
        self.assertEqual(observed["raw"], UTC_START)
        self.assertEqual(samples.read_bytes(), sample_bytes)


if __name__ == "__main__":
    unittest.main()
