#!/usr/bin/env python3
"""Regression tests for the generic TikTok Shop listing workflow."""

from __future__ import annotations

import datetime as dt
import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import unicodedata
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
WORKFLOW = Path(os.environ.get("LISTING_WORKFLOW_SCRIPT", SCRIPT_DIR / "listing_workflow.py")).resolve()
RENDERER = SCRIPT_DIR / "render_spec_chart.py"
URL_OPENER = SCRIPT_DIR / "open_product_urls.py"


class ListingWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="generic-listing-test-")
        self.root = Path(self.temporary.name).resolve()
        initialized = self.run_cli("init-config")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.config_path = self.root / ".tiktok-shop-listing" / "config.json"
        config = self.load_config()
        config["availability"]["document_url"] = "https://www.kdocs.cn/l/example-stock-document"
        config["availability"]["bound_catalog_roots"] = list(config["catalog_roots"])
        self.save_config(config)
        self.catalog = self.root / "选品" / "测试分类"
        self.catalog.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def history_path(self) -> Path:
        return self.root / ".tiktok-shop-listing" / "history.json"

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKFLOW), "--root", str(self.root), *map(str, arguments)],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def load_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def save_config(self, config: dict) -> None:
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def load_history(self) -> dict:
        return json.loads(self.history_path.read_text(encoding="utf-8"))

    def save_history(self, history: dict) -> None:
        self.history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def make_product(self, name: str) -> Path:
        source = self.catalog / name
        source.mkdir()
        (source / "front.jpg").write_bytes(b"test-image")
        return source

    def reserve(self, name: str, code: str, *extra: str) -> Path:
        source = self.make_product(name)
        result = self.run_cli(
            "reserve",
            str(source),
            "--product-code",
            code,
            "--date",
            "2026-08-11",
            *extra,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(json.loads(result.stdout)["output_dir"])

    def confirm(self, output_dir: Path, query: str, matched_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "confirm-availability",
            str(output_dir),
            "--query",
            query,
            "--matched-text",
            matched_text or query,
            "--active-sheet",
            "2026.8.11在售货盘",
            "--active-cell",
            "$A$2",
            "--scope",
            "all_sheets",
            "--sold-out-status",
            "clear",
        )

    def entry_for(self, output_dir: Path) -> dict:
        relative = output_dir.relative_to(self.root).as_posix()
        return next(
            entry for entry in self.load_history()["products"] if entry["output_relative"] == relative
        )

    def prepare_deliverables(self, output_dir: Path) -> None:
        (output_dir / "TK店铺标题和内容.txt").write_text(
            "Title: Verified Product\n\nContent:\nItem ID: TEST\n", encoding="utf-8"
        )
        jpeg = base64.b64decode(
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
            "2wBDAf//////////////////////////////////////////////////////////////////////////////////////"
            "wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/"
            "9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAA"
            "AAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAA"
            "AAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABD/"
            "xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/EH//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EH//"
            "xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EH//2Q=="
        )
        (output_dir / "扫同款产品图.jpg").write_bytes(jpeg)

    def test_init_config_and_show_config(self) -> None:
        result = self.run_cli("show-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["config"]["collection"]["price_threshold"], 50.0)
        self.assertEqual(payload["config"]["collection"]["price_replacement"], 50.01)
        self.assertEqual(payload["config"]["collection"]["sku_inventory"], 19)
        duplicate = self.run_cli("init-config")
        self.assertEqual(duplicate.returncode, 2)

        config = self.load_config()
        config["deliverables"]["supplier_links_file"] = "../outside.txt"
        self.save_config(config)
        invalid = self.run_cli("show-config")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("must not contain path separators", invalid.stderr)

        config = self.load_config()
        config["deliverables"]["copy_file"] = "é.txt"
        config["deliverables"]["supplier_links_file"] = "e\u0301.txt"
        self.save_config(config)
        unicode_collision = self.run_cli("show-config")
        self.assertEqual(unicode_collision.returncode, 2)
        self.assertIn("file names must be unique", unicode_collision.stderr)

    def test_audit_and_eligible_fail_closed_for_incomplete_setup(self) -> None:
        healthy = self.run_cli("audit")
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        self.assertTrue(json.loads(healthy.stdout)["valid"])
        self.assertTrue(json.loads(healthy.stdout)["collection_ready"])

        config = self.load_config()
        config["availability"]["document_url"] = ""
        self.save_config(config)
        missing_url = self.run_cli("audit")
        self.assertEqual(missing_url.returncode, 0)
        self.assertTrue(json.loads(missing_url.stdout)["valid"])
        self.assertFalse(json.loads(missing_url.stdout)["collection_ready"])
        self.assertIn("availability.document_url", missing_url.stdout)

        config["availability"]["document_url"] = "https://www.kdocs.cn/l/example-stock-document"
        config["catalog_roots"] = ["不存在的商品库"]
        self.save_config(config)
        missing_catalog = self.run_cli("audit")
        self.assertEqual(missing_catalog.returncode, 2)
        self.assertIn("不存在的商品库", missing_catalog.stdout)
        eligible = self.run_cli("eligible")
        self.assertEqual(eligible.returncode, 2)
        self.assertIn("catalog roots are missing", eligible.stderr)

    def test_category_change_requires_new_bound_supplier_document(self) -> None:
        config = self.load_config()
        config["catalog_roots"] = ["选品/测试分类"]
        self.save_config(config)

        audit = self.run_cli("audit")
        self.assertEqual(audit.returncode, 0, audit.stderr)
        payload = json.loads(audit.stdout)
        self.assertTrue(payload["valid"])
        self.assertFalse(payload["collection_ready"])
        self.assertIn("bound_catalog_roots", audit.stdout)

        output_dir = self.reserve("新分类商品", "NEW-1000")
        blocked = self.confirm(output_dir, "NEW-1000")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("ask the user", blocked.stderr)

        config["availability"]["document_url"] = ""
        config["availability"]["bound_catalog_roots"] = []
        self.save_config(config)
        selectable = self.run_cli("eligible")
        self.assertEqual(selectable.returncode, 0, selectable.stderr)

        config["availability"]["document_url"] = "https://www.kdocs.cn/l/new-category-document"
        config["availability"]["bound_catalog_roots"] = list(config["catalog_roots"])
        self.save_config(config)
        ready = self.run_cli("audit")
        self.assertTrue(json.loads(ready.stdout)["collection_ready"])

    def test_equivalent_bound_catalog_path_is_accepted(self) -> None:
        config = self.load_config()
        config["availability"]["bound_catalog_roots"] = [str(self.root / "选品")]
        self.save_config(config)
        audit = self.run_cli("audit")
        self.assertEqual(audit.returncode, 0, audit.stderr)
        self.assertTrue(json.loads(audit.stdout)["collection_ready"])

    def test_numeric_config_fields_reject_json_booleans(self) -> None:
        config = self.load_config()
        config["collection"]["sku_inventory"] = False
        self.save_config(config)
        invalid = self.run_cli("show-config")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("sku_inventory", invalid.stderr)

    def test_eligible_reserve_and_permanent_source_deduplication(self) -> None:
        source = self.make_product("厨房收纳架-A")
        eligible = self.run_cli("eligible")
        self.assertEqual(eligible.returncode, 0, eligible.stderr)
        self.assertEqual(json.loads(eligible.stdout)["eligible_total"], 1)

        reserved = self.run_cli(
            "reserve",
            str(source),
            "--product-code",
            "RACK-2026-A",
            "--date",
            "2026-08-11",
        )
        self.assertEqual(reserved.returncode, 0, reserved.stderr)
        output_dir = Path(json.loads(reserved.stdout)["output_dir"])
        output_dir.rmdir()

        eligible = self.run_cli("eligible")
        self.assertEqual(json.loads(eligible.stdout)["eligible_total"], 0)
        reused_source = self.run_cli(
            "reserve",
            str(source),
            "--product-code",
            "OTHER-2026",
            "--date",
            "2026-08-11",
        )
        self.assertEqual(reused_source.returncode, 2)
        self.assertIn("source_already_selected", reused_source.stderr)

    def test_duplicate_product_code_across_catalog_folders_is_rejected(self) -> None:
        self.reserve("商品一", "MODEL-100")
        other = self.make_product("商品二")
        duplicate = self.run_cli(
            "reserve",
            str(other),
            "--product-code",
            "model-100",
            "--date",
            "2026-08-11",
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("already_selected", duplicate.stderr)

    def test_product_code_and_alias_collisions_are_rejected(self) -> None:
        self.reserve("商品一", "ITEM-100", "--alias", "LEGACY-100")

        code_matches_old_alias = self.run_cli(
            "reserve",
            str(self.make_product("商品二")),
            "--product-code",
            "legacy-100",
            "--date",
            "2026-08-11",
        )
        self.assertEqual(code_matches_old_alias.returncode, 2)
        self.assertIn("already_selected", code_matches_old_alias.stderr)

        alias_matches_old_code = self.run_cli(
            "reserve",
            str(self.make_product("商品三")),
            "--product-code",
            "ITEM-200",
            "--alias",
            "item-100",
            "--date",
            "2026-08-11",
        )
        self.assertEqual(alias_matches_old_code.returncode, 2)
        self.assertIn("already_selected", alias_matches_old_code.stderr)

    def test_overlapping_catalog_roots_do_not_duplicate_candidates(self) -> None:
        config = self.load_config()
        config["catalog_roots"] = ["选品", "选品/测试分类"]
        self.save_config(config)
        source = self.make_product("重叠目录商品")

        eligible = self.run_cli("eligible")
        self.assertEqual(eligible.returncode, 0, eligible.stderr)
        self.assertEqual(json.loads(eligible.stdout)["eligible_total"], 1)

        reserved = self.run_cli(
            "reserve",
            str(source),
            "--product-code",
            "OVERLAP-100",
            "--date",
            "2026-08-11",
        )
        self.assertEqual(reserved.returncode, 0, reserved.stderr)
        eligible = self.run_cli("eligible")
        self.assertEqual(json.loads(eligible.stdout)["eligible_total"], 0)

    def test_unicode_alias_of_same_source_directory_is_rejected_when_supported(self) -> None:
        nfc_name = "Café商品"
        nfd_name = unicodedata.normalize("NFD", nfc_name)
        source = self.make_product(nfc_name)
        alias = source.with_name(nfd_name)
        if not alias.is_dir() or not os.path.samefile(source, alias):
            self.skipTest("Current filesystem does not expose NFC/NFD aliases for one directory")

        first = self.run_cli(
            "reserve", str(source), "--product-code", "UNICODE-ONE", "--date", "2026-08-11"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        duplicate = self.run_cli(
            "reserve", str(alias), "--product-code", "UNICODE-TWO", "--date", "2026-08-11"
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("source_already_selected", duplicate.stderr)

    def test_record_links_fails_closed_without_availability(self) -> None:
        output_dir = self.reserve("桌面灯", "LAMP-8801")
        result = self.run_cli("record-links", str(output_dir), "https://example.com/item")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no confirmed availability", result.stderr)
        self.assertFalse((output_dir / "供应商短链接.txt").exists())
        self.assertEqual(self.entry_for(output_dir)["status"], "awaiting_manual_scan")

    def test_confirm_record_and_complete_preserve_ranked_links(self) -> None:
        output_dir = self.reserve("蓝牙音箱", "SPK-2026-01")
        self.prepare_deliverables(output_dir)
        self.assertEqual(self.confirm(output_dir, "SPK-2026-01").returncode, 0)
        urls = ["https://example.com/high", "https://example.com/mid", "https://example.com/low"]
        record = self.run_cli("record-links", str(output_dir), *urls)
        self.assertEqual(record.returncode, 0, record.stderr)
        self.assertEqual(self.entry_for(output_dir)["supplier_links"], urls)
        collected = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/high/long",
            "--resolved-url",
            "https://example.com/mid/long",
            "--resolved-url",
            "https://example.com/low/long",
        )
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertEqual(self.entry_for(output_dir)["status"], "miaoshou_collected")

    def test_sold_out_sheet_and_neighbor_code_are_rejected(self) -> None:
        output_dir = self.reserve("充电器", "CHG-1001")
        sold_out = self.run_cli(
            "confirm-availability",
            str(output_dir),
            "--query",
            "CHG-1001",
            "--matched-text",
            "CHG-1001",
            "--active-sheet",
            "售罄下架款",
            "--active-cell",
            "$A$1",
            "--scope",
            "all_sheets",
            "--sold-out-status",
            "clear",
        )
        self.assertEqual(sold_out.returncode, 2)
        neighbor = self.confirm(output_dir, "CHG-1001", "CHG-10010")
        self.assertEqual(neighbor.returncode, 2)
        self.assertIn("complete product code", neighbor.stderr)

    def test_recorded_alias_can_confirm_but_unrecorded_alias_cannot(self) -> None:
        output_dir = self.reserve("收纳盒", "BOX-2026-PINK", "--alias", "BX26P")
        allowed = self.confirm(output_dir, "BX26P", "BX26P-S BX26P-L")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        wrong = self.confirm(output_dir, "PINKBOX")
        self.assertEqual(wrong.returncode, 2)
        self.assertIn("recorded product code", wrong.stderr)

    def test_stale_evidence_and_link_replacement_require_recheck(self) -> None:
        output_dir = self.reserve("数据线", "CABLE-2401")
        self.prepare_deliverables(output_dir)
        self.assertEqual(self.confirm(output_dir, "CABLE-2401").returncode, 0)
        first = self.run_cli("record-links", str(output_dir), "https://example.com/first")
        self.assertEqual(first.returncode, 0, first.stderr)
        replacement = self.run_cli("record-links", str(output_dir), "https://example.com/replacement")
        self.assertEqual(replacement.returncode, 2)
        self.assertIn("Check availability again", replacement.stderr)

        self.assertEqual(self.confirm(output_dir, "CABLE-2401").returncode, 0)
        replacement = self.run_cli("record-links", str(output_dir), "https://example.com/replacement")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        history = self.load_history()
        entry = next(item for item in history["products"] if item["product_code"] == "CABLE-2401")
        entry["availability"]["checked_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        ).isoformat(timespec="microseconds")
        self.save_history(history)
        stale = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/replacement/long",
        )
        self.assertEqual(stale.returncode, 2)
        self.assertIn("evidence is stale", stale.stderr)

    def test_config_change_invalidates_evidence(self) -> None:
        output_dir = self.reserve("挂钩", "HOOK-5510")
        self.assertEqual(self.confirm(output_dir, "HOOK-5510").returncode, 0)
        config = self.load_config()
        config["availability"]["sold_out_sheets"].append("暂停供货")
        self.save_config(config)
        result = self.run_cli("record-links", str(output_dir), "https://example.com/item")
        self.assertEqual(result.returncode, 2)
        self.assertIn("config_fingerprint", result.stderr)

    def test_availability_can_be_explicitly_disabled_in_local_config(self) -> None:
        config = self.load_config()
        config["availability"]["required"] = False
        config["availability"]["document_url"] = ""
        self.save_config(config)
        output_dir = self.reserve("便携风扇", "FAN-6600")
        self.prepare_deliverables(output_dir)
        record = self.run_cli("record-links", str(output_dir), "https://example.com/item")
        self.assertEqual(record.returncode, 0, record.stderr)
        collected = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/item/long",
        )
        self.assertEqual(collected.returncode, 0, collected.stderr)

    def test_verify_respects_optional_and_required_spec_chart(self) -> None:
        output_dir = self.reserve("水杯", "CUP-9000")
        self.prepare_deliverables(output_dir)
        optional = self.run_cli("verify", str(output_dir))
        self.assertEqual(optional.returncode, 0, optional.stderr)

        config = self.load_config()
        config["deliverables"]["spec_chart_required"] = True
        self.save_config(config)
        required = self.run_cli("verify", str(output_dir))
        self.assertEqual(required.returncode, 2)
        self.assertIn("spec_chart", json.loads(required.stdout)["missing"])

    def test_verify_rejects_invalid_copy_and_scan_image(self) -> None:
        output_dir = self.reserve("无效资料", "INVALID-100")
        (output_dir / "TK店铺标题和内容.txt").write_text("Title only", encoding="utf-8")
        (output_dir / "扫同款产品图.jpg").write_bytes(b"\xff\xd8\xff\xff\xd9")
        result = self.run_cli("verify", str(output_dir))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(set(json.loads(result.stdout)["invalid"]), {"copy", "scan_image"})

        self.prepare_deliverables(output_dir)
        (output_dir / "TK店铺标题和内容.txt").write_text(
            "Title:\nContent:\nItem ID: TEST\n", encoding="utf-8"
        )
        empty_title = self.run_cli("verify", str(output_dir))
        self.assertEqual(empty_title.returncode, 2)
        self.assertIn("copy", json.loads(empty_title.stdout)["invalid"])

        config = self.load_config()
        config["copy"]["title_max_chars"] = 5
        self.save_config(config)
        (output_dir / "TK店铺标题和内容.txt").write_text(
            "Title: Too Long\n\nContent:\nItem ID: TEST\n", encoding="utf-8"
        )
        overlong_title = self.run_cli("verify", str(output_dir))
        self.assertEqual(overlong_title.returncode, 2)
        self.assertIn("copy", json.loads(overlong_title.stdout)["invalid"])

    def test_mark_collected_requires_prepared_deliverables(self) -> None:
        output_dir = self.reserve("缺少资料", "MISSING-100")
        self.assertEqual(self.confirm(output_dir, "MISSING-100").returncode, 0)
        self.assertEqual(
            self.run_cli("record-links", str(output_dir), "https://example.com/item").returncode,
            0,
        )
        result = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/item/long",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Prepared deliverables", result.stderr)
        self.assertEqual(self.entry_for(output_dir)["status"], "awaiting_miaoshou_collection")

    def test_final_state_cannot_regress_and_file_rolls_back_on_history_failure(self) -> None:
        output_dir = self.reserve("鼠标垫", "PAD-3100")
        self.prepare_deliverables(output_dir)
        self.assertEqual(self.confirm(output_dir, "PAD-3100").returncode, 0)
        self.assertEqual(
            self.run_cli("record-links", str(output_dir), "https://example.com/item").returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "mark-collected",
                str(output_dir),
                "--resolved-url",
                "https://example.com/item/long",
            ).returncode,
            0,
        )
        regress = self.run_cli("record-links", str(output_dir), "https://example.com/again")
        self.assertEqual(regress.returncode, 2)
        self.assertEqual(self.entry_for(output_dir)["status"], "miaoshou_collected")

        rollback_output = self.reserve("键盘刷", "BRUSH-7200")
        self.assertEqual(self.confirm(rollback_output, "BRUSH-7200").returncode, 0)
        spec = importlib.util.spec_from_file_location("listing_workflow_under_test", WORKFLOW)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = module.load_config(self.config_path)
        context = module.build_context(self.root, self.config_path, config)
        original = module.atomic_write_json

        def fail_history_commit(path: Path, data: dict) -> None:
            raise OSError("simulated history failure")

        module.atomic_write_json = fail_history_commit
        try:
            with self.assertRaisesRegex(OSError, "simulated history failure"):
                module.command_record_links(
                    types.SimpleNamespace(
                        output_dir=str(rollback_output), urls=["https://example.com/item"]
                    ),
                    context,
                )
        finally:
            module.atomic_write_json = original
        self.assertFalse((rollback_output / "供应商短链接.txt").exists())
        self.assertNotIn("supplier_links", self.entry_for(rollback_output))

    def test_reserve_removes_output_folder_when_history_commit_fails(self) -> None:
        source = self.make_product("失败回滚商品")
        spec = importlib.util.spec_from_file_location("listing_workflow_reserve_test", WORKFLOW)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = module.load_config(self.config_path)
        context = module.build_context(self.root, self.config_path, config)
        original = module.atomic_write_json

        def fail_history_commit(path: Path, data: dict) -> None:
            raise OSError("simulated reserve failure")

        module.atomic_write_json = fail_history_commit
        try:
            with self.assertRaisesRegex(OSError, "simulated reserve failure"):
                module.command_reserve(
                    types.SimpleNamespace(
                        source=str(source),
                        product_code="ROLLBACK-100",
                        aliases=[],
                        category=None,
                        tags=[],
                        date="2026-08-11",
                    ),
                    context,
                )
        finally:
            module.atomic_write_json = original

        date_dir = self.root / "链接" / "2026-08-11"
        self.assertEqual(list(date_dir.iterdir()), [])


class UtilityScriptTests(unittest.TestCase):
    def test_url_dry_run_preserves_rank(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(URL_OPENER),
                "--dry-run",
                "https://example.com/high",
                "https://example.com/low",
            ],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([item["rank"] for item in payload["ranked_urls"]], [1, 2])

    def test_spec_renderer_outputs_expected_png(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spec-render-test-") as temporary:
            root = Path(temporary)
            source = root / "spec.json"
            output = root / "spec.png"
            source.write_text(
                json.dumps(
                    {
                        "product_code": "BOX-2026",
                        "unit": "cm",
                        "columns": ["Specification", "Value"],
                        "rows": [["Length", "20"], ["Width", "10"]],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(RENDERER), "--input", str(source), "--output", str(output)],
                capture_output=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())
            from PIL import Image

            with Image.open(output) as image:
                self.assertEqual(image.size, (1800, 2200))
                self.assertEqual(image.format, "PNG")


if __name__ == "__main__":
    unittest.main()
