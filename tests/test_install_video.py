import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

INSTALLER = Path(__file__).resolve().parents[1] / "install_video.py"
NAME = "swimwear-tiktok-video-workflow"


class InstallVideoTests(unittest.TestCase):
    def test_fresh_install_refuses_overwrite_and_update_backs_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            codex_dir = Path(temporary) / "codex"
            args = [sys.executable, str(INSTALLER), "--codex-dir", str(codex_dir), "--skip-deps"]
            first = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(first.returncode, 0, first.stderr)
            target = Path(json.loads(first.stdout)["installed"])
            self.assertTrue((target / "scripts" / "run.py").is_file())
            marker = target / "local-change.txt"
            marker.write_text("keep this", encoding="utf-8")
            refusal = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(refusal.returncode, 2)
            self.assertEqual(marker.read_text(), "keep this")
            update = subprocess.run([*args, "--replace"], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(update.returncode, 0, update.stderr)
            backup = Path(json.loads(update.stdout)["backup"])
            self.assertEqual((backup / marker.name).read_text(), "keep this")
            self.assertFalse((target / marker.name).exists())


if __name__ == "__main__":
    unittest.main()
