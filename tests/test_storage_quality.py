"""Storage optimizations preserve contracts using only temporary synthetic data."""
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fingerprint_service
import storage
from connection_profiles import ConnectionStore
from history_store import HistoryStore


class StorageSummaryTests(unittest.TestCase):
    def test_summary_scans_once_and_preserves_order_counts_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            names = ("run_a.jsonl", "quality_run_b.json", "run_c.jsonl")
            for index, name in enumerate(names, start=1):
                path = base / name
                path.write_bytes(b"x" * index)
                os.utime(path, (index, index))
            (base / "accounts.json").write_text("[]")
            (base / "run_directory.jsonl").mkdir()
            with patch.object(storage.os, "scandir", wraps=os.scandir) as scans:
                result = storage.storage_summary(base, {names[0]: "合成任务"}, orphan_scan_limit=1)
            self.assertEqual(scans.call_count, 1)
            self.assertEqual(result["artifact_count"], 3)
            self.assertEqual(result["total_bytes"], 6)
            self.assertEqual(result["referenced_bytes"], 1)
            self.assertEqual(result["orphan_count"], 2)
            self.assertEqual(result["orphan_bytes"], 5)
            self.assertEqual(result["largest"], {"name": names[2], "size": 3})
            self.assertEqual(result["orphans"], [{"name": names[2], "path": str(base / names[2]), "size": 3, "mtime": 3}])

    def test_path_references_and_empty_limit_keep_existing_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run_a.jsonl"
            path.write_bytes(b"abcd")
            result = storage.storage_summary(directory, {str(path): "合成任务"}, orphan_scan_limit=0)
            self.assertEqual(result["total_bytes"], 4)
            # Capacity accounting uses file-name keys; orphan exclusion also
            # accepts full paths, as it did before the scans were combined.
            self.assertEqual(result["referenced_bytes"], 0)
            self.assertEqual(result["orphan_count"], 0)
            self.assertEqual(result["orphan_bytes"], 0)
            self.assertEqual(result["orphans"], [])

    def test_unavailable_directory_keeps_empty_summary(self):
        with patch.object(storage.os, "scandir", side_effect=OSError("synthetic failure")) as scans:
            result = storage.storage_summary("synthetic-unavailable", {})
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(result["artifact_count"], 0)
        self.assertEqual(result["orphans"], [])
        self.assertIsNone(result["largest"])


class ConnectionReadTests(unittest.TestCase):
    def test_reveal_reads_metadata_once_and_uses_fresh_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = "a" * 24
            path = Path(directory) / "connections.json"
            path.write_text(json.dumps([{"id": identifier, "name": "合成连接", "url": "https://synthetic.invalid/v1", "updated_at": 1}]))
            keys = {"connection:" + identifier: "synthetic-first"}
            store = ConnectionStore(path, load_key=lambda ref: keys.get(ref, ""),
                                    save_key=lambda *args: True, delete_key=lambda *args: True, available=lambda: True)
            with patch.object(store, "_read", wraps=store._read) as reads:
                self.assertEqual(store.reveal(identifier), "synthetic-first")
                self.assertEqual(reads.call_count, 1)
            keys["connection:" + identifier] = "synthetic-second"
            self.assertEqual(store.reveal(identifier), "synthetic-second")
            keys.clear()
            with self.assertRaisesRegex(ValueError, "密钥无法读取"):
                store.reveal(identifier)
            with self.assertRaisesRegex(ValueError, "不一致"):
                store.resolve(identifier, "https://different.invalid/v1")


class HistoryCacheTests(unittest.TestCase):
    def test_same_database_path_aliases_share_committed_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "alias").mkdir()
            first = HistoryStore(base / "index.json")
            alias = HistoryStore(base / "alias" / ".." / "index.json")
            first.write({"tasks": [{"id": "first"}]})
            alias.write({"tasks": [{"id": "second"}]})
            first.write({"tasks": [{"id": "first"}]})
            self.assertEqual(first.read()["tasks"], [{"id": "first"}])
            self.assertEqual(alias.read()["tasks"], [{"id": "first"}])


class FingerprintSamplePageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = fingerprint_service.FingerprintManager(self.directory.name)
        self.addCleanup(self.manager.close)
        self.manager.restore()
        self.identifier = "b" * 32
        self.run = {"id": self.identifier, "status": "finished"}
        self.manager._runs[self.identifier] = self.run
        self.path = self.manager._samples_path(self.identifier)

    @staticmethod
    def with_empty_legacy_timing(row):
        return {**row, "timing": {
            "version": 1, "clock": "client", "timezone": "Asia/Singapore", "source": "legacy",
            "scheduled_at": None, "enqueued_at": None, "started_at": None,
            "first_token_at": None, "ended_at": None, "queue_ms": None,
            "schedule_lag_ms": None, "ttft_ms": None, "total_ms": None,
        }}

    def write(self, rows):
        self.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_pages_match_full_stable_sort_for_shuffled_and_duplicate_indexes(self):
        rows = [{"index": index % 17 - 3, "cell_id": "synthetic", "sequence": index} for index in range(170)]
        rows.extend([{"index": True, "cell_id": "synthetic", "sequence": 170},
                     {"index": 10 ** 80, "cell_id": "synthetic", "sequence": 171}])
        random.Random(731).shuffle(rows)
        self.write(rows)
        expected = [self.with_empty_legacy_timing(row) for row in sorted(rows, key=lambda row: row["index"])]
        for offset, limit in ((0, 1), (0, 100), (3, 7), (19, 25), (169, 3), (172, 100), (200, 1), (0, 500)):
            with self.subTest(offset=offset, limit=limit):
                self.assertEqual(self.manager.samples(self.identifier, offset, limit),
                                 {"samples": expected[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit})
        self.assertNotIn("storage_warning", self.run)

    def test_damaged_rows_keep_warning_and_valid_total_for_page_and_full_read(self):
        valid = [{"index": 2, "cell_id": "synthetic"}, {"index": 1, "cell_id": "synthetic"}]
        self.path.write_text(json.dumps(valid[0]) + '\n{broken\nnull\n{"index":"bad","cell_id":"synthetic"}\n' + json.dumps(valid[1]) + '\n', encoding="utf-8")
        page = self.manager.samples(self.identifier, limit=1)
        self.assertEqual(page["samples"], [self.with_empty_legacy_timing(valid[1])])
        self.assertEqual(page["total"], 2)
        self.assertIn("损坏或缺失", self.run["storage_warning"])
        samples, damaged = self.manager._read_samples(self.run)
        self.assertEqual(samples, [self.with_empty_legacy_timing(row) for row in valid])
        self.assertTrue(damaged)

    def test_missing_and_changed_sample_files_are_reread(self):
        self.assertEqual(self.manager.samples(self.identifier)["total"], 0)
        self.assertIn("storage_warning", self.run)
        first = {"index": 3, "cell_id": "synthetic"}
        second = {"index": 1, "cell_id": "synthetic"}
        self.write([first])
        self.assertEqual(self.manager.samples(self.identifier)["samples"], [self.with_empty_legacy_timing(first)])
        self.write([first, second])
        self.assertEqual(self.manager.samples(self.identifier)["samples"],
                         [self.with_empty_legacy_timing(row) for row in [second, first]])

    def test_recorded_timing_uses_whitelist_and_keeps_same_page_order(self):
        timing = {"version": 999, "clock": "server", "timezone": "arbitrary", "source": "recorded",
                  "started_at": "2026-10-15T12:00:00+08:00", "ended_at": "2026-10-15T12:00:01+08:00",
                  "total_ms": 900.5, "unrecognized_secret": "never-forward-this"}
        rows = [{"index": 2, "cell_id": "synthetic", "sequence": 0, "timing": timing},
                {"index": 1, "cell_id": "synthetic", "sequence": 1}]
        self.write(rows)
        expected = self.with_empty_legacy_timing(rows[0])
        expected["timing"].update(source="recorded", started_at="2026-10-15T12:00:00.000+08:00",
                                  ended_at="2026-10-15T12:00:01.000+08:00", total_ms=900.5)
        self.assertEqual(self.manager.samples(self.identifier, 1, 1),
                         {"samples": [expected], "total": 2, "offset": 1, "limit": 1})
        self.assertEqual(self.manager._read_samples(self.run),
                         ([expected, self.with_empty_legacy_timing(rows[1])], False))
        self.assertNotIn("storage_warning", self.run)

    def test_invalid_paging_arguments_keep_validation(self):
        self.write([])
        for offset, limit in ((True, 10), (-1, 10), (1.0, 10), (0, False), (0, 0), (0, 501), (0, 1.0)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(ValueError):
                self.manager.samples(self.identifier, offset, limit)


if __name__ == "__main__":
    unittest.main()
