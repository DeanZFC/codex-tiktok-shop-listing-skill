import importlib.util
import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_module():
    path = Path(__file__).parents[1] / "scripts" / "migrate_archive_layout.py"
    spec = importlib.util.spec_from_file_location("migrate_archive_layout", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migration = load_module()


class MigrationTests(unittest.TestCase):
    def test_migrates_archives_and_rewrites_nested_task_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            done = root / "done"
            state_root = root / "state"
            threads = state_root / "threads"
            done.mkdir()
            threads.mkdir(parents=True)
            first = done / "2026-07-21_001_product-a"
            second = done / "2026-07-21_002_product-b"
            for archive in (first, second):
                (archive / "图片制作").mkdir(parents=True)
                (archive / "视频制作").mkdir()
            state = {
                "version": 1,
                "active_task": {
                    "task_dir": str(second),
                    "image_dir": str(second / "图片制作"),
                    "video_dir": str(second / "视频制作"),
                    "reference": str(second / "图片制作" / "reference.png"),
                    "frames": {"1": str(second / "图片制作" / "frame.png")},
                },
            }
            state_file = threads / "thread.json"
            state_file.write_text(json.dumps(state), encoding="utf-8")

            dry = migration.migrate(done, state_root, dry_run=True)
            self.assertTrue(first.is_dir())
            self.assertEqual(len(dry["moves"]), 2)

            result = migration.migrate(done, state_root)
            first_new = done / "2026-07-21" / "001-001_product-a"
            second_new = done / "2026-07-21" / "002-002_product-b"
            self.assertTrue(first_new.is_dir())
            self.assertTrue(second_new.is_dir())
            self.assertFalse(first.exists())
            updated = json.loads(state_file.read_text(encoding="utf-8"))
            active = updated["active_task"]
            self.assertEqual(active["task_dir"], str(second_new))
            self.assertEqual(
                active["frames"]["1"],
                str(second_new / "图片制作" / "frame.png"),
            )
            self.assertEqual(result["unresolved_states"], [])

    def test_main_dry_run_emits_json_without_moving_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            done = root / "done"
            state_root = root / "state"
            done.mkdir()
            state_root.mkdir()
            archive = done / "2026-07-21_001_product-a"
            archive.mkdir()
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                result = migration.main(
                    [
                        "--done-root",
                        str(done),
                        "--state-root",
                        str(state_root),
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertTrue(archive.is_dir())
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(len(payload["moves"]), 1)


if __name__ == "__main__":
    unittest.main()
