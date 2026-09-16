"""Enforce the measured per-module coverage floor, not only the project average."""
import json
import subprocess
import sys
subprocess.run([sys.executable,'-m','coverage','json','-o','coverage.json'],check=True)
report=json.load(open('coverage.json'))
thresholds={'task_status.py':90,'preflight.py':60,'quality_report.py':90,'routing_compare.py':90}
failed=[]
for filename, minimum in thresholds.items():
    percent=report['files'][filename]['summary']['percent_covered']
    print(f'{filename}: {percent:.1f}% / minimum {minimum}%')
    if percent<minimum:failed.append(filename)
if failed:raise SystemExit('Coverage decreased: '+', '.join(failed))
