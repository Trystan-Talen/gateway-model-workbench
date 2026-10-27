"""Check the desktop launcher's environment without importing the application."""
import importlib
from pathlib import Path
import re
import sys


def check_runtime(project_dir=None):
    root = Path(project_dir) if project_dir is not None else Path(__file__).resolve().parents[1]
    environment = root / ".venv"
    try:
        config = (environment / "pyvenv.cfg").read_text(encoding="utf-8")
        match = re.search(r"^version\s*=\s*(\d+)\.(\d+)(?:\.|\s|$)", config, re.MULTILINE)
        if not match:
            raise ValueError("pyvenv.cfg（虚拟环境配置）缺少有效版本，请重建运行环境")
        configured = tuple(map(int, match.groups()))
        actual = sys.version_info[:2]
        if actual < (3, 11):
            raise ValueError("需要 Python（解释器）3.11 或更新版本")
        if configured != actual:
            raise ValueError(
                f"虚拟环境版本不一致：配置为 {configured[0]}.{configured[1]}，"
                f"实际为 {actual[0]}.{actual[1]}；请备份后重建 .venv（运行环境）"
            )
        if Path(sys.prefix).resolve() != environment.resolve() or sys.prefix == sys.base_prefix:
            raise ValueError("当前解释器未使用本项目的 .venv（运行环境），请重建环境")
        for name in ("httpx", "h2", "keyring"):
            try:
                importlib.import_module(name)
            except ImportError as exc:
                raise ValueError(
                    f"运行依赖 {name}（程序包）不可用：{exc}。请执行 "
                    ".venv/bin/python -m pip install -r requirements-dev.lock（安装锁定依赖）"
                ) from exc
    except (OSError, ValueError) as exc:
        print(f"运行环境检查失败：{exc}", file=sys.stderr)
        return 1
    print(f"运行环境检查通过：Python（解释器）{sys.version.split()[0]}，必要依赖可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(check_runtime())
