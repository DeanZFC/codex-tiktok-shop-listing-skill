#!/usr/bin/env python3
"""Install the video skill locally; keep existing installs unless --replace."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

NAME = "swimwear-tiktok-video-workflow"


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装可迁移的视频制作 Skill")
    parser.add_argument("--codex-dir", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--replace", action="store_true", help="备份已有版本后更新")
    parser.add_argument("--skip-deps", action="store_true", help="仅复制技能；已有 Python 依赖时使用")
    args = parser.parse_args(argv)
    if os.name != "posix" or sys.version_info < (3, 10):
        parser.error("需要 macOS/Linux Python 3.10+；Windows 的命令行脚本需在 WSL2 运行")
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
            python = runtime / "bin" / "python"
            subprocess.run([str(python), "-m", "pip", "install", "-r", str(target / "requirements.txt")], check=True)
    except Exception:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        if backup is not None:
            backup.rename(target)
        raise
    print(json.dumps({"installed": str(target), "python": str(python), "backup": str(backup) if backup else None, "next": "在 Codex 中使用该 Skill 并提供单个产品资料目录；先运行 scripts/run.py doctor"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
