"""覆盖率验证只在临时项目内运行，保留根目录原有产物。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from coverage import Coverage, CoverageData


PROJECT_DIR = Path(__file__).resolve().parent.parent


class ArtifactPathTests(unittest.TestCase):
    def test_coverage_configuration_preserves_sources_and_thresholds(self):
        coverage = Coverage(config_file=str(PROJECT_DIR / "pyproject.toml"))
        self.assertEqual(coverage.get_option("run:source"),
                         ["task_status", "preflight", "quality_report", "routing_compare"])
        self.assertEqual(coverage.get_option("report:fail_under"), 75)
        for option in ("run:data_file", "json:output", "html:directory", "xml:output", "lcov:output"):
            with self.subTest(option=option):
                self.assertEqual(Path(coverage.get_option(option)).parts[:2], ("artifacts", "coverage"))

    def test_importing_threshold_checker_does_not_run_commands(self):
        spec = importlib.util.spec_from_file_location("coverage_checker", PROJECT_DIR / "tools" / "check_coverage.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(subprocess, "run") as command, \
                mock.patch.object(Path, "mkdir") as mkdir:
            spec.loader.exec_module(module)
        command.assert_not_called()
        mkdir.assert_not_called()
        self.assertEqual(module.THRESHOLDS, {
            "task_status.py": 90, "preflight.py": 60,
            "quality_report.py": 90, "routing_compare.py": 90,
        })

    def run_checker(self, *, incomplete=None):
        with tempfile.TemporaryDirectory(prefix="coverage-artifacts-") as folder:
            project = (Path(folder) / "project").resolve()
            caller = (Path(folder) / "caller").resolve()
            destination = project / "artifacts" / "coverage"
            (project / "tools").mkdir(parents=True)
            destination.mkdir(parents=True)
            caller.mkdir()
            shutil.copy2(PROJECT_DIR / "pyproject.toml", project / "pyproject.toml")
            checker = project / "tools" / "check_coverage.py"
            shutil.copy2(PROJECT_DIR / "tools" / "check_coverage.py", checker)
            # 根目录与调用目录里故意保留不能被覆盖或误读的旧产物。
            for root in (project, caller):
                (root / ".coverage").write_bytes(b"existing coverage data")
                (root / "coverage.json").write_text("existing report", encoding="utf-8")
            data = CoverageData(basename=str(destination / ".coverage"))
            measured = {}
            for name in ("task_status", "preflight", "quality_report", "routing_compare"):
                module = project / f"{name}.py"
                module.write_text("FIRST = 1\nSECOND = 2\n", encoding="utf-8")
                measured[str(module)] = {1} if name == incomplete else {1, 2}
            data.add_lines(measured)
            data.write()
            result = subprocess.run(
                [sys.executable, str(checker)], cwd=caller,
                env={**os.environ, "COVERAGE_FILE": str(caller / ".coverage")},
                capture_output=True, text=True, timeout=30,
            )
            for root in (project, caller):
                self.assertEqual((root / ".coverage").read_bytes(), b"existing coverage data")
                self.assertEqual((root / "coverage.json").read_text(encoding="utf-8"), "existing report")
            report = json.loads((destination / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual(set(report["files"]), {f"{name}.py" for name in (
                "task_status", "preflight", "quality_report", "routing_compare")})
            return result

    def test_checker_uses_project_artifacts_from_another_working_directory(self):
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("routing_compare.py: 100.0% / minimum 90%", result.stdout)

    def test_per_module_floor_still_rejects_low_coverage(self):
        result = self.run_checker(incomplete="preflight")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("preflight.py: 50.0% / minimum 60%", result.stdout)
        self.assertIn("Coverage decreased: preflight.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
