import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "legacy_video_run.py"


class PortableTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.product = self.root / "001-蓝色 牛仔裤"
        self.product.mkdir()
        self.picture = self.product / "正面图片.png"
        Image.new("RGB", (90, 160), "blue").save(self.picture)
        (self.product / "商品资料.txt").write_text("Blue jeans. Size S, M.", encoding="utf-8")
        self.workspace = self.root / "workspace"

    def call(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(RUNNER), *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if expected == 0 else result

    def start(self, *extra):
        return self.call("start", self.product, "--workspace", self.workspace, *extra)

    def test_exact_product_roundtrip_with_spaces_and_sibling_output(self):
        original = self.picture.read_bytes()
        result = self.start()
        task = Path(result["task_dir"])
        self.assertEqual(task.name, "001-蓝色 牛仔裤")
        self.assertEqual(result["image_mode"], "website")
        self.assertEqual(Path(result["source_dir"]), self.product)
        self.assertEqual((task / self.picture.name).read_bytes(), original)
        self.assertEqual(self.picture.read_bytes(), original)
        state = self.call("task", task, "status")
        self.assertEqual(state["active_task"]["stage"], "selected")
        self.assertEqual(state["state_file"], result["state_file"])
        self.start_failure = self.call("start", self.product, "--workspace", self.workspace, expected=2)
        self.assertIn("已有任务", self.start_failure.stderr)
        self.assertEqual(len(list(self.workspace.glob("已做/*/*/工作流进度.json"))), 1)

    def prepare_first(self, *extra):
        result = self.start(*extra)
        task = Path(result["task_dir"])
        self.call("task", task, "register-reference", self.picture)
        (task / "图片制作" / "01-start-prompt.txt").write_text("成年人物穿蓝色牛仔裤，竖屏9:16，产品款式不变。", encoding="utf-8")
        self.call("task", task, "mark-image-prompt", 1)
        return task

    def test_website_attempts_do_not_fabricate_api_usage(self):
        task = self.prepare_first()
        for number in range(1, 4):
            result = self.call("task", task, "claim-image-attempt", 1, "website")
            self.assertEqual(result["claimed"]["attempt"], number)
            self.assertEqual(result["active_task"]["image_attempts"]["1"], {"api": 0, "website": number})
        self.assertEqual(result["next_on_failure"], "manual")
        self.call("task", task, "claim-image-attempt", 1, "website", expected=2)
        candidate = task / "图片制作" / "generated.png"
        Image.new("RGB", (90, 160), "green").save(candidate)
        self.call("task", task, "ingest-image", 1)
        self.call("task", task, "retry-image", 1)
        self.call("task", task, "mark-image-prompt", 1)
        self.call("task", task, "claim-image-attempt", 1, "website", expected=2)

    def test_optional_api_mode_preserves_fallback_sequence(self):
        task = self.prepare_first("--image-mode", "api-first")
        self.call("task", task, "claim-image-attempt", 1, "website", expected=2)
        self.call("task", task, "claim-image-attempt", 1, "api")
        self.call("task", task, "claim-image-attempt", 1, "api")
        result = self.call("task", task, "claim-image-attempt", 1, "website")
        self.assertEqual(result["active_task"]["image_attempts"]["1"], {"api": 2, "website": 1})

    def test_symlink_source_and_nested_output_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.product, target_is_directory=True)
        self.call("start", alias, "--workspace", self.workspace, expected=2)
        result = self.call("start", self.product, "--workspace", self.product / "output", expected=2)
        self.assertIn("重叠", result.stderr)
        self.assertFalse(list(self.product.rglob("工作流进度.json")))

    def test_new_product_does_not_inherit_unfinished_task(self):
        first = self.start()
        before = Path(first["state_file"]).read_bytes()
        other = self.root / "002-黑色牛仔裤"
        other.mkdir()
        Image.new("RGB", (90, 160), "black").save(other / "product.png")
        second = self.call("start", other, "--workspace", self.workspace)
        self.assertTrue(Path(second["task_dir"]).name.startswith("002-"))
        self.assertNotEqual(first["task_dir"], second["task_dir"])
        self.assertEqual(Path(first["state_file"]).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
