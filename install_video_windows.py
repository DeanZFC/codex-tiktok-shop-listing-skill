#!/usr/bin/env python3
"""Install a Windows-discoverable skill with its POSIX runtime in WSL2."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "skills" / "swimwear-tiktok-video-workflow" / "scripts"))
from windows import WslBridge


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装 Windows Codex 技能与 WSL2 视频运行环境")
    parser.add_argument("--distro", default="Ubuntu", help="wsl --list --verbose 中的 WSL2 发行版名")
    parser.add_argument("--codex-dir", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--replace", action="store_true", help="备份并更新已有技能")
    args = parser.parse_args(argv)
    if os.name != "nt" or sys.version_info < (3, 10):
        parser.error("请在 Windows 的 Python 3.10+ 中执行；macOS/Linux 用 install_video.py")
    target = args.codex_dir.expanduser().resolve() / "skills" / "swimwear-tiktok-video-workflow"
    if target.exists() and not args.replace:
        parser.error("已安装同名技能；更新请加 --replace，旧技能会先备份")
    try:
        bridge = WslBridge(args.distro)
        preflight = bridge.exec([
            "python3", "-c",
            "import json,pathlib,platform,shutil,sys,venv; "
            "print(json.dumps({'home':str(pathlib.Path.home()),'kernel':platform.release(),"
            "'python':list(sys.version_info[:2]),'ffmpeg':shutil.which('ffmpeg'),'ffprobe':shutil.which('ffprobe')}))",
        ])
        info = json.loads(preflight.stdout)
        if "wsl2" not in info["kernel"].lower():
            raise ValueError("需要 WSL2。先用 wsl --list --verbose 查看版本；必要时 wsl --set-version Ubuntu 2")
        if info["python"] < [3, 10] or not info["ffmpeg"] or not info["ffprobe"]:
            raise ValueError("WSL 依赖未齐全；在该发行版中运行 sudo apt-get update，然后 sudo apt-get install -y python3 python3-venv ffmpeg")
        # Runtime and all task state live on the Linux filesystem, not DrvFS.
        backend_home = info["home"] + "/.local/share/tiktok-video/codex"
        backend = backend_home + "/skills/swimwear-tiktok-video-workflow"
        backend_installer = bridge.to_linux(str(ROOT / "install_video.py"))
        command = ["python3", backend_installer, "--codex-dir", backend_home]
        if args.replace:
            command.append("--replace")
        bridge.exec(command, capture=False)
        bridge.exec([backend + "/.venv/bin/python", backend + "/scripts/run.py", "doctor"], capture=False)
        command = [sys.executable, str(ROOT / "install_video.py"), "--codex-dir", str(args.codex_dir), "--skip-deps"]
        if args.replace:
            command.append("--replace")
        subprocess.run(command, check=True)
        (target / "wsl-runtime.json").write_text(json.dumps({
            "distro": args.distro, "skill_dir": backend,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"installed": str(target), "wsl_skill": backend, "distro": args.distro,
                          "next": "让 Codex 读取 SKILL.md；用 Windows Python 运行 scripts/windows.py doctor"}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr, end="")
        print(f"安装未完成：{exc}\n首次使用请先安装 WSL2 Ubuntu 并完成 Linux 用户创建，详见 references/windows.md。可加 --replace 安全重试。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
