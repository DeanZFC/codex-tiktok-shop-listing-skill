"""Installer orchestration tests. No actual WSL provisioning or paid APIs."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_windows", ROOT / "install_workflow_windows.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
NAME = "manage-tiktok-shop-listings"


class WindowsInstallerTests(unittest.TestCase):
    def preflight(self, **changes):
        info = dict(home="/home/test user", kernel="6.6.87.2-microsoft-standard-WSL2", python=[3, 12], ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
        info.update(changes)
        return subprocess.CompletedProcess([], 0, json.dumps(info), "")

    def test_install_records_only_local_configuration_after_backend_doctor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "中文 路径"
            target = root / "skills" / NAME
            def local_install(command, **kwargs):
                self.assertIn("--skip-deps", command)
                target.mkdir(parents=True)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(installer, "os", SimpleNamespace(name="nt", environ={})), patch.object(installer, "WslBridge") as bridge_class, patch.object(installer.subprocess, "run", side_effect=local_install) as local, redirect_stdout(StringIO()):
                bridge = bridge_class.return_value
                bridge.exec.side_effect = [self.preflight(), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
                bridge.to_linux.return_value = "/mnt/d/repo/install_workflow.py"
                self.assertEqual(installer.main(["--codex-dir", str(root), "--distro", "Ubuntu"]), 0)
                self.assertEqual(bridge.exec.call_args_list[2].args[0][-1], "doctor")
                local.assert_called_once()
            self.assertEqual(json.loads((target / "wsl-runtime.json").read_text(encoding="utf-8")), {"distro": "Ubuntu", "skill_dir": "/home/test user/.local/share/tiktok-shop-workflow/codex/skills/" + NAME})

    def test_wsl1_or_missing_dependency_never_copies_windows_skill(self):
        for changes in ({"kernel": "4.4.0-Microsoft"}, {"ffprobe": None}, {"python": [3, 9]}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                with patch.object(installer, "os", SimpleNamespace(name="nt", environ={})), patch.object(installer, "WslBridge") as bridge_class, patch.object(installer.subprocess, "run") as local, redirect_stderr(StringIO()):
                    bridge_class.return_value.exec.return_value = self.preflight(**changes)
                    self.assertEqual(installer.main(["--codex-dir", directory]), 2)
                    local.assert_not_called()

    def test_backend_failure_preserves_existing_windows_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "skills" / NAME
            target.mkdir(parents=True)
            marker = target / "local-change.txt"
            marker.write_text("keep", encoding="utf-8")
            with patch.object(installer, "os", SimpleNamespace(name="nt", environ={})), patch.object(installer, "WslBridge") as bridge_class, patch.object(installer.subprocess, "run") as local, redirect_stderr(StringIO()):
                bridge_class.return_value.exec.side_effect = [self.preflight(), subprocess.CalledProcessError(1, ["python3"], stderr="dependency install failed\n")]
                bridge_class.return_value.to_linux.return_value = "/mnt/d/repo/install_workflow.py"
                self.assertEqual(installer.main(["--codex-dir", directory, "--replace"]), 2)
                local.assert_not_called()
            self.assertEqual(marker.read_text(), "keep")

    def test_existing_installation_requires_replace_before_any_wsl_call(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "skills" / NAME).mkdir(parents=True)
            with patch.object(installer, "os", SimpleNamespace(name="nt", environ={})), patch.object(installer, "WslBridge") as bridge, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    installer.main(["--codex-dir", directory])
                self.assertEqual(error.exception.code, 2)
                bridge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
