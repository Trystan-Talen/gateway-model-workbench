"""Exercise the double-click launcher without production data or a real service."""
import importlib.util
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class RuntimeCheckTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="runtime-check-")
        self.addCleanup(self.folder.cleanup)
        self.project = Path(self.folder.name) / "中文环境 with spaces"
        self.environment = self.project / ".venv"
        self.environment.mkdir(parents=True)
        self.config = self.environment / "pyvenv.cfg"
        self.config.write_text("home = /independent/python\nversion = 3.11.15\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("launcher_runtime_test", ROOT / "tools" / "check_runtime.py")
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)

    def check(self, *, actual=(3, 11), prefix=None, base_prefix="/independent/python", imports=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.helper.sys, "version_info", actual + (15, "final", 0)))
            stack.enter_context(mock.patch.object(self.helper.sys, "version", ".".join(map(str, actual)) + ".15"))
            stack.enter_context(mock.patch.object(self.helper.sys, "prefix", str(prefix or self.environment)))
            stack.enter_context(mock.patch.object(self.helper.sys, "base_prefix", str(base_prefix)))
            importer = stack.enter_context(mock.patch.object(self.helper.importlib, "import_module", side_effect=imports))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = self.helper.check_runtime(self.project)
        return status, stdout.getvalue(), stderr.getvalue(), importer

    def test_valid_environment_checks_only_runtime_dependencies(self):
        status, stdout, stderr, importer = self.check()
        self.assertEqual(status, 0, stderr)
        self.assertIn("3.11.15", stdout)
        self.assertEqual(importer.call_args_list, [mock.call("httpx"), mock.call("h2"), mock.call("keyring")])

    def test_configuration_interpreter_version_mismatch_is_rejected(self):
        status, _, stderr, importer = self.check(actual=(3, 12))
        self.assertEqual(status, 1)
        self.assertIn("版本不一致", stderr)
        importer.assert_not_called()

    def test_python_older_than_supported_minimum_is_rejected(self):
        self.config.write_text("version = 3.10.15\n", encoding="utf-8")
        status, _, stderr, importer = self.check(actual=(3, 10))
        self.assertEqual(status, 1)
        self.assertIn("3.11", stderr)
        importer.assert_not_called()

    def test_another_project_or_base_interpreter_is_rejected(self):
        for options in ({"prefix": self.project / "another-venv"}, {"base_prefix": self.environment}):
            with self.subTest(options=options):
                status, _, stderr, importer = self.check(**options)
                self.assertEqual(status, 1)
                self.assertIn("本项目", stderr)
                importer.assert_not_called()

    def test_missing_or_malformed_configuration_is_rejected(self):
        for content in ("home = /independent/python\n", "version = nonsense\n", "version = 3.1wrong\n"):
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                status, _, stderr, importer = self.check()
                self.assertEqual(status, 1)
                self.assertIn("有效版本", stderr)
                importer.assert_not_called()
        self.config.unlink()
        status, _, stderr, importer = self.check()
        self.assertEqual(status, 1)
        self.assertIn("pyvenv.cfg", stderr)
        importer.assert_not_called()

    def test_permission_denied_is_reported_without_importing_application(self):
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(1, "Operation not permitted")):
            status, _, stderr, importer = self.check()
        self.assertEqual(status, 1)
        self.assertIn("Operation not permitted", stderr)
        importer.assert_not_called()

    def test_missing_dependency_reports_locked_install_command(self):
        for missing in ("httpx", "h2", "keyring"):
            with self.subTest(missing=missing):
                def import_dependency(name):
                    if name == missing:
                        raise ModuleNotFoundError(f"No module named '{name}'")
                    return object()
                status, _, stderr, _ = self.check(imports=import_dependency)
                self.assertEqual(status, 1)
                self.assertIn(missing, stderr)
                self.assertIn("requirements-dev.lock", stderr)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="launcher-")
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.project = self.base / "中文项目 with spaces"
        self.project.mkdir()
        self.launcher = self.project / "启动正式版.command"
        shutil.copy2(ROOT / self.launcher.name, self.launcher)
        (self.project / "tools").mkdir()
        shutil.copy2(ROOT / "tools" / "check_runtime.py", self.project / "tools" / "check_runtime.py")
        # A production sentinel proves this test never needs real accounts or credentials.
        self.data = self.project / "data"
        self.data.mkdir()
        self.sentinel = self.data / "accounts.json"
        self.sentinel.write_text("synthetic production sentinel", encoding="utf-8")
        self.bin = self.base / "command substitutes"
        self.bin.mkdir()
        self.trace = self.base / "trace"
        self.trace.mkdir()
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
            "TEST_TRACE": str(self.trace),
            "TEST_CHECK_STATUS": "0",
            "TEST_HEALTH": "1",
            "TEST_STOP_ON_OPEN": "1",
            "ROUTE_TEST_PORT": "48765",
        }
        self.write_command("lsof", 'printf "called\\n" >> "$TEST_TRACE/lsof"\nexit 1\n')
        self.write_command("curl", '''printf 'called\\n' >> "$TEST_TRACE/curl"
if [ "$TEST_HEALTH" = 1 ] && [ -f "$TEST_TRACE/server-pid" ]; then
  printf '{"ok":true}\\n'
else
  exit 1
fi
''')
        self.write_command("open", '''printf '%s\\n' "$@" >> "$TEST_TRACE/open"
if [ "$TEST_STOP_ON_OPEN" = 1 ]; then : > "$TEST_TRACE/stop"; fi
''')
        self.write_command("sleep", '/bin/sleep 0.02\n')
        self.write_command("seq", 'for ((i=1; i<=20; i++)); do printf "%s\\n" "$i"; done\n')

    def write_command(self, name, body, directory=None):
        path = (directory or self.bin) / name
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def install_interpreter(self):
        environment = self.project / ".venv"
        (environment / "bin").mkdir(parents=True)
        (environment / "pyvenv.cfg").write_text("version = 3.11.15\n", encoding="utf-8")
        return self.write_command("python", '''case "$*" in
  *check_runtime.py*)
    printf '%s\\n' "$@" > "$TEST_TRACE/check-args"
    printf '%s|%s|%s\\n' "${PYTHONHOME-unset}" "${PYTHONPATH-unset}" "${__PYVENV_LAUNCHER__-unset}" > "$TEST_TRACE/environment"
    pwd -P > "$TEST_TRACE/cwd"
    if [ "$TEST_CHECK_STATUS" != 0 ]; then
      printf '%s\\n' "${TEST_CHECK_ERROR:-PermissionError: Operation not permitted: .venv/pyvenv.cfg}" >&2
      exit "$TEST_CHECK_STATUS"
    fi
    printf '运行环境正常：Python 3.11.15\\n'
    exit 0
    ;;
esac
printf '%s\\n' "$@" > "$TEST_TRACE/server-args"
printf '%s\\n' "$$" > "$TEST_TRACE/server-pid"
trap 'printf "terminated\\n" > "$TEST_TRACE/terminated"; exit 0' TERM INT HUP
while [ ! -f "$TEST_TRACE/stop" ]; do /bin/sleep 0.02; done
printf 'finished\\n' > "$TEST_TRACE/finished"
''', environment / "bin")

    def invoke(self, *args):
        result = subprocess.run(
            ["/bin/bash", str(self.launcher), *args], cwd=self.base, env=self.env,
            input="\n", capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(self.sentinel.read_text(), "synthetic production sentinel")
        self.assertEqual(list(self.data.iterdir()), [self.sentinel])
        return result

    def assert_no_service_actions(self):
        for name in ("server-pid", "server-args", "lsof", "curl", "open"):
            self.assertFalse((self.trace / name).exists(), name)

    def test_check_missing_environment_does_not_start_or_pause(self):
        result = self.invoke("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("环境", result.stdout + result.stderr)
        self.assertNotIn("按任意键", result.stdout + result.stderr)
        self.assert_no_service_actions()

    def test_check_dangling_interpreter_does_not_start(self):
        directory = self.project / ".venv" / "bin"
        directory.mkdir(parents=True)
        (directory / "python").symlink_to(self.base / "missing-interpreter")
        result = self.invoke("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_service_actions()

    def test_check_handles_interpreter_permission_failure_without_starting(self):
        self.install_interpreter()
        self.env["TEST_CHECK_STATUS"] = "1"
        result = self.invoke("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("权限", result.stdout + result.stderr)
        self.assertNotIn("按任意键", result.stdout + result.stderr)
        self.assert_no_service_actions()

    def test_failed_normal_preflight_does_not_inspect_or_stop_existing_service(self):
        self.install_interpreter()
        self.env["TEST_CHECK_STATUS"] = "9"
        self.env["TEST_CHECK_ERROR"] = "synthetic runtime initialization failure"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_service_actions()

    def test_check_cleans_python_environment_and_never_starts_service(self):
        self.install_interpreter()
        self.env.update(PYTHONHOME="/synthetic/bad/home", PYTHONPATH="/synthetic/bad/path",
                        __PYVENV_LAUNCHER__="/synthetic/bad/launcher")
        result = self.invoke("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.trace / "environment").read_text().strip(), "unset|unset|unset")
        arguments = (self.trace / "check-args").read_text().splitlines()
        self.assertEqual(arguments[:2], ["-E", "-s"])
        self.assertEqual(Path(arguments[2]).name, "check_runtime.py")
        self.assertEqual(Path((self.trace / "cwd").read_text().strip()), self.project.resolve())
        self.assertNotIn("按任意键", result.stdout + result.stderr)
        self.assert_no_service_actions()

    def test_normal_start_waits_for_health_then_opens_and_reaps_server(self):
        self.install_interpreter()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.trace / "check-args").exists())
        self.assertIn("route_test_ui.py", (self.trace / "server-args").read_text())
        self.assertTrue((self.trace / "curl").exists())
        self.assertIn("http://127.0.0.1:48765/", (self.trace / "open").read_text())
        self.assertTrue((self.trace / "finished").exists())
        self.assert_server_gone()

    def test_unhealthy_start_cleans_up_child_without_opening_browser(self):
        self.install_interpreter()
        self.env.update(TEST_HEALTH="0", TEST_STOP_ON_OPEN="0")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.trace / "open").exists())
        self.assertTrue((self.trace / "terminated").exists())
        self.assert_server_gone()

    def assert_server_gone(self):
        pid = int((self.trace / "server-pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
