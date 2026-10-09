"""The regression entry point must isolate imports from production data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestRunnerIsolationTests(unittest.TestCase):
    def invoke(self, directory, body, missing_directory=False):
        base = Path(directory)
        suite = base / "suite"
        suite.mkdir()
        (suite / "test_synthetic.py").write_text(body, encoding="utf-8")
        production = base / "production"
        production.mkdir()
        sentinel = production / "accounts.json"
        sentinel.write_text("synthetic sentinel: must not be opened", encoding="utf-8")
        artifacts = base / "artifacts"
        env = {**os.environ, "ROUTE_TEST_DATA_DIR": str(production), "TEST_ARTIFACT_DIR": str(artifacts)}
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "run_tests.py"),
                                 "--start-directory", str(base / "missing" if missing_directory else suite)], cwd=base, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(list(production.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), "synthetic sentinel: must not be opened")
        runs = list(artifacts.iterdir())
        self.assertEqual(len(runs), 1)
        self.assertEqual({p.name for p in runs[0].iterdir()}, {"tests.log", "summary.json"})
        return result, json.loads((runs[0] / "summary.json").read_text()), runs[0]

    def test_imports_use_isolated_data_before_service_initialization(self):
        body = '''import os
from pathlib import Path
import unittest
import route_test_ui as ui
class SyntheticTest(unittest.TestCase):
    def test_data(self):
        self.assertIn("data-", Path(ui.BASE_DIR).name)
        self.assertTrue(Path(ui.BASE_DIR).is_dir())
        self.assertEqual(os.environ["ROUTE_TEST_DISABLE_CREDENTIAL_STORE"], "1")
        self.assertFalse(Path(ui.ACCOUNTS_FILE).exists())
'''
        with tempfile.TemporaryDirectory() as directory:
            result, summary, run = self.invoke(directory, body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(summary["tests"], 1)
            self.assertIn("OK", (run / "tests.log").read_text())

    def test_failures_preserve_evidence_and_remove_synthetic_state(self):
        body = '''import unittest
class SyntheticTest(unittest.TestCase):
    def test_failure(self):
        self.fail("synthetic failure")
'''
        with tempfile.TemporaryDirectory() as directory:
            result, summary, run = self.invoke(directory, body)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["failures"], 1)
            self.assertIn("synthetic failure", (run / "tests.log").read_text())

    def test_discovery_error_is_logged_and_counted_after_data_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            result, summary, run = self.invoke(directory, "", missing_directory=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(summary["status"], "error")
            self.assertEqual(summary["errors"], 1)
            self.assertTrue(summary["data_removed_after_run"])
            self.assertIn("ImportError", (run / "tests.log").read_text())


if __name__ == "__main__":
    unittest.main()
