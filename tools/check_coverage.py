"""Enforce the measured per-module coverage floor, not only the project average."""
import json
from pathlib import Path
import subprocess
import sys

PROJECT_DIR = Path(__file__).resolve().parent.parent
THRESHOLDS = {
    "task_status.py": 90, "preflight.py": 60,
    "quality_report.py": 90, "routing_compare.py": 90,
}


def main():
    destination = PROJECT_DIR / "artifacts" / "coverage"
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / "coverage.json"
    subprocess.run([
        sys.executable, "-m", "coverage", "json",
        f"--rcfile={PROJECT_DIR / 'pyproject.toml'}",
        f"--data-file={destination / '.coverage'}",
        "-o", str(report_path),
    ], cwd=PROJECT_DIR, check=True)
    with report_path.open(encoding="utf-8") as source:
        report = json.load(source)
    failed = []
    for filename, minimum in THRESHOLDS.items():
        percent = report["files"][filename]["summary"]["percent_covered"]
        print(f"{filename}: {percent:.1f}% / minimum {minimum}%")
        if percent < minimum:
            failed.append(filename)
    if failed:
        raise SystemExit("Coverage decreased: " + ", ".join(failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
