#!/usr/bin/env python3
"""Windows CLI bridge; no shell interpolation, browser secrets, or state rewriting."""
import json
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys

CONFIG = Path(__file__).resolve().parents[1] / "wsl-runtime.json"
SCRIPTS = {"openai_image.py", "toapi_image.py", "salvage_merge.py", "workflow.py", "listing_workflow.py"}


class WslBridge:
    def __init__(self, distro, skill_dir=None):
        if not isinstance(distro, str) or not distro.strip() or any(c in distro for c in "\0\r\n"):
            raise ValueError("WSL 发行版名称无效")
        if skill_dir is not None and (not isinstance(skill_dir, str) or not skill_dir.startswith("/")):
            raise ValueError("WSL 技能路径必须为 Linux 绝对路径")
        self.distro = distro
        self.skill_dir = skill_dir

    def exec(self, arguments, capture=True, check=True):
        # --exec passes an argv array directly, never bash -c / PowerShell text.
        return subprocess.run(
            ["wsl.exe", "--distribution", self.distro, "--exec", *arguments],
            capture_output=capture, text=True, encoding="utf-8", errors="replace", check=check,
        )

    def to_linux(self, value):
        if PureWindowsPath(value).is_absolute():
            path = PureWindowsPath(value)
            if path.drive.lower() in ("\\\\wsl.localhost\\" + self.distro.lower(), "\\\\wsl$\\" + self.distro.lower()):
                return "/" + "/".join(path.parts[1:])
            if not re.match(r"^[A-Za-z]:[\\/]", value):
                raise ValueError("只支持本机盘符或当前 WSL 发行版的 UNC 路径")
            return self.exec(["wslpath", "-a", "-u", value]).stdout.strip()
        if re.match(r"^[A-Za-z]:", value) or value.startswith("\\"):
            raise ValueError("Windows 路径请使用带盘符的绝对路径或完整 UNC 路径")
        return value

    def to_windows(self, value):
        if not value.startswith("/"):
            raise ValueError("需要 Linux 绝对路径")
        return self.exec(["wslpath", "-w", value]).stdout.strip()

    def run(self, arguments, script="run.py"):
        if self.skill_dir is None:
            raise ValueError("缺少 WSL 技能安装配置，请重新运行安装器")
        converted = [self.to_linux(arg) for arg in arguments]
        if script == "run.py":
            for index, value in enumerate(converted):
                if value == "--workspace" and index + 1 < len(converted):
                    workspace = converted[index + 1]
                elif value.startswith("--workspace="):
                    workspace = self.to_linux(value.split("=", 1)[1])
                    converted[index] = "--workspace=" + workspace
                else:
                    continue
                if not workspace.startswith("/") or workspace == "/mnt" or workspace.startswith("/mnt/"):
                    raise ValueError("任务工作区必须位于 WSL Linux 文件系统；省略 --workspace 使用默认值，成片用 copy 导出到 Windows")
        return self.exec([
            self.skill_dir + "/.venv/bin/python", self.skill_dir + "/scripts/" + script,
            *converted,
        ], check=False)

    def paths_in(self, value):
        """Add display paths separately: task JSON retains its Linux identities."""
        paths = {}
        def visit(item):
            if isinstance(item, dict):
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, str) and item.startswith("/") and "\n" not in item:
                if item not in paths:
                    try:
                        paths[item] = self.to_windows(item)
                    except (OSError, subprocess.CalledProcessError):
                        pass
        visit(value)
        return paths


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("-h", "--help"):
        print("windows.py doctor | start PRODUCT_DIR --product-code CODE | listing COMMAND ...\n"
              "windows.py start-video OUTPUT_DIR | task OUTPUT_DIR COMMAND ...\n"
              "windows.py path LINUX_PATH\nwindows.py copy SOURCE_FILE DESTINATION_FILE\n"
              "windows.py script SCRIPT_NAME ARGUMENTS ...\n"
              "Windows 路径会自动转换；任务输出保留 Linux 路径，并另附 windows_paths。")
        return 0
    try:
        command = arguments[0]
        if command in ("start", "start-video", "task") and len(arguments) < 2:
            raise ValueError(f"{command} 需要绝对产品目录或任务目录")
        if command in ("start", "start-video", "task"):
            candidate = arguments[1]
            if not (candidate.startswith("/") or PureWindowsPath(candidate).is_absolute()):
                raise ValueError("请提供 Windows 盘符或 Linux 根目录开头的绝对路径")
        if command == "copy" and (len(arguments) != 3 or not all(
            item.startswith("/") or PureWindowsPath(item).is_absolute() for item in arguments[1:]
        )):
            raise ValueError("copy 需要 Windows 盘符或 Linux 根目录开头的两个绝对文件路径")
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        bridge = WslBridge(config["distro"], config["skill_dir"])
        if command == "path":
            if len(arguments) != 2:
                raise ValueError("用法：windows.py path LINUX_PATH")
            print(bridge.to_windows(arguments[1]))
            return 0
        script = "run.py"
        if command == "copy":
            if len(arguments) != 3:
                raise ValueError("用法：windows.py copy SOURCE_FILE DESTINATION_FILE（不覆盖已有文件）")
            script, arguments = "transfer_file.py", arguments[1:]
        elif command == "script":
            if len(arguments) < 2 or arguments[1] not in SCRIPTS:
                raise ValueError("允许的脚本：" + ", ".join(sorted(SCRIPTS)))
            script, arguments = arguments[1], arguments[2:]
        elif command not in ("doctor", "start", "start-video", "listing", "task"):
            raise ValueError("未知命令；请运行 windows.py --help")
        result = bridge.run(arguments, script)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            print(result.stdout, end="")
        else:
            print(json.dumps({"result": payload, "windows_paths": bridge.paths_in(payload)}, ensure_ascii=False, indent=2))
        return result.returncode
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Windows/WSL 桥接失败：{exc}\n请检查 WSL2 发行版与 wsl-runtime.json；首次安装运行 install_workflow_windows.py。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
