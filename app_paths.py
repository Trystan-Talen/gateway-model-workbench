"""Code and runtime data paths. Existing installations retain their data location."""
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
LEGACY_DATA = any((CODE_DIR / name).exists() for name in ('accounts.json', 'task_index.json', 'tasks.json'))
DATA_DIR = Path(os.environ.get('ROUTE_TEST_DATA_DIR') or (CODE_DIR if LEGACY_DATA and not (CODE_DIR / 'data' / '.migrated-from-root').exists() else CODE_DIR / 'data')).expanduser().resolve()
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
