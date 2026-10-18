#!/usr/bin/env python3
"""Install the unified TikTok listing-to-video skill locally."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

NAME = "manage-tiktok-shop-listings"


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装 TikTok Shop 上架到视频的一体化 Skill")
    parser.add_argument("--codex-dir", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--replace", action="store_true", help="备份已有版本后更新")
    parser.add_argument("--skip-deps", action="store_true", help="仅复制技能；已有 Python 依赖时使用")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10) or (os.name != "posix" and not args.skip_deps):
        parser.error("需要 Python 3.10+；Windows 请运行 install_workflow_windows.py")
    source = Path(__file__).resolve().parent / "skills" / NAME
    codex_dir = args.codex_dir.expanduser().resolve()
    target = codex_dir / "skills" / NAME
    backup = None
    if target.exists() or target.is_symlink():
        if not args.replace:
            parser.error("已安装同名技能；更新时加 --replace，会先备份现有版本")
        backup = codex_dir / "skill-backups" / (NAME + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup.parent.mkdir(parents=True, exist_ok=True)
        target.rename(backup)
    try:
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", ".DS_Store"))
        python = Path(sys.executable)
        if not args.skip_deps:
            runtime = target / ".venv"
            venv.EnvBuilder(with_pip=True).create(runtime)
            python = runtime / ("bin/python" if os.name == "posix" else "Scripts/python.exe")
            subprocess.run([str(python), "-m", "pip", "install", "-r", str(target / "requirements.txt")], check=True)
    except Exception:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        if backup is not None:
            backup.rename(target)
        raise
    entrypoint = "scripts/windows.py" if os.name == "nt" else "scripts/run.py"
    print(json.dumps({"installed": str(target), "python": str(python), "backup": str(backup) if backup else None, "next": f"在 Codex 中使用 manage-tiktok-shop-listings 并提供单个产品资料目录；先运行 {entrypoint} doctor"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
