from contextlib import redirect_stdout
from io import StringIO
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "manage-tiktok-shop-listings" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import listing_workflow
import run


class UnifiedHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="unified-handoff-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.product = self.root / "supplier" / "真实产品目录"
        self.product.mkdir(parents=True)
        Image.new("RGB", (90, 160), "blue").save(self.product / "front.png")
        self.context = run.bootstrap_product(self.workspace, self.product)
        out = StringIO()
        with redirect_stdout(out):
            listing_workflow.command_reserve(
                argparse.Namespace(
                    source=str(self.product),
                    product_code="JEANS-100",
                    aliases=[],
                    category="jeans",
                    tags=[],
                    date="2026-10-18",
                ),
                self.context,
            )
        self.output_dir = Path(json.loads(out.getvalue())["output_dir"])

    def entry(self):
        history = listing_workflow.load_history(self.context["history"])
        return listing_workflow.find_entry_by_output(history, self.output_dir, self.context)

    def test_video_starts_only_after_exact_publication_and_reuses_listing_directory(self):
        with self.assertRaisesRegex(ValueError, "tiktok_published"):
            run.start_video(self.workspace, self.output_dir, "website")

        history = listing_workflow.load_history(self.context["history"])
        entry = listing_workflow.find_entry_by_output(history, self.output_dir, self.context)
        entry["status"] = "miaoshou_collected"
        listing_workflow.atomic_write_json(self.context["history"], history)
        out = StringIO()
        with redirect_stdout(out):
            result = listing_workflow.command_mark_published(
                argparse.Namespace(
                    output_dir=str(self.output_dir),
                    stores=["Renova"],
                    product_ids=["1729000000000000100"],
                ),
                self.context,
            )
        self.assertEqual(result, 0)

        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(run.start_video(self.workspace, self.output_dir, "website"), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(Path(payload["task_dir"]), self.output_dir)
        self.assertEqual(Path(payload["source_dir"]), self.product.resolve())
        self.assertEqual(payload["product_name"], "JEANS-100")
        self.assertTrue((self.output_dir / "图片制作").is_dir())
        self.assertTrue((self.output_dir / "视频制作").is_dir())
        self.assertTrue((self.output_dir / "工作流进度.json").is_file())

        resumed = StringIO()
        with redirect_stdout(resumed):
            self.assertEqual(run.resume_task(self.output_dir, ["status"]), 0)
        self.assertEqual(
            json.loads(resumed.getvalue())["active_task"]["task_dir"],
            str(self.output_dir),
        )

    def test_new_supplier_parent_is_appended_without_breaking_old_history_source(self):
        other = self.root / "second-supplier" / "另一个产品"
        other.mkdir(parents=True)
        Image.new("RGB", (90, 160), "black").save(other / "front.png")
        updated = run.bootstrap_product(self.workspace, other)
        self.assertEqual(
            updated["catalogs"],
            [self.product.parent.resolve(), other.parent.resolve()],
        )
        history = listing_workflow.load_history(updated["history"])
        old_entry = listing_workflow.find_entry_by_output(
            history, self.output_dir, updated
        )
        self.assertEqual(
            listing_workflow.source_path_from_entry(old_entry, updated),
            self.product.resolve(),
        )
        self.assertEqual(updated["config"]["availability"]["document_url"], "")
        self.assertEqual(updated["config"]["availability"]["bound_catalog_roots"], [])


if __name__ == "__main__":
    unittest.main()
