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
        self.pricing_file_index = 0

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

    def record_pricing(
        self,
        output_dir: Path,
        skus: list[dict] | None = None,
        *,
        currency: str = "CNY",
        sales_unit: str = "piece",
        expected_sku_count: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.pricing_file_index += 1
        sku_file = self.root / f"pricing-{self.pricing_file_index}.json"
        rows = skus if skus is not None else [
            {
                "sku_key": "Default",
                "original_price": "50.00",
                "final_price": "50.00",
            }
        ]
        sku_file.write_text(
            json.dumps({"skus": rows}, ensure_ascii=False), encoding="utf-8"
        )
        return self.run_cli(
            "record-pricing",
            str(output_dir),
            "--currency",
            currency,
            "--sales-unit",
            sales_unit,
            "--expected-sku-count",
            str(expected_sku_count if expected_sku_count is not None else len(rows)),
            "--sku-file",
            str(sku_file),
        )

    def prepare_for_pricing(
        self,
        name: str,
        code: str,
        *,
        deliverables: bool = False,
    ) -> Path:
        output_dir = self.reserve(name, code)
        if deliverables:
            self.prepare_deliverables(output_dir)
        confirmed = self.confirm(output_dir, code)
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        recorded = self.run_cli(
            "record-links", str(output_dir), f"https://example.com/{code.casefold()}"
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        return output_dir

    def prepare_collected(self, name: str, code: str) -> Path:
        output_dir = self.prepare_for_pricing(name, code, deliverables=True)
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)
        collected = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            f"https://example.com/{code.casefold()}/resolved",
        )
        self.assertEqual(collected.returncode, 0, collected.stderr)
        return output_dir

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
        self.assertEqual(payload["config"]["collection"]["minimum_profit_rate"], 0.05)
        self.assertNotIn("price_replacement", payload["config"]["collection"])
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

    def test_named_publication_store_selection(self) -> None:
        config = self.load_config()
        config["publication"].update(
            {
                "store_selection": "named",
                "store_names": ["Renova"],
                "expected_store_count": 1,
            }
        )
        self.save_config(config)
        valid = self.run_cli("show-config")
        self.assertEqual(valid.returncode, 0, valid.stderr)

        config["publication"]["expected_store_count"] = 2
        self.save_config(config)
        mismatch = self.run_cli("show-config")
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn("equal the number of publication.store_names", mismatch.stderr)

        config["publication"]["expected_store_count"] = 1
        config["publication"]["store_names"] = ["Renova", "renova"]
        self.save_config(config)
        duplicate = self.run_cli("show-config")
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("non-empty list of unique names", duplicate.stderr)

        config["publication"] = {
            "auto_claim_tiktok": True,
            "auto_publish_tiktok": True,
            "store_selection": "all",
            "store_names": ["Renova"],
            "expected_store_count": 2,
            "publish_mode": "immediate",
        }
        self.save_config(config)
        unexpected_names = self.run_cli("show-config")
        self.assertEqual(unexpected_names.returncode, 2)
        self.assertIn("only allowed when store_selection is named", unexpected_names.stderr)

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

        config["collection"]["sku_inventory"] = 19
        config["collection"]["minimum_profit_rate"] = False
        self.save_config(config)
        invalid_rate = self.run_cli("show-config")
        self.assertEqual(invalid_rate.returncode, 2)
        self.assertIn("minimum_profit_rate", invalid_rate.stderr)

        config["collection"]["minimum_profit_rate"] = 1
        self.save_config(config)
        out_of_range_rate = self.run_cli("show-config")
        self.assertEqual(out_of_range_rate.returncode, 2)
        self.assertIn("minimum_profit_rate", out_of_range_rate.stderr)

        config["collection"]["minimum_profit_rate"] = 0.049
        self.save_config(config)
        below_minimum_rate = self.run_cli("show-config")
        self.assertEqual(below_minimum_rate.returncode, 2)
        self.assertIn("minimum_profit_rate", below_minimum_rate.stderr)

        config["collection"]["minimum_profit_rate"] = 0.05
        config["collection"]["price_threshold"] = 49.99
        self.save_config(config)
        changed_threshold = self.run_cli("show-config")
        self.assertEqual(changed_threshold.returncode, 2)
        self.assertIn("price_threshold", changed_threshold.stderr)

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
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)
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

    def test_record_pricing_uses_decimal_ceiling_and_preserves_prices_at_or_above_threshold(self) -> None:
        output_dir = self.prepare_for_pricing("多尺码外套", "COAT-DECIMAL")
        skus = [
            {
                "sku_key": "Black / S",
                "original_price": "49.99",
                "supplier_cost": "47.61",
                "final_price": "50.00",
                "cost_sheet": "在售",
                "cost_cell": "$H$2",
                "cost_raw_text": "47.61",
                "supplier_currency": "CNY",
                "supplier_unit": "piece",
                "supplier_sku_text": "Black / S",
                "product_query": "COAT-DECIMAL",
                "product_cell": "$A$2",
                "product_raw_text": "COAT-DECIMAL",
                "supplier_sku_cell": "$B$2",
            },
            {
                "sku_key": "Black / M",
                "original_price": "49.99",
                "supplier_cost": "47.60",
                "final_price": "49.99",
                "cost_sheet": "在售",
                "cost_cell": "$H$3",
                "cost_raw_text": "47.60",
                "supplier_currency": "CNY",
                "supplier_unit": "piece",
                "supplier_sku_text": "Black / M",
                "product_query": "COAT-DECIMAL",
                "product_cell": "$A$3",
                "product_raw_text": "COAT-DECIMAL",
                "supplier_sku_cell": "$B$3",
            },
            {
                "sku_key": "Black / L",
                "original_price": "49.99",
                "supplier_cost": "47.62",
                "final_price": "50.01",
                "cost_sheet": "在售",
                "cost_cell": "$H$4",
                "cost_raw_text": "CNY 47.62",
                "supplier_currency": "CNY",
                "supplier_unit": "piece",
                "supplier_sku_text": "Black / L",
                "product_query": "COAT-DECIMAL",
                "product_cell": "$A$4",
                "product_raw_text": "COAT-DECIMAL",
                "supplier_sku_cell": "$B$4",
            },
            {
                "sku_key": "Black / XL",
                "original_price": "50.00",
                "final_price": "50.00",
            },
            {
                "sku_key": "Black / 2XL",
                "original_price": "88.00",
                "final_price": "88.00",
            },
            {
                "sku_key": "Black / 3XL",
                "original_price": "0.10",
                "supplier_cost": "0.10",
                "final_price": "0.11",
                "cost_sheet": "在售",
                "cost_cell": "$H$7",
                "cost_raw_text": "0.10",
                "supplier_currency": "CNY",
                "supplier_unit": "piece",
                "supplier_sku_text": "Black / 3XL",
                "product_query": "COAT-DECIMAL",
                "product_cell": "$A$7",
                "product_raw_text": "COAT-DECIMAL",
                "supplier_sku_cell": "$B$7",
            },
            {
                "sku_key": "Black / 4XL",
                "original_price": "30.00",
                "supplier_cost": "30.01",
                "final_price": "31.52",
                "cost_sheet": "在售",
                "cost_cell": "$H$8",
                "cost_raw_text": "CNY 30.01",
                "supplier_currency": "CNY",
                "supplier_unit": "piece",
                "supplier_sku_text": "Black / 4XL",
                "product_query": "COAT-DECIMAL",
                "product_cell": "$A$8",
                "product_raw_text": "COAT-DECIMAL",
                "supplier_sku_cell": "$B$8",
            },
        ]
        result = self.record_pricing(output_dir, skus)
        self.assertEqual(result.returncode, 0, result.stderr)
        confirmation = self.entry_for(output_dir)["pricing_confirmation"]
        self.assertEqual(confirmation["currency"], "CNY")
        self.assertEqual(
            [sku["sku_key"] for sku in confirmation["skus"]],
            [sku["sku_key"] for sku in skus],
        )
        self.assertEqual(
            [sku["final_price"] for sku in confirmation["skus"]],
            [sku["final_price"] for sku in skus],
        )
        self.assertEqual(
            [sku.get("minimum_price") for sku in confirmation["skus"]],
            ["50.00", "49.98", "50.01", None, None, "0.11", "31.52"],
        )

    def test_record_pricing_rejects_wrong_final_price_at_decimal_boundaries(self) -> None:
        output_dir = self.prepare_for_pricing("边界价格商品", "PRICE-BOUNDARY")
        invalid_cases = [
            ("49.99", "47.61", "49.99"),
            ("49.99", "47.62", "50.00"),
            ("49.99", "40.00", "42.00"),
            ("50.00", None, "50.01"),
            ("60.00", None, "63.00"),
        ]
        for index, (original, cost, final) in enumerate(invalid_cases, start=1):
            sku = {
                "sku_key": f"Case {index}",
                "original_price": original,
                "final_price": final,
            }
            if cost is not None:
                sku.update(
                    supplier_cost=cost,
                    cost_sheet="在售",
                    cost_cell=f"$H${index + 1}",
                    cost_raw_text=cost,
                    supplier_currency="CNY",
                    supplier_unit="piece",
                    supplier_sku_text=f"Case {index}",
                    product_query="PRICE-BOUNDARY",
                    product_cell=f"$A${index + 1}",
                    product_raw_text="PRICE-BOUNDARY",
                    supplier_sku_cell=f"$B${index + 1}",
                )
            with self.subTest(original=original, cost=cost, final=final):
                result = self.record_pricing(output_dir, [sku])
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

    def test_record_pricing_requires_positive_plain_money_strings_with_at_most_two_decimals(self) -> None:
        output_dir = self.prepare_for_pricing("金额格式商品", "PRICE-FORMAT")
        invalid_originals = [49.99, "49.999", "0", "-1.00", "NaN", "Infinity", "1e1", ""]
        for original in invalid_originals:
            sku = {
                "sku_key": "Default",
                "original_price": original,
                "supplier_cost": "40.00",
                "final_price": "49.99",
                "cost_sheet": "在售",
                "cost_cell": "$H$2",
                "cost_raw_text": "40.00",
                "supplier_currency": "CNY",
                "supplier_unit": "piece",
                "supplier_sku_text": "Default",
                "product_query": "PRICE-FORMAT",
                "product_cell": "$A$2",
                "product_raw_text": "PRICE-FORMAT",
                "supplier_sku_cell": "$B$2",
            }
            with self.subTest(original=original):
                result = self.record_pricing(output_dir, [sku])
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        for field, value in (("supplier_cost", "47.611"), ("final_price", "50.001")):
            sku = {
                "sku_key": "Default",
                "original_price": "49.99",
                "supplier_cost": "47.61",
                "final_price": "50.00",
                "cost_sheet": "在售",
                "cost_cell": "$H$2",
                "cost_raw_text": "47.61",
                "supplier_currency": "CNY",
                "supplier_unit": "piece",
                "supplier_sku_text": "Default",
                "product_query": "PRICE-FORMAT",
                "product_cell": "$A$2",
                "product_raw_text": "PRICE-FORMAT",
                "supplier_sku_cell": "$B$2",
            }
            sku[field] = value
            with self.subTest(field=field, value=value):
                result = self.record_pricing(output_dir, [sku])
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

    def test_low_price_sku_requires_complete_supplier_cost_evidence(self) -> None:
        output_dir = self.prepare_for_pricing("成本证据商品", "COST-EVIDENCE")
        complete = {
            "sku_key": "Black / S",
            "original_price": "49.99",
            "supplier_cost": "47.61",
            "final_price": "50.00",
            "cost_sheet": "在售",
            "cost_cell": "$H$2",
            "cost_raw_text": "47.61",
            "supplier_currency": "CNY",
            "supplier_unit": "piece",
            "supplier_sku_text": "Black / S",
            "product_query": "COST-EVIDENCE",
            "product_cell": "$A$2",
            "product_raw_text": "COST-EVIDENCE",
            "supplier_sku_cell": "$B$2",
        }
        for missing_field in (
            "supplier_cost",
            "cost_sheet",
            "cost_cell",
            "cost_raw_text",
            "supplier_currency",
            "supplier_unit",
            "supplier_sku_text",
            "product_query",
            "product_cell",
            "product_raw_text",
            "supplier_sku_cell",
        ):
            sku = dict(complete)
            del sku[missing_field]
            with self.subTest(missing_field=missing_field):
                result = self.record_pricing(output_dir, [sku])
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        bad_cell = dict(complete, cost_cell="H0")
        result = self.record_pricing(output_dir, [bad_cell])
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        mismatched_currency = dict(complete, supplier_currency="USD")
        self.assertEqual(self.record_pricing(output_dir, [mismatched_currency]).returncode, 2)
        mismatched_unit = dict(complete, supplier_unit="set")
        self.assertEqual(self.record_pricing(output_dir, [mismatched_unit]).returncode, 2)
        mismatched_sku = dict(complete, supplier_sku_text="Black / M")
        self.assertEqual(self.record_pricing(output_dir, [mismatched_sku]).returncode, 2)

        mismatched_product = dict(complete, product_query="OTHER-CODE")
        self.assertEqual(self.record_pricing(output_dir, [mismatched_product]).returncode, 2)
        adjacent_row = dict(complete, cost_cell="$H$3")
        self.assertEqual(self.record_pricing(output_dir, [adjacent_row]).returncode, 2)

        mismatched_raw_text = dict(complete, cost_raw_text="46.61")
        result = self.record_pricing(output_dir, [mismatched_raw_text])
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        sold_out_cost = dict(complete, cost_sheet="售罄下架款")
        result = self.record_pricing(output_dir, [sold_out_cost])
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        negative_raw = dict(complete, cost_raw_text="-47.61")
        self.assertEqual(self.record_pricing(output_dir, [negative_raw]).returncode, 2)
        percent_raw = dict(complete, cost_raw_text="47.61%")
        self.assertEqual(self.record_pricing(output_dir, [percent_raw]).returncode, 2)
        wrong_currency_raw = dict(complete, cost_raw_text="USD 47.61")
        self.assertEqual(self.record_pricing(output_dir, [wrong_currency_raw]).returncode, 2)

    def test_record_pricing_requires_declared_retained_sku_count(self) -> None:
        output_dir = self.prepare_for_pricing("SKU数量商品", "SKU-COUNT")
        rows = [
            {"sku_key": "Black / S", "original_price": "50.00", "final_price": "50.00"},
            {"sku_key": "Black / M", "original_price": "51.00", "final_price": "51.00"},
        ]
        missing_row = self.record_pricing(output_dir, rows[:1], expected_sku_count=2)
        self.assertEqual(missing_row.returncode, 2)
        completed = self.record_pricing(output_dir, rows, expected_sku_count=2)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        evidence = self.entry_for(output_dir)["pricing_confirmation"]
        self.assertEqual(evidence["expected_sku_count"], 2)
        self.assertEqual(len(evidence["skus"]), 2)

    def test_record_pricing_rejects_invalid_currency_and_normalized_duplicate_skus(self) -> None:
        output_dir = self.prepare_for_pricing("币种与SKU商品", "SKU-NORMALIZE")
        valid = [{"sku_key": "Default", "original_price": "50.00", "final_price": "50.00"}]
        for currency in ("CN", "CNY1", "ZZZ", "¥", ""):
            with self.subTest(currency=currency):
                result = self.record_pricing(output_dir, valid, currency=currency)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        duplicates = [
            {"sku_key": " Black / Ｓ ", "original_price": "50.00", "final_price": "50.00"},
            {"sku_key": "black / s", "original_price": "51.00", "final_price": "51.00"},
        ]
        duplicate_result = self.record_pricing(output_dir, duplicates)
        self.assertEqual(duplicate_result.returncode, 2)
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        empty_result = self.record_pricing(output_dir, [])
        self.assertEqual(empty_result.returncode, 2)
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

    def test_record_pricing_requires_links_and_fresh_availability(self) -> None:
        output_dir = self.reserve("定价状态商品", "PRICE-STATE")
        self.assertEqual(self.confirm(output_dir, "PRICE-STATE").returncode, 0)
        before_links = self.record_pricing(output_dir)
        self.assertEqual(before_links.returncode, 2)

        recorded = self.run_cli("record-links", str(output_dir), "https://example.com/state")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        history = self.load_history()
        entry = next(item for item in history["products"] if item["product_code"] == "PRICE-STATE")
        entry["availability"]["checked_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        ).isoformat(timespec="microseconds")
        self.save_history(history)
        stale = self.record_pricing(output_dir)
        self.assertEqual(stale.returncode, 2)
        self.assertIn("stale", stale.stderr.casefold())
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

    def test_confirm_and_record_links_clear_previous_pricing_confirmation(self) -> None:
        output_dir = self.prepare_for_pricing("证据更新商品", "PRICE-REFRESH")
        first = self.record_pricing(output_dir)
        self.assertEqual(first.returncode, 0, first.stderr)

        reconfirm = self.confirm(output_dir, "PRICE-REFRESH")
        self.assertEqual(reconfirm.returncode, 0, reconfirm.stderr)
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

        second = self.record_pricing(output_dir)
        self.assertEqual(second.returncode, 0, second.stderr)
        same_links = self.run_cli(
            "record-links", str(output_dir), "https://example.com/price-refresh"
        )
        self.assertEqual(same_links.returncode, 0, same_links.stderr)
        self.assertNotIn("pricing_confirmation", self.entry_for(output_dir))

    def test_pricing_evidence_binds_availability_links_and_pricing_config(self) -> None:
        output_dir = self.prepare_for_pricing(
            "定价证据绑定商品", "PRICE-BINDING", deliverables=True
        )
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)

        history = self.load_history()
        entry = next(item for item in history["products"] if item["product_code"] == "PRICE-BINDING")
        confirmation = entry["pricing_confirmation"]
        self.assertEqual(
            confirmation["availability_checked_at"], entry["availability"]["checked_at"]
        )
        self.assertEqual(
            confirmation["availability_config_fingerprint"],
            entry["availability"]["config_fingerprint"],
        )
        self.assertEqual(confirmation["recorded_links"], entry["supplier_links"])
        self.assertEqual(confirmation["links_recorded_at"], entry["links_recorded_at"])
        checked_at = dt.datetime.fromisoformat(entry["availability"]["checked_at"])
        entry["availability"]["checked_at"] = (checked_at + dt.timedelta(microseconds=1)).isoformat(
            timespec="microseconds"
        )
        self.save_history(history)
        mismatched_availability = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/price-binding/long",
        )
        self.assertEqual(mismatched_availability.returncode, 2)
        self.assertIn("pricing", mismatched_availability.stderr.casefold())

        self.assertEqual(self.confirm(output_dir, "PRICE-BINDING").returncode, 0)
        refreshed = self.record_pricing(output_dir)
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)

        history = self.load_history()
        entry = next(item for item in history["products"] if item["product_code"] == "PRICE-BINDING")
        tampered_links = ["https://example.com/replaced-outside-workflow"]
        tampered_time = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(
            timespec="microseconds"
        )
        entry["supplier_links"] = tampered_links
        entry["links_recorded_at"] = tampered_time
        entry["availability"]["recorded_links"] = tampered_links
        entry["availability"]["recorded_links_at"] = tampered_time
        self.save_history(history)
        mismatched_links = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/replaced-outside-workflow/long",
        )
        self.assertEqual(mismatched_links.returncode, 2)
        self.assertIn("pricing", mismatched_links.stderr.casefold())

        self.assertEqual(self.confirm(output_dir, "PRICE-BINDING").returncode, 0)
        refreshed = self.record_pricing(output_dir)
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        config = self.load_config()
        config["collection"]["minimum_profit_rate"] = 0.06
        self.save_config(config)
        changed_config = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/price-binding/long",
        )
        self.assertEqual(changed_config.returncode, 2)
        self.assertIn("pricing", changed_config.stderr.casefold())

    def test_modified_pricing_sku_evidence_is_rejected_at_completion(self) -> None:
        output_dir = self.prepare_for_pricing(
            "定价防篡改商品", "PRICE-TAMPER", deliverables=True
        )
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)
        history = self.load_history()
        entry = next(item for item in history["products"] if item["product_code"] == "PRICE-TAMPER")
        entry["pricing_confirmation"]["skus"][0]["final_price"] = "50.01"
        self.save_history(history)
        result = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/price-tamper/long",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("pricing", result.stderr.casefold())

    def test_mark_collected_requires_pricing_confirmation(self) -> None:
        output_dir = self.prepare_for_pricing(
            "缺少定价确认商品", "PRICE-MISSING", deliverables=True
        )
        result = self.run_cli(
            "mark-collected",
            str(output_dir),
            "--resolved-url",
            "https://example.com/price-missing/long",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("pricing", result.stderr.casefold())
        self.assertEqual(self.entry_for(output_dir)["status"], "awaiting_miaoshou_collection")

    def test_mark_published_records_exact_store_product_ids_and_unlocks_video(self) -> None:
        output_dir = self.prepare_collected("发布成功商品", "PUBLISH-100")
        published = self.run_cli(
            "mark-published",
            str(output_dir),
            "--store",
            "Renova",
            "--product-id",
            "1729000000000000100",
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        payload = json.loads(published.stdout)
        self.assertFalse(payload["idempotent"])
        entry = self.entry_for(output_dir)
        self.assertEqual(entry["status"], "tiktok_published")
        self.assertEqual(
            entry["tiktok_publication"]["records"],
            [{"store": "Renova", "product_id": "1729000000000000100"}],
        )

        ready = self.run_cli("video-ready", str(output_dir))
        self.assertEqual(ready.returncode, 0, ready.stderr)
        ready_payload = json.loads(ready.stdout)
        self.assertTrue(ready_payload["ready"])
        self.assertEqual(Path(ready_payload["listing_output_dir"]), output_dir)
        self.assertEqual(ready_payload["product_code"], "PUBLISH-100")
        self.assertEqual(
            Path(ready_payload["source_dir"]), self.catalog / "发布成功商品"
        )

        repeated = self.run_cli(
            "mark-published",
            str(output_dir),
            "--store",
            "Renova",
            "--product-id",
            "1729000000000000100",
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertTrue(json.loads(repeated.stdout)["idempotent"])

    def test_mark_published_rejects_wrong_state_partial_or_changed_records(self) -> None:
        waiting = self.reserve("尚未采集商品", "WAIT-PUBLISH")
        wrong_state = self.run_cli(
            "mark-published",
            str(waiting),
            "--store",
            "Renova",
            "--product-id",
            "100",
        )
        self.assertEqual(wrong_state.returncode, 2)
        self.assertIn("status", wrong_state.stderr)
        self.assertEqual(self.run_cli("video-ready", str(waiting)).returncode, 2)

        output_dir = self.prepare_collected("发布参数错误商品", "PUBLISH-ERROR")
        missing_id = self.run_cli(
            "mark-published", str(output_dir), "--store", "Renova"
        )
        self.assertEqual(missing_id.returncode, 2)
        wrong_store = self.run_cli(
            "mark-published",
            str(output_dir),
            "--store",
            "Other Store",
            "--product-id",
            "101",
        )
        self.assertEqual(wrong_store.returncode, 2)
        self.assertEqual(self.entry_for(output_dir)["status"], "miaoshou_collected")

        self.assertEqual(
            self.run_cli(
                "mark-published",
                str(output_dir),
                "--store",
                "Renova",
                "--product-id",
                "101",
            ).returncode,
            0,
        )
        changed = self.run_cli(
            "mark-published",
            str(output_dir),
            "--store",
            "Renova",
            "--product-id",
            "102",
        )
        self.assertEqual(changed.returncode, 2)
        self.assertIn("different", changed.stderr)

    def test_mark_published_validates_multiple_stores_and_rolls_back_on_write_failure(self) -> None:
        config = self.load_config()
        config["publication"]["store_names"] = ["Renova", "Second Store"]
        config["publication"]["expected_store_count"] = 2
        self.save_config(config)
        output_dir = self.prepare_collected("双店发布商品", "PUBLISH-200")

        duplicate_id = self.run_cli(
            "mark-published",
            str(output_dir),
            "--store",
            "Renova",
            "--product-id",
            "201",
            "--store",
            "Second Store",
            "--product-id",
            "201",
        )
        self.assertEqual(duplicate_id.returncode, 2)
        partial = self.run_cli(
            "mark-published",
            str(output_dir),
            "--store",
            "Renova",
            "--product-id",
            "201",
        )
        self.assertEqual(partial.returncode, 2)

        spec = importlib.util.spec_from_file_location("listing_publish_rollback", WORKFLOW)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded_config = module.load_config(self.config_path)
        context = module.build_context(self.root, self.config_path, loaded_config)
        original = module.atomic_write_json

        def fail_history_commit(path: Path, data: dict) -> None:
            raise OSError("simulated publication failure")

        module.atomic_write_json = fail_history_commit
        try:
            with self.assertRaisesRegex(OSError, "simulated publication failure"):
                module.command_mark_published(
                    types.SimpleNamespace(
                        output_dir=str(output_dir),
                        stores=["Renova", "Second Store"],
                        product_ids=["201", "202"],
                    ),
                    context,
                )
        finally:
            module.atomic_write_json = original
        entry = self.entry_for(output_dir)
        self.assertEqual(entry["status"], "miaoshou_collected")
        self.assertNotIn("tiktok_publication", entry)

    def test_tiktok_product_id_cannot_bind_to_two_listing_records(self) -> None:
        first = self.prepare_collected("商品ID唯一一", "UNIQUE-ID-1")
        second = self.prepare_collected("商品ID唯一二", "UNIQUE-ID-2")
        self.assertEqual(
            self.run_cli(
                "mark-published",
                str(first),
                "--store",
                "Renova",
                "--product-id",
                "1729000000000000555",
            ).returncode,
            0,
        )
        duplicate = self.run_cli(
            "mark-published",
            str(second),
            "--store",
            "Renova",
            "--product-id",
            "1729000000000000555",
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("another listing", duplicate.stderr)
        self.assertEqual(self.entry_for(second)["status"], "miaoshou_collected")

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
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)
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

    def test_availability_can_be_disabled_for_links_and_high_prices(self) -> None:
        config = self.load_config()
        config["availability"]["required"] = False
        config["availability"]["document_url"] = ""
        self.save_config(config)
        output_dir = self.reserve("便携风扇", "FAN-6600")
        self.prepare_deliverables(output_dir)
        record = self.run_cli("record-links", str(output_dir), "https://example.com/item")
        self.assertEqual(record.returncode, 0, record.stderr)
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)
        self.assertIn("pricing_confirmation", self.entry_for(output_dir))

        low_price = [{
            "sku_key": "Default",
            "original_price": "49.99",
            "supplier_cost": "40.00",
            "final_price": "49.99",
            "cost_sheet": "在售",
            "cost_cell": "$H$2",
            "cost_raw_text": "40.00",
            "supplier_currency": "CNY",
            "supplier_unit": "piece",
            "supplier_sku_text": "Default",
            "product_query": "FAN-6600",
            "product_cell": "$A$2",
            "product_raw_text": "FAN-6600",
            "supplier_sku_cell": "$B$2",
        }]
        blocked = self.record_pricing(output_dir, low_price)
        self.assertEqual(blocked.returncode, 2)

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
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)
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
        pricing = self.record_pricing(output_dir)
        self.assertEqual(pricing.returncode, 0, pricing.stderr)
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
