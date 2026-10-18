"""Runs on Windows too; WSL process calls are mocked, not a WSL/browser E2E test."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("video_windows", ROOT / "skills/swimwear-tiktok-video-workflow/scripts/windows.py")
windows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(windows)


class WindowsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = windows.WslBridge("Ubuntu", "/home/test/技能目录")

    def test_unicode_paths_and_metacharacters_are_single_arguments(self):
        source = r"D:\产品 空格\款式 & $(echo unsafe)"
        mapped = "/mnt/d/产品 空格/款式 & $(echo unsafe)"
        with patch.object(windows.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, mapped + "\n", ""),
            subprocess.CompletedProcess([], 0, "{}", ""),
        ]) as run:
            self.bridge.run(["start", source])
        self.assertEqual(run.call_args_list[0].args[0], ["wsl.exe", "--distribution", "Ubuntu", "--exec", "wslpath", "-a", "-u", source])
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["start", mapped])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_current_distro_unc_and_linux_paths_roundtrip(self):
        with patch.object(windows.subprocess, "run") as run:
            self.assertEqual(self.bridge.to_linux(r"\\wsl.localhost\Ubuntu\home\test\产品"), "/home/test/产品")
            self.assertEqual(self.bridge.to_linux(r"\\wsl$\Ubuntu\home\test\产品"), "/home/test/产品")
            self.assertEqual(self.bridge.to_linux("/home/test/产品"), "/home/test/产品")
            run.assert_not_called()
        for value in (r"D:产品", r"\产品", r"\\server\share\产品", r"\\wsl.localhost\Debian\home\产品"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bridge.to_linux(value)

    def test_windows_archive_root_is_rejected_before_start(self):
        with patch.object(self.bridge, "exec", return_value=subprocess.CompletedProcess([], 0, "/mnt/d/output\n", "")) as execute:
            with self.assertRaises(ValueError):
                self.bridge.run(["start", "/mnt/d/product", "--workspace", r"D:\output"])
            self.assertEqual(execute.call_count, 1)
            with self.assertRaises(ValueError):
                self.bridge.run(["start", "/mnt/d/product", "--workspace=/mnt/d/output"])
            self.assertEqual(execute.call_count, 1)

    def config(self, root):
        config = root / "wsl-runtime.json"
        config.write_text(json.dumps({"distro": "Ubuntu", "skill_dir": "/home/test/skill"}), encoding="utf-8")
        return config

    def test_result_keeps_state_paths_and_returns_windows_aliases(self):
        result = {"active_task": {"task_dir": "/home/test/产品", "stage": "selected"}}
        with tempfile.TemporaryDirectory() as directory:
            out = StringIO()
            with patch.object(windows, "CONFIG", self.config(Path(directory))), patch.object(windows.WslBridge, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(result), "")) as run, patch.object(windows.WslBridge, "to_windows", return_value=r"\\wsl.localhost\Ubuntu\home\test\产品"), redirect_stdout(out):
                self.assertEqual(windows.main(["task", "/home/test/产品", "status"]), 0)
            run.assert_called_once_with(["task", "/home/test/产品", "status"], "run.py")
            output = json.loads(out.getvalue())
            self.assertEqual(output["result"], result)
            self.assertEqual(output["windows_paths"]["/home/test/产品"], r"\\wsl.localhost\Ubuntu\home\test\产品")

    def test_failure_exit_code_and_stderr_survive_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            err = StringIO()
            with patch.object(windows, "CONFIG", self.config(Path(directory))), patch.object(windows.WslBridge, "run", return_value=subprocess.CompletedProcess([], 2, "", "重复任务\n")), redirect_stdout(StringIO()), redirect_stderr(err):
                self.assertEqual(windows.main(["start", r"D:\产品"]), 2)
            self.assertIn("重复任务", err.getvalue())

    def test_copy_uses_file_transfer_and_unknown_script_cannot_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(windows, "CONFIG", self.config(Path(directory))), patch.object(windows.WslBridge, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(windows.main(["copy", r"D:\图片.png", "/home/test/候选.png"]), 0)
                run.assert_called_once_with([r"D:\图片.png", "/home/test/候选.png"], "transfer_file.py")
                self.assertEqual(windows.main(["script", "../../evil.py"]), 2)
                self.assertEqual(run.call_count, 1)

    def test_missing_installation_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            err = StringIO()
            with patch.object(windows, "CONFIG", Path(directory) / "missing.json"), redirect_stderr(err):
                self.assertEqual(windows.main(["doctor"]), 2)
            self.assertIn("install_video_windows.py", err.getvalue())


if __name__ == "__main__":
    unittest.main()
