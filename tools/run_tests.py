"""Run backend regressions with isolated data and persistent test evidence."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _TestOutput:
    def __init__(self, console, log):
        self.console = console
        self.log = log

    def write(self, text):
        self.console.write(text)
        return self.log.write(text)

    def flush(self):
        self.console.flush()
        self.log.flush()


def run_tests(start_directory="tests", pattern="test*.py", verbosity=1):
    base = Path(os.environ.get("TEST_ARTIFACT_DIR", PROJECT_ROOT / "artifacts" / "tests")).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    output = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=base))
    print(f"后端回归输出：{output}", flush=True)
    started = time.monotonic()
    summary = {"status": "error", "tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    # Set isolation before discovery: test modules import the service at module scope.
    # Inherited production data paths must never be honored by a test invocation.
    data = None
    log_path = output / "tests.log"
    with log_path.open("w", encoding="utf-8") as log:
        log_path.chmod(0o600)
        stream = _TestOutput(sys.stderr, log)
        try:
            with tempfile.TemporaryDirectory(prefix="data-", dir=output) as data:
                os.environ["ROUTE_TEST_DATA_DIR"] = data
                os.environ["ROUTE_TEST_DISABLE_CREDENTIAL_STORE"] = "1"
                sys.path.insert(0, str(PROJECT_ROOT))
                suite = unittest.defaultTestLoader.discover(str(start_directory), pattern=pattern)
                result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
                summary.update(status="passed" if result.wasSuccessful() else "failed", tests=result.testsRun,
                               failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped))
                return 0 if result.wasSuccessful() else 1
        except KeyboardInterrupt:
            summary["status"] = "interrupted"
            return 130
        except Exception:
            summary["errors"] = 1
            traceback.print_exc(file=stream)
            return 1
        finally:
            summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
            summary["data_removed_after_run"] = data is None or not Path(data).exists()
            summary_path = output / "summary.json"
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            summary_path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-directory", default=str(PROJECT_ROOT / "tests"))
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return run_tests(args.start_directory, args.pattern, 2 if args.verbose else 1)


if __name__ == "__main__":
    raise SystemExit(main())
