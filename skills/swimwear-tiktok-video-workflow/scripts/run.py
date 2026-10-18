#!/usr/bin/env python3
"""Portable product/task entry point. No login secrets are stored here."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys


def doctor():
    report = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "supported_runtime": os.name == "posix" and sys.version_info >= (3, 10),
        "Pillow": importlib.util.find_spec("PIL") is not None,
        "openai_optional": importlib.util.find_spec("openai") is not None,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "image_api_configured": bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL")),
        "browser_login": "Codex 在实际使用 toapi.top 时检查登录，需要时打开内置浏览器",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(report[k] for k in ("supported_runtime", "Pillow", "ffmpeg", "ffprobe")) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="从指定产品资料目录启动或恢复 TikTok 视频")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    start = sub.add_parser("start")
    start.add_argument("product", type=Path)
    start.add_argument("--workspace", type=Path, default=Path(os.environ.get("TIKTOK_VIDEO_WORKSPACE", Path.home() / "TikTokVideoWorkspace")))
    start.add_argument("--image-mode", choices=("website", "api-first"), default="website")
    task = sub.add_parser("task")
    task.add_argument("directory", type=Path)
    task.add_argument("operation", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    if os.name != "posix" or sys.version_info < (3, 10):
        parser.error("需要 macOS/Linux 的 Python 3.10+；Windows 请运行 scripts/windows.py，通过已安装的 WSL2 后端执行")
    import workflow
    try:
        if args.command == "start":
            return workflow.main([
                "--done-root", str(args.workspace.expanduser().absolute() / "已做"),
                "start-product", str(args.product),
                "--image-mode", args.image_mode,
            ])
        directory = args.directory.expanduser().resolve(strict=True)
        state_path = directory / workflow.TASK_STATE_FILE_NAME
        if state_path.is_symlink() or not state_path.is_file():
            raise ValueError("任务进度必须是普通文件")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        active = state["active_task"]
        if Path(active["task_dir"]).resolve() != directory:
            raise ValueError("任务目录与进度文件中的路径不一致；复制旧任务需要单独迁移路径")
        catalog = Path(active["source_dir"]).parent
        return workflow.main([
            "--catalog", str(catalog), "--done-root", str(directory.parent.parent),
            "--source-product", str(Path(active["source_dir"])),
            "--task-dir", str(directory), *(args.operation or ["status"]),
        ])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
