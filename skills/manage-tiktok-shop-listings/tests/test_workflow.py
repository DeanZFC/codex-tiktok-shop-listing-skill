import copy
import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from PIL import Image
from unittest.mock import patch
from typing import get_type_hints

import yaml


def load_workflow():
    workflow_path = Path(__file__).parents[1] / "scripts" / "workflow.py"
    if not workflow_path.is_file():
        raise AssertionError(f"Workflow module is missing: {workflow_path}")
    spec = importlib.util.spec_from_file_location("swimwear_tiktok_workflow", workflow_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load workflow module from {workflow_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


workflow = load_workflow()


class FirstChoice:
    def choice(self, values):
        return values[0]


class LastChoice:
    def choice(self, values):
        return values[-1]


def source_snapshot(root: Path) -> dict[str, bytes]:
    ignored = {"图片制作", "视频制作"}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).parts[0] not in ignored
    }


def tree_snapshot(root: Path) -> dict[str, tuple]:
    if not root.exists() and not root.is_symlink():
        return {}
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("dir",)
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        else:
            snapshot[relative] = ("other", path.stat(follow_symlinks=False).st_mode)
    return snapshot


class WorkflowTest(unittest.TestCase):
    def test_minimal_workflow_contract(self):
        config_fields = fields(workflow.Config)

        self.assertEqual(
            [field.name for field in config_fields],
            [
                "catalog",
                "done_root",
                "state_file",
                "ffmpeg",
                "ffprobe",
                "link_history",
                "source_product",
            ],
        )
        self.assertEqual(
            [field.type for field in config_fields],
            [Path, Path, Path, str, str, Path | None, Path | None],
        )
        self.assertEqual(config_fields[3].default, "ffmpeg")
        self.assertEqual(config_fields[4].default, "ffprobe")
        self.assertIsNone(config_fields[5].default)

        config = workflow.Config(Path("catalog"), Path("done"), Path("state"))
        with self.assertRaises(FrozenInstanceError):
            config.catalog = Path("other-catalog")

        self.assertEqual(
            get_type_hints(workflow.main),
            {"argv": list[str] | None, "return": int},
        )

        help_output = StringIO()
        with self.assertRaises(SystemExit) as exit_context:
            with redirect_stdout(help_output):
                workflow.main(["--help"])
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn("Swimwear TikTok video workflow", help_output.getvalue())


class SelectionTests(unittest.TestCase):
    TASK_FIELDS = {
        "product_name",
        "source_dir",
        "task_dir",
        "image_dir",
        "video_dir",
        "stage",
        "reference",
        "image_baseline",
        "image_attempts",
        "video_baseline",
        "video_attempts",
        "frames",
        "videos",
        "merge",
        "created_at",
        "updated_at",
    }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = self.root / "catalog"
        self.done_root = self.root / "done"
        self.state_file = self.root / "state" / "state.json"
        self.catalog.mkdir()
        self.done_root.mkdir()
        self.product_names = [f"{index:03d}_product-{index}" for index in range(4)]
        for index, name in enumerate(self.product_names):
            product = self.catalog / name
            (product / "nested" / "deeper").mkdir(parents=True)
            (product / "top-level.bin").write_bytes(bytes([index, 0, 255]))
            (product / "nested" / "photo.jpg").write_bytes(
                b"jpeg-placeholder-" + str(index).encode("ascii")
            )
            (product / "nested" / "deeper" / "notes.txt").write_bytes(
                f"product {index}\n".encode("utf-8")
            )

    def config(
        self,
        *,
        catalog: Path | None = None,
        done_root: Path | None = None,
        state_file: Path | None = None,
    ):
        return workflow.Config(
            catalog or self.catalog,
            done_root or self.done_root,
            state_file or self.state_file,
            ffmpeg="test-ffmpeg",
            ffprobe="test-ffprobe",
        )

    def select_first(self, today: date = date(2026, 7, 15)):
        return workflow.select_product(self.config(), FirstChoice(), today)

    def archive_path(
        self, product_name: str, day: str = "2026-07-13", index: int = 1
    ) -> Path:
        return self.done_root / day / f"{index:03d}-{product_name}"

    def archive_existing(self, product_name: str, day: str = "2026-07-13") -> Path:
        date_dir = self.done_root / day
        date_dir.mkdir(exist_ok=True)
        indexes = [
            int(match.group(1))
            for path in date_dir.iterdir()
            if (match := workflow.TASK_DIR_PATTERN.fullmatch(path.name)) is not None
        ]
        archive = self.archive_path(product_name, day, max(indexes, default=0) + 1)
        archive.mkdir()
        return archive

    def write_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

    def read_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def cli_roots(self) -> list[str]:
        return [
            "--catalog",
            str(self.catalog),
            "--done-root",
            str(self.done_root),
            "--state-file",
            str(self.state_file),
            "--ffmpeg",
            "test-ffmpeg",
            "--ffprobe",
            "test-ffprobe",
        ]

    def test_config_defaults_stages_and_default_state_are_fixed(self):
        self.assertEqual(
            workflow.DEFAULT_CATALOG,
            workflow.DEFAULT_WORKSPACE_ROOT / "产品",
        )
        self.assertEqual(
            workflow.LEGACY_CATALOG,
            workflow.DEFAULT_WORKSPACE_ROOT / "旧产品",
        )
        self.assertEqual(
            workflow.DEFAULT_LINK_HISTORY,
            workflow.DEFAULT_WORKSPACE_ROOT / ".swimwear-link-selection/history.json",
        )
        self.assertEqual(workflow.DEFAULT_DONE_ROOT, workflow.DEFAULT_WORKSPACE_ROOT / "已做")
        self.assertEqual(
            workflow.DEFAULT_STATE_FILE,
            workflow.DEFAULT_WORKSPACE_ROOT / ".swimwear-tiktok-video-workflow/state.json",
        )
        self.assertEqual(
            workflow.STAGES,
            (
                "selected",
                "reference-ready",
                "awaiting-image-1",
                "image-1-ready",
                "awaiting-image-2",
                "image-2-ready",
                "awaiting-image-3",
                "frames-ready",
                "awaiting-videos",
                "videos-ready",
                "merged",
            ),
        )
        self.assertEqual(workflow.default_state(), {"version": 1, "active_task": None})

    def test_open_pinned_directory_closes_descriptor_when_fstat_fails(self):
        real_open = os.open
        real_fstat = os.fstat
        opened = []

        def tracked_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        caught = None
        with patch.object(workflow.os, "open", side_effect=tracked_open), patch.object(
            workflow.os,
            "fstat",
            side_effect=OSError(errno.EIO, "forced fstat failure"),
        ):
            try:
                workflow._open_pinned_directory(self.catalog, "商品目录")
            except BaseException as exc:
                caught = exc

        self.assertEqual(len(opened), 1)
        descriptor_open = True
        try:
            real_fstat(opened[0])
        except OSError as exc:
            self.assertEqual(exc.errno, errno.EBADF)
            descriptor_open = False
        finally:
            if descriptor_open:
                os.close(opened[0])

        self.assertFalse(descriptor_open, "fstat 失败后目录描述符必须关闭")
        self.assertIsInstance(caught, workflow.WorkflowError)

    def test_select_archives_first_choice_with_exact_task_and_state_contract(self):
        task = self.select_first()
        source = self.catalog / self.product_names[0]
        expected = self.archive_path(self.product_names[0], "2026-07-15")

        self.assertEqual(set(task), self.TASK_FIELDS)
        self.assertEqual(task["product_name"], self.product_names[0])
        self.assertEqual(Path(task["source_dir"]), source.resolve())
        self.assertEqual(Path(task["task_dir"]), expected.resolve())
        self.assertEqual(Path(task["image_dir"]), (expected / "图片制作").resolve())
        self.assertEqual(Path(task["video_dir"]), (expected / "视频制作").resolve())
        self.assertEqual(task["stage"], "selected")
        self.assertIsNone(task["reference"])
        self.assertEqual(task["image_baseline"], {})
        self.assertEqual(task["image_attempts"], {})
        self.assertEqual(task["video_baseline"], {})
        self.assertEqual(task["video_attempts"], {})
        self.assertEqual(task["frames"], {})
        self.assertEqual(task["videos"], [])
        self.assertIsNone(task["merge"])
        self.assertEqual(source_snapshot(expected), source_snapshot(source))
        self.assertTrue((expected / "图片制作").is_dir())
        self.assertTrue((expected / "视频制作").is_dir())
        self.assertEqual(self.read_state(), {"version": 1, "active_task": task})

    def test_same_day_numbering_increments_from_001(self):
        first = self.select_first(date(2026, 7, 15))
        state = self.read_state()
        state["active_task"]["stage"] = "merged"
        workflow.save_state_atomic(self.config(), state)

        second = workflow.select_product(
            self.config(), FirstChoice(), date(2026, 7, 15)
        )

        self.assertEqual(Path(first["task_dir"]).name, f"001-{self.product_names[0]}")
        self.assertEqual(Path(second["task_dir"]).name, f"002-{self.product_names[1]}")

    def test_new_date_restarts_numbering_from_001(self):
        self.select_first(date(2026, 7, 15))
        state = self.read_state()
        state["active_task"]["stage"] = "merged"
        workflow.save_state_atomic(self.config(), state)

        second = workflow.select_product(
            self.config(), FirstChoice(), date(2026, 7, 16)
        )

        self.assertEqual(Path(second["task_dir"]).parent.name, "2026-07-16")
        self.assertEqual(Path(second["task_dir"]).name, f"001-{self.product_names[1]}")

    def test_completed_products_are_deduplicated_across_date_directories(self):
        self.archive_existing(self.product_names[0], "2026-07-13")
        self.archive_existing(self.product_names[1], "2026-07-14")

        self.assertEqual(
            workflow.completed_product_names(self.done_root),
            set(self.product_names[:2]),
        )

    def test_completed_scan_ignores_invalid_dates_and_non_product_entries(self):
        self.archive_existing(self.product_names[0], "2026-02-30")
        (self.done_root / "not-a-date" / f"001-{self.product_names[1]}").mkdir(
            parents=True
        )
        valid_date = self.done_root / "2026-07-13"
        valid_date.mkdir()
        (valid_date / "notes.txt").write_text("ignore", encoding="utf-8")
        (valid_date / f"bad-{self.product_names[2]}").mkdir()

        self.assertEqual(workflow.completed_product_names(self.done_root), set())

    def test_completed_scan_rejects_date_and_product_symlinks(self):
        outside_date = self.root / "outside-date"
        outside_date.mkdir()
        date_link = self.done_root / "2026-07-13"
        date_link.symlink_to(outside_date, target_is_directory=True)
        with self.assertRaisesRegex(workflow.WorkflowError, "日期目录.*符号链接"):
            workflow.completed_product_names(self.done_root)

        date_link.unlink()
        date_link.mkdir()
        outside_product = self.root / "outside-product"
        outside_product.mkdir()
        product_link = date_link / f"001-{self.product_names[0]}"
        product_link.symlink_to(outside_product, target_is_directory=True)
        with self.assertRaisesRegex(workflow.WorkflowError, "产品归档目录.*符号链接"):
            workflow.completed_product_names(self.done_root)

    def test_select_skips_only_valid_dated_completed_archives(self):
        self.archive_existing(self.product_names[0], "2026-07-13")
        self.archive_existing(self.product_names[1], "2026-02-30")
        (self.done_root / "not-a-date" / f"001-{self.product_names[2]}").mkdir(
            parents=True
        )

        task = self.select_first()

        self.assertEqual(task["product_name"], self.product_names[1])

    def test_select_restores_unmerged_active_without_modifying_anything(self):
        first = self.select_first()
        task_before = tree_snapshot(Path(first["task_dir"]))
        state_before = self.state_file.read_bytes()
        done_before = tree_snapshot(self.done_root)

        restored = workflow.select_product(
            self.config(), LastChoice(), date(2026, 7, 16)
        )

        self.assertEqual(restored, first)
        self.assertEqual(self.state_file.read_bytes(), state_before)
        self.assertEqual(tree_snapshot(Path(first["task_dir"])), task_before)
        self.assertEqual(tree_snapshot(self.done_root), done_before)

    def test_select_after_merged_archives_a_new_product(self):
        first = self.select_first()
        state = self.read_state()
        state["active_task"]["stage"] = "merged"
        workflow.save_state_atomic(self.config(), state)

        second = workflow.select_product(
            self.config(), FirstChoice(), date(2026, 7, 16)
        )

        self.assertEqual(second["product_name"], self.product_names[1])
        self.assertEqual(Path(second["task_dir"]).parent.name, "2026-07-16")
        self.assertEqual(Path(second["task_dir"]).name, f"001-{self.product_names[1]}")
        self.assertTrue(Path(first["task_dir"]).is_dir())

    def test_replace_keeps_old_archive_and_never_makes_it_selectable_again(self):
        old = self.select_first()
        sentinel = Path(old["task_dir"]) / "manual-sentinel.bin"
        sentinel.write_bytes(b"preserve-me")

        replacement = workflow.replace_product(
            self.config(), FirstChoice(), date(2026, 7, 15)
        )

        self.assertEqual(replacement["product_name"], self.product_names[1])
        self.assertTrue(Path(old["task_dir"]).is_dir())
        self.assertEqual(sentinel.read_bytes(), b"preserve-me")
        state = self.read_state()
        state["active_task"]["stage"] = "merged"
        workflow.save_state_atomic(self.config(), state)
        third = workflow.select_product(
            self.config(), FirstChoice(), date(2026, 7, 16)
        )
        self.assertEqual(third["product_name"], self.product_names[2])

    def test_replace_is_allowed_only_in_selected_or_reference_ready(self):
        first = self.select_first()
        state = self.read_state()
        state["active_task"]["stage"] = "reference-ready"
        workflow.save_state_atomic(self.config(), state)
        replacement = workflow.replace_product(
            self.config(), FirstChoice(), date(2026, 7, 15)
        )
        self.assertNotEqual(replacement["product_name"], first["product_name"])

        for stage in set(workflow.STAGES) - {"selected", "reference-ready"}:
            state = self.read_state()
            state["active_task"]["stage"] = stage
            workflow.save_state_atomic(self.config(), state)
            before = self.state_file.read_bytes()
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(workflow.WorkflowError, "当前阶段不能换产品"):
                    workflow.replace_product(
                        self.config(), FirstChoice(), date(2026, 7, 16)
                    )
                self.assertEqual(self.state_file.read_bytes(), before)

    def test_force_replace_explicitly_abandons_late_stage_and_preserves_archive(self):
        old = self.select_first()
        manual_video = Path(old["video_dir"]) / "manual-finished.mp4"
        manual_video.write_bytes(b"manual-video-bytes")
        state = self.read_state()
        state["active_task"]["stage"] = "awaiting-image-1"
        workflow.save_state_atomic(self.config(), state)

        with self.assertRaisesRegex(workflow.WorkflowError, "当前阶段不能换产品"):
            workflow.replace_product(
                self.config(), FirstChoice(), date(2026, 7, 16)
            )

        replacement = workflow.replace_product(
            self.config(), FirstChoice(), date(2026, 7, 16), force=True
        )

        self.assertNotEqual(replacement["product_name"], old["product_name"])
        self.assertEqual(manual_video.read_bytes(), b"manual-video-bytes")
        self.assertTrue(Path(old["task_dir"]).is_dir())
        self.assertEqual(self.read_state()["active_task"], replacement)

    def test_force_replace_still_requires_an_unmerged_active_task(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "当前阶段不能换产品"):
            workflow.replace_product(
                self.config(), FirstChoice(), date(2026, 7, 16), force=True
            )

        active = self.select_first()
        state = self.read_state()
        state["active_task"]["stage"] = "merged"
        workflow.save_state_atomic(self.config(), state)
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "当前阶段不能换产品"):
            workflow.replace_product(
                self.config(), FirstChoice(), date(2026, 7, 16), force=True
            )
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertTrue(Path(active["task_dir"]).is_dir())

    def test_select_reports_clear_error_when_all_one_hundred_are_done(self):
        for index in range(4, 100):
            name = f"{index:03d}_product-{index}"
            (self.catalog / name).mkdir()
            self.product_names.append(name)
        for name in self.product_names:
            self.archive_existing(name)

        with self.assertRaisesRegex(workflow.WorkflowError, "没有未做产品"):
            workflow.select_product(self.config(), FirstChoice(), date(2026, 7, 15))

    def test_discovery_and_archive_entry_both_reject_top_level_product_symlink(self):
        real_product = self.catalog / self.product_names[0]
        linked_product = self.catalog / "999_linked-product"
        linked_product.symlink_to(real_product, target_is_directory=True)

        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow.discover_products(self.catalog)
        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow._create_task(
                self.config(), linked_product, date(2026, 7, 15)
            )
        self.assertFalse(self.state_file.exists())

    def test_archive_copy_remains_anchored_when_product_path_is_swapped_and_restored(self):
        product = self.catalog / self.product_names[0]
        backup = self.catalog / "original-product-backup"
        attacker = self.root / "attacker-product"
        attacker.mkdir()
        (attacker / "top-level.bin").write_bytes(b"attacker-content")
        original_copytree = shutil.copytree

        def swapping_copytree(source, destination, *args, **kwargs):
            product.rename(backup)
            attacker.rename(product)
            try:
                return original_copytree(source, destination, *args, **kwargs)
            finally:
                product.rename(attacker)
                backup.rename(product)

        with patch.object(workflow.shutil, "copytree", side_effect=swapping_copytree):
            task = self.select_first()

        archived = Path(task["task_dir"])
        self.assertEqual(
            (archived / "top-level.bin").read_bytes(),
            (product / "top-level.bin").read_bytes(),
        )
        self.assertNotEqual((archived / "top-level.bin").read_bytes(), b"attacker-content")

    def test_archive_recursive_copy_never_uses_path_based_copytree(self):
        with patch.object(
            workflow.shutil,
            "copytree",
            side_effect=AssertionError("path-based copy is forbidden"),
        ):
            task = self.select_first()

        self.assertEqual(task["product_name"], self.product_names[0])

    def test_archive_rejects_internal_file_symlink_without_copying_external_bytes(self):
        outside = self.root / "outside-secret.bin"
        outside.write_bytes(b"must-not-be-copied")
        link = self.catalog / self.product_names[0] / "nested" / "secret-link.bin"
        link.symlink_to(outside)
        done_before = tree_snapshot(self.done_root)

        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            self.select_first()

        self.assertEqual(tree_snapshot(self.done_root), done_before)
        self.assertFalse(self.state_file.exists())

    def test_archive_rejects_internal_directory_symlink_without_traversal(self):
        outside = self.root / "outside-directory"
        outside.mkdir()
        (outside / "secret.bin").write_bytes(b"must-not-be-copied")
        link = self.catalog / self.product_names[0] / "linked-directory"
        link.symlink_to(outside, target_is_directory=True)
        done_before = tree_snapshot(self.done_root)

        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            self.select_first()

        self.assertEqual(tree_snapshot(self.done_root), done_before)
        self.assertFalse(self.state_file.exists())

    def test_select_rejects_datetime_before_creating_archive_or_state(self):
        done_before = tree_snapshot(self.done_root)

        with self.assertRaisesRegex(workflow.WorkflowError, "日期"):
            workflow.select_product(
                self.config(), FirstChoice(), datetime(2026, 7, 15, 12, 30)
            )

        self.assertEqual(tree_snapshot(self.done_root), done_before)
        self.assertFalse(self.state_file.exists())

    def test_overlapping_roots_are_rejected_before_any_write(self):
        cases = []
        same = self.root / "same"
        same.mkdir()
        cases.append((same, same))
        outer_catalog = self.root / "outer-catalog"
        inner_done = outer_catalog / "done"
        inner_done.mkdir(parents=True)
        cases.append((outer_catalog, inner_done))
        outer_done = self.root / "outer-done"
        inner_catalog = outer_done / "catalog"
        inner_catalog.mkdir(parents=True)
        cases.append((inner_catalog, outer_done))

        for index, (catalog, done_root) in enumerate(cases):
            state_file = self.root / f"overlap-state-{index}.json"
            before = tree_snapshot(self.root)
            with self.subTest(catalog=catalog, done_root=done_root):
                with self.assertRaisesRegex(workflow.WorkflowError, "重叠|包含"):
                    workflow.select_product(
                        self.config(
                            catalog=catalog,
                            done_root=done_root,
                            state_file=state_file,
                        ),
                        FirstChoice(),
                        date(2026, 7, 15),
                    )
                self.assertFalse(state_file.exists())
                self.assertEqual(tree_snapshot(self.root), before)

    def test_done_root_file_or_symlink_is_never_deleted_or_followed(self):
        real_done = self.root / "real-done"
        real_done.mkdir()
        sentinel = real_done / "sentinel.bin"
        sentinel.write_bytes(b"untouched")
        done_link = self.root / "done-link"
        done_link.symlink_to(real_done, target_is_directory=True)
        done_file = self.root / "done-file"
        done_file.write_bytes(b"not-a-directory")

        for index, invalid_done in enumerate((done_link, done_file)):
            state_file = self.root / f"invalid-done-state-{index}.json"
            with self.subTest(done_root=invalid_done):
                with self.assertRaises(workflow.WorkflowError):
                    workflow.select_product(
                        self.config(done_root=invalid_done, state_file=state_file),
                        FirstChoice(),
                        date(2026, 7, 15),
                    )
                self.assertFalse(state_file.exists())
                self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertTrue(done_link.is_symlink())
        self.assertEqual(done_file.read_bytes(), b"not-a-directory")

    def test_existing_target_file_or_symlink_is_never_deleted_or_overwritten(self):
        target = self.archive_path(self.product_names[0], "2026-07-15")
        target.parent.mkdir()
        target.write_bytes(b"target-file")
        with self.assertRaises(workflow.WorkflowError):
            self.select_first()
        self.assertEqual(target.read_bytes(), b"target-file")
        self.assertFalse(self.state_file.exists())

        target.unlink()
        protected = self.root / "protected"
        protected.mkdir()
        sentinel = protected / "sentinel.bin"
        sentinel.write_bytes(b"protected")
        target.symlink_to(protected, target_is_directory=True)
        with self.assertRaises(workflow.WorkflowError):
            self.select_first()
        self.assertTrue(target.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b"protected")
        self.assertFalse(self.state_file.exists())

    def test_copy_failure_cleans_hidden_temp_and_preserves_state_and_done(self):
        state_before = b'{"active_task":null,"version":1}\n'
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_bytes(state_before)
        existing = self.done_root / "existing-archive"
        existing.mkdir()
        (existing / "sentinel.bin").write_bytes(b"keep")
        done_before = tree_snapshot(self.done_root)
        socket_path = self.catalog / self.product_names[0] / "unsupported.sock"
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(unix_socket.close)
        unix_socket.bind(str(socket_path))

        with self.assertRaises(workflow.WorkflowError):
            self.select_first()

        self.assertEqual(self.state_file.read_bytes(), state_before)
        self.assertEqual(tree_snapshot(self.done_root), done_before)
        self.assertFalse(any(path.name.startswith(".") for path in self.done_root.iterdir()))

    def test_base_exception_during_copy_cleans_hidden_temp_without_wrapping(self):
        class StopCopy(BaseException):
            pass

        def interrupted_copy(source_fd, destination_fd, destination_name):
            os.mkdir(destination_name, dir_fd=destination_fd)
            raise StopCopy("interrupt")

        with patch.object(
            workflow, "_copy_tree_anchored", side_effect=interrupted_copy
        ):
            with self.assertRaises(StopCopy):
                self.select_first()

        self.assertFalse(self.state_file.exists())
        self.assertEqual(list(self.done_root.iterdir()), [])

    def test_concurrent_real_directory_is_reused_without_overwrite(self):
        target = self.archive_path(self.product_names[0], "2026-07-15")
        original_anchored_copy = workflow._copy_tree_anchored
        original_copytree = shutil.copytree
        injected = False

        def concurrent_copy(source_fd, destination_fd, destination_name):
            nonlocal injected
            result = original_anchored_copy(
                source_fd, destination_fd, destination_name
            )
            if not injected:
                injected = True
                original_copytree(
                    self.catalog / self.product_names[0], target, symlinks=True
                )
                (target / "图片制作").mkdir()
                (target / "视频制作").mkdir()
                (target / "concurrent-sentinel.bin").write_bytes(b"other-writer")
            return result

        with patch.object(
            workflow, "_copy_tree_anchored", side_effect=concurrent_copy
        ):
            task = self.select_first()

        self.assertEqual(Path(task["task_dir"]), target.resolve())
        self.assertEqual((target / "concurrent-sentinel.bin").read_bytes(), b"other-writer")
        self.assertFalse(any(path.name.startswith(".") for path in self.done_root.iterdir()))

    def test_concurrent_non_directory_target_is_not_overwritten(self):
        target = self.archive_path(self.product_names[0], "2026-07-15")
        original_anchored_copy = workflow._copy_tree_anchored
        injected = False

        def concurrent_copy(source_fd, destination_fd, destination_name):
            nonlocal injected
            result = original_anchored_copy(
                source_fd, destination_fd, destination_name
            )
            if not injected:
                injected = True
                target.write_bytes(b"concurrent-file")
            return result

        with patch.object(
            workflow, "_copy_tree_anchored", side_effect=concurrent_copy
        ):
            with self.assertRaises(workflow.WorkflowError):
                self.select_first()

        self.assertEqual(target.read_bytes(), b"concurrent-file")
        self.assertFalse(self.state_file.exists())
        self.assertFalse(any(path.name.startswith(".") for path in self.done_root.iterdir()))

    def test_state_rejects_unknown_fields_invalid_paths_stage_date_and_task_name(self):
        self.select_first()
        valid_state = self.read_state()
        invalid_states = []

        top_level = copy.deepcopy(valid_state)
        top_level["unknown"] = True
        invalid_states.append(top_level)
        missing_field = copy.deepcopy(valid_state)
        missing_field["active_task"].pop("merge")
        invalid_states.append(missing_field)
        extra_field = copy.deepcopy(valid_state)
        extra_field["active_task"]["unknown"] = True
        invalid_states.append(extra_field)
        invalid_stage = copy.deepcopy(valid_state)
        invalid_stage["active_task"]["stage"] = "not-a-stage"
        invalid_states.append(invalid_stage)
        outside_source = copy.deepcopy(valid_state)
        outside_source["active_task"]["source_dir"] = str(self.root / "outside-product")
        invalid_states.append(outside_source)
        outside_task = copy.deepcopy(valid_state)
        outside_task["active_task"]["task_dir"] = str(self.root / "outside-task")
        invalid_states.append(outside_task)
        invalid_date = copy.deepcopy(valid_state)
        invalid_date["active_task"]["task_dir"] = str(
            self.archive_path(self.product_names[0], "2026-02-30")
        )
        invalid_states.append(invalid_date)
        mismatched_name = copy.deepcopy(valid_state)
        mismatched_name["active_task"]["task_dir"] = str(
            self.archive_path(self.product_names[1], "2026-07-15")
        )
        invalid_states.append(mismatched_name)
        outside_image = copy.deepcopy(valid_state)
        outside_image["active_task"]["image_dir"] = str(self.root / "图片制作")
        invalid_states.append(outside_image)
        outside_frame = copy.deepcopy(valid_state)
        outside_frame["active_task"]["frames"] = {
            "1": str(self.root / "outside-frame.png")
        }
        invalid_states.append(outside_frame)
        outside_video = copy.deepcopy(valid_state)
        outside_video["active_task"]["videos"] = [
            str(self.root / "outside-video.mp4")
        ]
        invalid_states.append(outside_video)

        for invalid_state in invalid_states:
            with self.subTest(state=invalid_state):
                self.write_state(invalid_state)
                with self.assertRaises(workflow.WorkflowError):
                    workflow.load_state(self.config())

    def test_atomic_state_write_rejects_overlapping_roots_before_writing(self):
        same_root = self.root / "same-state-root"
        same_root.mkdir()
        state_file = self.root / "must-not-exist.json"
        config = self.config(
            catalog=same_root,
            done_root=same_root,
            state_file=state_file,
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "重叠|包含"):
            workflow.save_state_atomic(config, workflow.default_state())

        self.assertFalse(state_file.exists())

    def test_atomic_state_write_failure_preserves_old_state_and_removes_temp(self):
        self.select_first()
        old_bytes = self.state_file.read_bytes()
        state = self.read_state()
        state["active_task"]["updated_at"] = "2026-07-15T12:00:00+00:00"

        with patch.object(workflow.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(workflow.WorkflowError):
                workflow.save_state_atomic(self.config(), state)

        self.assertEqual(self.state_file.read_bytes(), old_bytes)
        self.assertFalse(
            any(
                path.name.endswith(".tmp")
                for path in self.state_file.parent.iterdir()
                if path != self.state_file
            )
        )
        self.assertEqual(workflow.load_state(self.config()), self.read_state())

    def test_select_state_save_failure_rolls_back_new_archive_and_pending_record(self):
        done_before = tree_snapshot(self.done_root)

        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=workflow.WorkflowError("injected state failure"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "state failure"):
                self.select_first()

        self.assertEqual(tree_snapshot(self.done_root), done_before)
        self.assertFalse(self.state_file.exists())
        self.assertNotIn(
            self.product_names[0], workflow.completed_product_names(self.done_root)
        )

    def test_select_recovers_pending_archive_after_post_publish_base_exception(self):
        class SimulatedCrash(BaseException):
            pass

        with patch.object(
            workflow,
            "_after_archive_published",
            side_effect=SimulatedCrash("after publish"),
        ):
            with self.assertRaises(SimulatedCrash):
                self.select_first()

        target = self.archive_path(self.product_names[0], "2026-07-15")
        pending = [
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        ]
        self.assertTrue(target.is_dir())
        self.assertEqual(len(pending), 1)
        self.assertFalse(self.state_file.exists())
        self.assertNotIn(
            self.product_names[0], workflow.completed_product_names(self.done_root)
        )

        recovered = workflow.select_product(
            self.config(), LastChoice(), date(2026, 7, 16)
        )

        self.assertEqual(Path(recovered["task_dir"]), target.resolve())
        self.assertEqual(recovered["product_name"], self.product_names[0])
        self.assertEqual(self.read_state()["active_task"], recovered)
        self.assertFalse(
            any(
                path.name.startswith(".swimwear-pending-")
                for path in self.done_root.iterdir()
            )
        )

    def test_pending_recovery_rejects_same_name_foreign_directory(self):
        class SimulatedCrash(BaseException):
            pass

        with patch.object(
            workflow,
            "_after_archive_published",
            side_effect=SimulatedCrash("after publish"),
        ):
            with self.assertRaises(SimulatedCrash):
                self.select_first()

        target = self.archive_path(self.product_names[0], "2026-07-15")
        original_archive = self.root / "original-published-archive"
        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        pending_before = pending.read_bytes()
        target.rename(original_archive)
        target.mkdir()
        (target / "图片制作").mkdir()
        (target / "视频制作").mkdir()
        (target / "foreign.bin").write_bytes(b"external-owner")
        foreign_before = tree_snapshot(target)

        with self.assertRaisesRegex(workflow.WorkflowError, "身份|替换"):
            workflow.select_product(
                self.config(), LastChoice(), date(2026, 7, 16)
            )

        self.assertFalse(self.state_file.exists())
        self.assertEqual(tree_snapshot(target), foreign_before)
        self.assertTrue(original_archive.is_dir())
        self.assertTrue(pending.is_file())
        self.assertEqual(pending.read_bytes(), pending_before)

    def test_new_target_pending_persists_published_inode(self):
        class SimulatedCrash(BaseException):
            pass

        with patch.object(
            workflow,
            "_after_archive_published",
            side_effect=SimulatedCrash("after publish"),
        ):
            with self.assertRaises(SimulatedCrash):
                self.select_first()

        target = self.archive_path(self.product_names[0], "2026-07-15")
        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        record = json.loads(pending.read_text(encoding="utf-8"))
        identity = target.stat()
        self.assertEqual(record["phase"], "published")
        self.assertEqual(
            record["target_identity"],
            {"dev": identity.st_dev, "ino": identity.st_ino},
        )

    def test_reused_target_pending_persists_reused_inode(self):
        class SimulatedCrash(BaseException):
            pass

        target = self.archive_path(self.product_names[0], "2026-07-15")
        original_anchored_copy = workflow._copy_tree_anchored
        original_copytree = shutil.copytree
        injected = False

        def concurrent_copy(source_fd, destination_fd, destination_name):
            nonlocal injected
            result = original_anchored_copy(
                source_fd, destination_fd, destination_name
            )
            if not injected:
                injected = True
                original_copytree(
                    self.catalog / self.product_names[0], target, symlinks=True
                )
                (target / "图片制作").mkdir()
                (target / "视频制作").mkdir()
            return result

        with (
            patch.object(
                workflow, "_copy_tree_anchored", side_effect=concurrent_copy
            ),
            patch.object(
                workflow,
                "_after_archive_published",
                side_effect=SimulatedCrash("after reused publish"),
            ),
        ):
            with self.assertRaises(SimulatedCrash):
                self.select_first()

        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        record = json.loads(pending.read_text(encoding="utf-8"))
        identity = target.stat()
        self.assertEqual(record["phase"], "published")
        self.assertEqual(
            record["target_identity"],
            {"dev": identity.st_dev, "ino": identity.st_ino},
        )

    def test_prepared_pending_after_publish_crash_fails_closed(self):
        class SimulatedCrash(BaseException):
            pass

        with patch.object(
            workflow,
            "_after_target_published_before_pending_update",
            side_effect=SimulatedCrash("before pending update"),
        ):
            with self.assertRaises(SimulatedCrash):
                self.select_first()

        target = self.archive_path(self.product_names[0], "2026-07-15")
        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        target_before = tree_snapshot(target)
        pending_before = pending.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "prepared|发布阶段|身份"):
            workflow.select_product(
                self.config(), LastChoice(), date(2026, 7, 16)
            )

        self.assertFalse(self.state_file.exists())
        self.assertEqual(tree_snapshot(target), target_before)
        self.assertEqual(pending.read_bytes(), pending_before)

    def test_replace_pending_recovers_when_base_state_is_unchanged(self):
        old = self.select_first()
        base_state = self.read_state()
        expected_digest = hashlib.sha256(
            json.dumps(
                base_state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        class SimulatedCrash(BaseException):
            pass

        with patch.object(
            workflow,
            "_after_archive_published",
            side_effect=SimulatedCrash("replacement published"),
        ):
            with self.assertRaises(SimulatedCrash):
                workflow.replace_product(
                    self.config(), FirstChoice(), date(2026, 7, 15)
                )

        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        record = json.loads(pending.read_text(encoding="utf-8"))
        replacement_task = record["task"]
        self.assertEqual(record["base_state_digest"], expected_digest)
        self.assertNotEqual(replacement_task["product_name"], old["product_name"])

        recovered_state = workflow._status_state(self.config())

        self.assertEqual(recovered_state["active_task"], replacement_task)
        self.assertEqual(self.read_state(), recovered_state)
        self.assertFalse(pending.exists())

    def test_replace_pending_rejects_legal_state_drift_without_modifying_evidence(self):
        old = self.select_first()

        class SimulatedCrash(BaseException):
            pass

        with patch.object(
            workflow,
            "_after_archive_published",
            side_effect=SimulatedCrash("replacement published"),
        ):
            with self.assertRaises(SimulatedCrash):
                workflow.replace_product(
                    self.config(), FirstChoice(), date(2026, 7, 15)
                )

        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        replacement = json.loads(pending.read_text(encoding="utf-8"))["task"]
        self.assertNotEqual(replacement["product_name"], old["product_name"])

        drifted_state = self.read_state()
        drifted_state["active_task"]["stage"] = "reference-ready"
        drifted_state["active_task"]["updated_at"] = "2026-07-15T13:00:00+00:00"
        workflow.save_state_atomic(self.config(), drifted_state)

        state_before = self.state_file.read_bytes()
        pending_before = pending.read_bytes()
        old_tree_before = tree_snapshot(Path(old["task_dir"]))
        replacement_tree_before = tree_snapshot(Path(replacement["task_dir"]))

        with self.assertRaisesRegex(workflow.WorkflowError, "base|冲突"):
            workflow._status_state(self.config())

        self.assertEqual(self.state_file.read_bytes(), state_before)
        self.assertEqual(pending.read_bytes(), pending_before)
        self.assertEqual(tree_snapshot(Path(old["task_dir"])), old_tree_before)
        self.assertEqual(
            tree_snapshot(Path(replacement["task_dir"])), replacement_tree_before
        )

    def test_pending_is_bound_to_canonical_state_file_workflow(self):
        class SimulatedCrash(BaseException):
            pass

        config_a = self.config()
        state_file_b = self.root / "state-b" / "state.json"
        config_b = self.config(state_file=state_file_b)
        with patch.object(
            workflow,
            "_after_archive_published",
            side_effect=SimulatedCrash("workflow A published"),
        ):
            with self.assertRaises(SimulatedCrash):
                workflow.select_product(
                    config_a, FirstChoice(), date(2026, 7, 15)
                )

        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        pending_before = pending.read_bytes()

        status_b = workflow._status_state(config_b)

        self.assertEqual(status_b, workflow.default_state())
        self.assertFalse(state_file_b.exists())
        self.assertTrue(pending.is_file())
        self.assertEqual(pending.read_bytes(), pending_before)

        relative_state_a = Path(os.path.relpath(self.state_file, Path.cwd()))
        equivalent_config_a = self.config(state_file=relative_state_a)
        recovered_a = workflow._status_state(equivalent_config_a)

        self.assertEqual(recovered_a["active_task"]["product_name"], self.product_names[0])
        self.assertEqual(self.read_state(), recovered_a)
        self.assertFalse(pending.exists())

    def test_select_after_merged_pending_recovers_when_base_state_is_unchanged(self):
        old = self.select_first()
        state = self.read_state()
        state["active_task"]["stage"] = "merged"
        workflow.save_state_atomic(self.config(), state)

        class SimulatedCrash(BaseException):
            pass

        with patch.object(
            workflow,
            "_after_archive_published",
            side_effect=SimulatedCrash("next selection published"),
        ):
            with self.assertRaises(SimulatedCrash):
                workflow.select_product(
                    self.config(), FirstChoice(), date(2026, 7, 16)
                )

        pending = next(
            path
            for path in self.done_root.iterdir()
            if path.name.startswith(".swimwear-pending-")
        )
        replacement_task = json.loads(pending.read_text(encoding="utf-8"))["task"]

        recovered_state = workflow._status_state(self.config())

        self.assertNotEqual(replacement_task["product_name"], old["product_name"])
        self.assertEqual(recovered_state["active_task"], replacement_task)
        self.assertFalse(pending.exists())

    def test_reused_target_state_failure_clears_pending_without_modifying_target(self):
        target = self.archive_path(self.product_names[0], "2026-07-15")
        original_anchored_copy = workflow._copy_tree_anchored
        original_copytree = shutil.copytree
        injected = False

        def concurrent_copy(source_fd, destination_fd, destination_name):
            nonlocal injected
            result = original_anchored_copy(
                source_fd, destination_fd, destination_name
            )
            if not injected:
                injected = True
                original_copytree(
                    self.catalog / self.product_names[0], target, symlinks=True
                )
                (target / "图片制作").mkdir()
                (target / "视频制作").mkdir()
                (target / "foreign.bin").write_bytes(b"external-owner")
            return result

        with (
            patch.object(
                workflow, "_copy_tree_anchored", side_effect=concurrent_copy
            ),
            patch.object(
                workflow,
                "save_state_atomic",
                side_effect=workflow.WorkflowError("injected state failure"),
            ),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "state failure"):
                self.select_first()

        target_before = tree_snapshot(target)
        self.assertFalse(
            any(
                path.name.startswith(".swimwear-pending-")
                for path in self.done_root.iterdir()
            )
        )
        self.assertFalse(self.state_file.exists())

        next_task = workflow.select_product(
            self.config(), FirstChoice(), date(2026, 7, 16)
        )

        self.assertEqual(tree_snapshot(target), target_before)
        self.assertEqual(next_task["product_name"], self.product_names[1])

    def test_owned_rollback_quarantines_before_deleting_and_preserves_foreign_target(self):
        original_archive = self.root / "original-before-rollback"
        foreign_before = None

        def replace_before_quarantine(creation):
            nonlocal foreign_before
            target = Path(creation.task["task_dir"])
            target.rename(original_archive)
            target.mkdir()
            (target / "图片制作").mkdir()
            (target / "视频制作").mkdir()
            (target / "foreign.bin").write_bytes(b"external-owner")
            foreign_before = tree_snapshot(target)

        with (
            patch.object(
                workflow,
                "save_state_atomic",
                side_effect=workflow.WorkflowError("injected state failure"),
            ),
            patch.object(
                workflow,
                "_before_rollback_quarantine",
                side_effect=replace_before_quarantine,
            ),
        ):
            with self.assertRaises(workflow.WorkflowError):
                self.select_first()

        target = self.archive_path(self.product_names[0], "2026-07-15")
        self.assertIsNotNone(foreign_before)
        self.assertEqual(tree_snapshot(target), foreign_before)
        self.assertTrue(original_archive.is_dir())
        self.assertFalse(self.state_file.exists())
        self.assertEqual(
            len(
                [
                    path
                    for path in self.done_root.iterdir()
                    if path.name.startswith(".swimwear-pending-")
                ]
            ),
            1,
        )

    def test_workflow_lock_does_not_wrap_body_oserror(self):
        with self.assertRaisesRegex(OSError, "body failure"):
            with workflow._workflow_lock(self.config()):
                raise OSError("body failure")

    def test_concurrent_select_calls_are_serialized(self):
        original_create = workflow._create_task
        guard = threading.Lock()
        first_entered = threading.Event()
        release_first = threading.Event()
        overlap = threading.Event()
        active_calls = 0
        call_count = 0

        def delayed_create(*args, **kwargs):
            nonlocal active_calls, call_count
            with guard:
                active_calls += 1
                call_count += 1
                current_call = call_count
                if active_calls > 1:
                    overlap.set()
                if current_call == 1:
                    first_entered.set()
            try:
                if current_call == 1:
                    release_first.wait(2)
                return original_create(*args, **kwargs)
            finally:
                with guard:
                    active_calls -= 1

        with patch.object(workflow, "_create_task", side_effect=delayed_create):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    workflow.select_product,
                    self.config(),
                    FirstChoice(),
                    date(2026, 7, 15),
                )
                self.assertTrue(first_entered.wait(1))
                second = executor.submit(
                    workflow.select_product,
                    self.config(),
                    LastChoice(),
                    date(2026, 7, 15),
                )
                overlapped_before_release = overlap.wait(0.3)
                release_first.set()
                first_result = first.result(timeout=3)
                second_result = second.result(timeout=3)

        self.assertFalse(overlapped_before_release)
        self.assertEqual(first_result, second_result)

    def test_concurrent_selects_with_distinct_states_archive_distinct_products(self):
        original_create = workflow._create_task
        guard = threading.Lock()
        first_entered = threading.Event()
        release_first = threading.Event()
        overlap = threading.Event()
        active_calls = 0
        call_count = 0

        def delayed_create(*args, **kwargs):
            nonlocal active_calls, call_count
            with guard:
                active_calls += 1
                call_count += 1
                current_call = call_count
                if active_calls > 1:
                    overlap.set()
                if current_call == 1:
                    first_entered.set()
            try:
                if current_call == 1:
                    release_first.wait(2)
                return original_create(*args, **kwargs)
            finally:
                with guard:
                    active_calls -= 1

        config_a = self.config(state_file=self.root / "state-a" / "state.json")
        config_b = self.config(state_file=self.root / "state-b" / "state.json")
        with patch.object(workflow, "_create_task", side_effect=delayed_create):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    workflow.select_product,
                    config_a,
                    FirstChoice(),
                    date(2026, 7, 15),
                )
                self.assertTrue(first_entered.wait(1))
                second = executor.submit(
                    workflow.select_product,
                    config_b,
                    FirstChoice(),
                    date(2026, 7, 15),
                )
                overlapped_before_release = overlap.wait(0.3)
                release_first.set()
                first_result = first.result(timeout=3)
                second_result = second.result(timeout=3)

        self.assertFalse(overlapped_before_release)
        self.assertEqual(
            {first_result["product_name"], second_result["product_name"]},
            set(self.product_names[:2]),
        )
        self.assertNotEqual(first_result["task_dir"], second_result["task_dir"])

    def test_concurrent_replace_calls_are_serialized(self):
        self.select_first()
        original_create = workflow._create_task
        guard = threading.Lock()
        first_entered = threading.Event()
        release_first = threading.Event()
        overlap = threading.Event()
        active_calls = 0
        call_count = 0

        def delayed_create(*args, **kwargs):
            nonlocal active_calls, call_count
            with guard:
                active_calls += 1
                call_count += 1
                current_call = call_count
                if active_calls > 1:
                    overlap.set()
                if current_call == 1:
                    first_entered.set()
            try:
                if current_call == 1:
                    release_first.wait(2)
                return original_create(*args, **kwargs)
            finally:
                with guard:
                    active_calls -= 1

        with patch.object(workflow, "_create_task", side_effect=delayed_create):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    workflow.replace_product,
                    self.config(),
                    FirstChoice(),
                    date(2026, 7, 15),
                )
                self.assertTrue(first_entered.wait(1))
                second = executor.submit(
                    workflow.replace_product,
                    self.config(),
                    LastChoice(),
                    date(2026, 7, 15),
                )
                overlapped_before_release = overlap.wait(0.3)
                release_first.set()
                first_result = first.result(timeout=3)
                second_result = second.result(timeout=3)

        self.assertFalse(overlapped_before_release)
        self.assertNotEqual(first_result["product_name"], self.product_names[0])
        self.assertNotEqual(second_result["product_name"], self.product_names[0])

    def test_load_state_rejects_symlink_swapped_between_stat_and_open(self):
        self.select_first()
        attacker_state = self.root / "attacker-state.json"
        attacker_state.write_bytes(self.state_file.read_bytes())
        original_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if (
                not swapped
                and kwargs.get("dir_fd") is not None
                and Path(path) == Path(self.state_file.name)
            ):
                swapped = True
                self.state_file.unlink()
                self.state_file.symlink_to(attacker_state)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(workflow.os, "open", side_effect=swapping_open):
            with self.assertRaises(workflow.WorkflowError):
                workflow.load_state(self.config())

    def test_load_state_rejects_boolean_version(self):
        self.write_state({"version": True, "active_task": None})

        with self.assertRaisesRegex(workflow.WorkflowError, "版本"):
            workflow.load_state(self.config())

    def test_cli_status_select_replace_emit_json_and_chinese_errors(self):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status_code = workflow.main(self.cli_roots() + ["status"])
        self.assertEqual(status_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), workflow.default_state())
        self.assertEqual(stderr.getvalue(), "")

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            select_code = workflow.main(
                self.cli_roots() + ["select", "--date", "2026-07-15", "--seed", "7"]
            )
        selected = json.loads(stdout.getvalue())
        self.assertEqual(select_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(selected, self.read_state()["active_task"])

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            replace_code = workflow.main(
                self.cli_roots() + ["replace", "--date", "2026-07-15", "--seed", "9"]
            )
        replacement = json.loads(stdout.getvalue())
        self.assertEqual(replace_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotEqual(replacement["product_name"], selected["product_name"])

        manual_video = Path(replacement["video_dir"]) / "manual-finished.mp4"
        manual_video.write_bytes(b"manual-video-bytes")
        state = self.read_state()
        state["active_task"]["stage"] = "awaiting-image-1"
        workflow.save_state_atomic(self.config(), state)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            force_replace_code = workflow.main(
                self.cli_roots()
                + [
                    "replace",
                    "--date",
                    "2026-07-16",
                    "--seed",
                    "11",
                    "--force",
                ]
            )
        forced_replacement = json.loads(stdout.getvalue())
        self.assertEqual(force_replace_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotEqual(
            forced_replacement["product_name"], replacement["product_name"]
        )
        self.assertEqual(manual_video.read_bytes(), b"manual-video-bytes")

        state = self.read_state()
        state["active_task"] = None
        workflow.save_state_atomic(self.config(), state)
        for name in self.product_names:
            if name not in workflow.completed_product_names(self.done_root):
                self.archive_existing(name)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            error_code = workflow.main(
                self.cli_roots() + ["select", "--date", "2026-07-16", "--seed", "1"]
            )
        self.assertEqual(error_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("没有未做产品", stderr.getvalue())
        self.assertRegex(stderr.getvalue(), r"[\u4e00-\u9fff]")

    def test_default_cli_select_creates_independent_task_local_states(self):
        base_args = [
            "--catalog",
            str(self.catalog),
            "--done-root",
            str(self.done_root),
        ]
        selected_tasks = []
        for seed in (1, 2):
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = workflow.main(
                    base_args
                    + ["select", "--date", "2026-07-15", "--seed", str(seed)]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            selected_tasks.append(json.loads(stdout.getvalue()))

        self.assertNotEqual(
            selected_tasks[0]["product_name"],
            selected_tasks[1]["product_name"],
        )
        for selected in selected_tasks:
            task_dir = Path(selected["task_dir"])
            state_file = task_dir / workflow.TASK_STATE_FILE_NAME
            self.assertEqual(Path(selected["state_file"]), state_file)
            stored = json.loads(state_file.read_text(encoding="utf-8"))
            expected_task = dict(selected)
            expected_task.pop("state_file")
            self.assertEqual(stored, {"version": 1, "active_task": expected_task})
        self.assertFalse(self.state_file.exists())

    def test_concurrent_independent_starts_use_distinct_task_states(self):
        config = self.config()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    workflow.start_independent_product,
                    config,
                    FirstChoice(),
                    date(2026, 7, 15),
                )
                for _ in range(2)
            ]
            tasks = [future.result(timeout=10) for future in futures]

        self.assertEqual(len({task["product_name"] for task in tasks}), 2)
        for task in tasks:
            state_file = Path(task["task_dir"]) / workflow.TASK_STATE_FILE_NAME
            stored = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["active_task"], task)
        self.assertFalse(self.state_file.exists())

    def test_task_local_replace_preserves_old_state_and_creates_new_state(self):
        old = workflow.start_independent_product(
            self.config(), FirstChoice(), date(2026, 7, 15)
        )
        old_state_file = Path(old["task_dir"]) / workflow.TASK_STATE_FILE_NAME
        old_state_before = old_state_file.read_bytes()
        old_config = self.config(state_file=old_state_file)

        replacement = workflow.replace_with_independent_product(
            old_config,
            FirstChoice(),
            date(2026, 7, 15),
        )

        new_state_file = Path(replacement["task_dir"]) / workflow.TASK_STATE_FILE_NAME
        self.assertNotEqual(old_state_file, new_state_file)
        self.assertEqual(old_state_file.read_bytes(), old_state_before)
        self.assertEqual(
            json.loads(new_state_file.read_text(encoding="utf-8"))["active_task"],
            replacement,
        )

    def test_migrate_legacy_state_into_task_directory_and_remove_source(self):
        task = self.select_first()
        legacy_state = self.state_file.read_bytes()

        result = workflow.migrate_state_to_task(self.config(), remove_source=True)

        target = Path(task["task_dir"]) / workflow.TASK_STATE_FILE_NAME
        self.assertEqual(Path(result["state_file"]), target)
        self.assertTrue(result["migrated"])
        self.assertTrue(result["source_removed"])
        self.assertFalse(self.state_file.exists())
        self.assertEqual(target.read_bytes(), legacy_state)

    def test_cli_translates_operation_oserror_without_changing_direct_api(self):
        injected = OSError("pending cleanup failed")
        with patch.object(workflow, "_status_state", side_effect=injected):
            with self.assertRaisesRegex(OSError, "pending cleanup failed"):
                workflow._status_state(self.config())

            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = workflow.main(self.cli_roots() + ["status"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertRegex(stderr.getvalue(), r"[\u4e00-\u9fff]")
        self.assertIn("pending cleanup failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class LinkSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = self.root / "链接"
        self.done_root = self.root / "已做"
        self.state_file = self.root / "state" / "state.json"
        self.history_file = self.root / ".swimwear-link-selection" / "history.json"
        self.catalog.mkdir()
        self.done_root.mkdir()
        self.entries = []

    def config(self) -> workflow.Config:
        return workflow.Config(
            self.catalog,
            self.done_root,
            self.state_file,
            ffmpeg="test-ffmpeg",
            ffprobe="test-ffprobe",
            link_history=self.history_file,
        )

    def add_product(
        self,
        product_name: str,
        *,
        day: str,
        number: int,
        status: str = "tiktok_published",
        missing: str | None = None,
    ) -> Path:
        product = self.catalog / day / f"{number:03d}-{product_name}"
        product.mkdir(parents=True)
        payloads = {
            "TK店铺标题和内容.txt": b"Title: Product\n\nContent:\nColor: Blue\n",
            "扫同款产品图.jpg": b"image-placeholder",
            "尺寸配置图.png": b"size-placeholder",
            "供应商短链接.txt": b"Link 1: https://example.com/item\n",
        }
        for filename, payload in payloads.items():
            if filename != missing:
                (product / filename).write_bytes(payload)
        self.entries.append(
            {
                "product_key": product_name,
                "canonical_key": workflow._canonical_product_key(product_name),
                "first_selected_date": day,
                "output_dir": str(product.resolve()),
                "status": status,
            }
        )
        return product

    def write_history(self) -> None:
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history_file.write_text(
            json.dumps({"version": 1, "products": self.entries}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_select_uses_only_collected_complete_products_across_dates(self):
        waiting = self.add_product(
            "WAIT-01", day="2026-07-23", number=1, status="awaiting_manual_scan"
        )
        first = self.add_product("Q964", day="2026-07-24", number=2)
        second = self.add_product("YZ2003-蓝色", day="2026-07-25", number=1)
        self.write_history()

        candidates = workflow._selection_candidates(self.config())

        self.assertEqual(candidates, [first.resolve(), second.resolve()])
        self.assertNotIn(waiting.resolve(), candidates)
        task = workflow.select_product(
            self.config(), FirstChoice(), date(2026, 7, 25)
        )
        self.assertEqual(task["product_name"], "Q964")
        self.assertEqual(Path(task["source_dir"]), first.resolve())
        self.assertEqual(Path(task["task_dir"]).name, "001-Q964")
        self.assertEqual(source_snapshot(Path(task["task_dir"])), source_snapshot(first))

    def test_completed_product_is_excluded_by_canonical_key(self):
        first = self.add_product("FT26876（HL002-玫红）", day="2026-07-24", number=5)
        second = self.add_product("TD-J521", day="2026-07-25", number=1)
        self.write_history()
        archived = self.done_root / "2026-07-24" / "001-FT26876(HL002-玫红)"
        archived.mkdir(parents=True)

        candidates = workflow._selection_candidates(self.config())

        self.assertNotIn(first.resolve(), candidates)
        self.assertEqual(candidates, [second.resolve()])

    def test_collected_product_with_missing_required_file_is_skipped(self):
        incomplete = self.add_product(
            "M043",
            day="2026-07-23",
            number=5,
            missing="供应商短链接.txt",
        )
        complete = self.add_product("HD520-01大码", day="2026-07-24", number=6)
        self.write_history()

        candidates = workflow._selection_candidates(self.config())

        self.assertNotIn(incomplete.resolve(), candidates)
        self.assertEqual(candidates, [complete.resolve()])

    def test_link_date_product_and_required_file_symlinks_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        linked_date = self.catalog / "2026-07-23"
        linked_date.symlink_to(outside, target_is_directory=True)
        product = outside / "001-Q964"
        product.mkdir()
        self.entries.append(
            {
                "product_key": "Q964",
                "canonical_key": "q964",
                "output_dir": str(linked_date / "001-Q964"),
                "status": "tiktok_published",
            }
        )
        self.write_history()
        with self.assertRaisesRegex(workflow.WorkflowError, "链接日期目录.*符号链接"):
            workflow._selection_candidates(self.config())

        linked_date.unlink()
        real_date = self.catalog / "2026-07-23"
        real_date.mkdir()
        linked_product = real_date / "001-Q964"
        linked_product.symlink_to(outside, target_is_directory=True)
        self.write_history()
        with self.assertRaisesRegex(workflow.WorkflowError, "链接商品目录.*符号链接"):
            workflow._selection_candidates(self.config())

        linked_product.unlink()
        real_product = self.add_product("M043", day="2026-07-23", number=2)
        required = real_product / "尺寸配置图.png"
        required.unlink()
        required.symlink_to(self.root / "outside-size.png")
        self.entries = [self.entries[-1]]
        self.write_history()
        with self.assertRaisesRegex(workflow.WorkflowError, "链接商品资料.*符号链接"):
            workflow._selection_candidates(self.config())

    def test_default_cli_enables_link_history_but_custom_catalog_stays_flat(self):
        parser = workflow._build_parser()
        default_args = parser.parse_args(["status"])
        default_config = workflow._base_config(default_args, self.state_file)
        self.assertEqual(default_config.link_history, workflow.DEFAULT_LINK_HISTORY)

        custom_args = parser.parse_args(["--catalog", str(self.catalog), "status"])
        custom_config = workflow._base_config(custom_args, self.state_file)
        self.assertIsNone(custom_config.link_history)

    def test_old_flat_catalog_task_can_resume_after_default_catalog_change(self):
        legacy_catalog = self.root / "legacy-catalog"
        legacy_product = legacy_catalog / "001_product"
        legacy_product.mkdir(parents=True)
        legacy_config = workflow.Config(
            legacy_catalog,
            self.done_root,
            self.state_file,
        )
        task = workflow.select_product(
            legacy_config, FirstChoice(), date(2026, 7, 25)
        )
        new_config = self.config()
        with patch.object(workflow, "LEGACY_CATALOG", legacy_catalog):
            restored = workflow.load_state(new_config)["active_task"]
        self.assertEqual(restored, task)


class ImageLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = self.root / "catalog"
        self.done_root = self.root / "done"
        self.state_file = self.root / "state" / "state.json"
        self.catalog.mkdir()
        self.done_root.mkdir()
        self.product = self.catalog / "001_product"
        self.product.mkdir()
        self.source_image = self.product / "source.jpg"
        exif = Image.Exif()
        exif[0x010E] = "metadata-must-not-survive"
        Image.new("RGB", (90, 160), (12, 34, 56)).save(
            self.source_image,
            format="JPEG",
            exif=exif,
        )
        self.config = workflow.Config(
            self.catalog,
            self.done_root,
            self.state_file,
            ffmpeg="test-ffmpeg",
            ffprobe="test-ffprobe",
        )
        self.task = workflow.select_product(
            self.config,
            FirstChoice(),
            date(2026, 7, 15),
        )
        self.task_dir = Path(self.task["task_dir"])
        self.image_dir = Path(self.task["image_dir"])

    def read_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def set_task(self, **updates) -> dict:
        state = self.read_state()
        state["active_task"].update(updates)
        workflow.save_state_atomic(self.config, state)
        return state["active_task"]

    def assert_state_unchanged(self, before: bytes) -> None:
        self.assertEqual(self.state_file.read_bytes(), before)

    def prepare_reference(self) -> Path:
        workflow.register_reference(self.config, self.source_image)
        return self.image_dir / workflow.REFERENCE_FILE

    def write_prompt(self, index: int, text: str = "生成竖屏泳装产品图") -> Path:
        prompt = self.image_dir / workflow.IMAGE_PROMPTS[index]
        prompt.write_text(text, encoding="utf-8")
        return prompt

    def write_frame(
        self,
        index: int,
        *,
        size: tuple[int, int] = (90, 160),
        color="green",
    ) -> Path:
        frame = self.image_dir / workflow.FRAME_FILES[index]
        Image.new("RGB", size, color).save(frame)
        return frame

    def prepare_awaiting_first(self) -> tuple[Path, Path]:
        reference = self.prepare_reference()
        prompt = self.write_prompt(1)
        workflow.mark_image_prompt(self.config, 1)
        return reference, prompt

    def write_download(
        self,
        name: str = "download.png",
        *,
        size: tuple[int, int] = (90, 160),
        color="blue",
        image_format: str | None = None,
    ) -> Path:
        download = self.image_dir / name
        Image.new("RGB", size, color).save(download, format=image_format)
        return download

    def cli_roots(self) -> list[str]:
        return [
            "--catalog",
            str(self.catalog),
            "--done-root",
            str(self.done_root),
            "--state-file",
            str(self.state_file),
            "--ffmpeg",
            "test-ffmpeg",
            "--ffprobe",
            "test-ffprobe",
        ]

    def test_reference_constants_and_png_without_exif(self):
        self.assertEqual(workflow.REFERENCE_FILE, "00-product-reference.png")
        self.assertEqual(
            workflow.REFERENCE_ALLOWED_STAGES,
            {"selected", "reference-ready"},
        )
        self.assertEqual(workflow.REFERENCE_NEXT_STAGE, "reference-ready")

        result = workflow.register_reference(self.config, self.source_image)

        reference = self.image_dir / workflow.REFERENCE_FILE
        self.assertEqual(result, self.read_state()["active_task"])
        self.assertEqual(result["stage"], "reference-ready")
        self.assertEqual(result["reference"], str(reference.resolve(strict=True)))
        with Image.open(reference) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(len(image.getexif()), 0)

    def test_register_reference_applies_valid_crop(self):
        source = self.task_dir / "crop-source.png"
        image = Image.new("RGB", (8, 6), "red")
        for x in range(2, 6):
            for y in range(1, 4):
                image.putpixel((x, y), (0, 255, 0))
        image.save(source)

        workflow.register_reference(self.config, source, (2, 1, 4, 3))

        with Image.open(self.image_dir / workflow.REFERENCE_FILE) as reference:
            reference.load()
            self.assertEqual(reference.size, (4, 3))
            self.assertEqual(reference.getpixel((0, 0)), (0, 255, 0))
            self.assertEqual(reference.getpixel((3, 2)), (0, 255, 0))

    def test_register_reference_rejects_invalid_crop_without_mutation(self):
        before = self.state_file.read_bytes()
        invalid_crops = (
            (0, 0, 1.5, 1),
            (True, 0, 1, 1),
            (-1, 0, 1, 1),
            (0, -1, 1, 1),
            (0, 0, 0, 1),
            (0, 0, 1, -1),
            (89, 0, 2, 1),
            (0, 159, 1, 2),
        )

        for crop in invalid_crops:
            with self.subTest(crop=crop):
                with self.assertRaises(workflow.WorkflowError):
                    workflow.register_reference(self.config, self.source_image, crop)
                self.assert_state_unchanged(before)
                self.assertFalse((self.image_dir / workflow.REFERENCE_FILE).exists())

    def test_register_reference_rejects_outside_symlink_and_broken_image(self):
        outside = self.root / "outside.png"
        Image.new("RGB", (90, 160), "blue").save(outside)
        linked = self.task_dir / "linked.png"
        linked.symlink_to(outside)
        broken = self.task_dir / "broken.png"
        broken.write_bytes(b"not-an-image")
        before = self.state_file.read_bytes()

        for source in (outside, linked, broken):
            with self.subTest(source=source.name):
                with self.assertRaises(workflow.WorkflowError):
                    workflow.register_reference(self.config, source)
                self.assert_state_unchanged(before)
                self.assertFalse((self.image_dir / workflow.REFERENCE_FILE).exists())

    def test_register_reference_rejects_wrong_stage(self):
        self.set_task(stage="awaiting-image-1")
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.register_reference(self.config, self.source_image)

        self.assert_state_unchanged(before)
        self.assertFalse((self.image_dir / workflow.REFERENCE_FILE).exists())

    def test_reference_ready_regeneration_write_failure_preserves_old_png(self):
        workflow.register_reference(self.config, self.source_image)
        reference = self.image_dir / workflow.REFERENCE_FILE
        old_png = reference.read_bytes()
        old_state = self.state_file.read_bytes()
        replacement = self.task_dir / "replacement.png"
        Image.new("RGBA", (90, 160), (200, 100, 50, 128)).save(replacement)

        with patch.object(workflow.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(workflow.WorkflowError):
                workflow.register_reference(self.config, replacement)

        self.assertEqual(reference.read_bytes(), old_png)
        self.assert_state_unchanged(old_state)
        self.assertEqual(list(self.image_dir.glob(".*.tmp")), [])

    def test_reference_publish_check_failure_restores_old_png_and_state(self):
        workflow.register_reference(self.config, self.source_image)
        reference = self.image_dir / workflow.REFERENCE_FILE
        old_png = reference.read_bytes()
        with Image.open(reference) as image:
            old_pixel = image.getpixel((0, 0))
        old_state = self.state_file.read_bytes()
        replacement = self.task_dir / "replacement.png"
        Image.new("RGB", (90, 160), "magenta").save(replacement)
        original_replace = workflow.os.replace
        original_lstat = workflow.Path.lstat
        canonical_published = False
        injected = False

        def tracking_replace(source, destination):
            nonlocal canonical_published
            result = original_replace(source, destination)
            if Path(destination) == reference:
                canonical_published = True
            return result

        def fail_first_post_publish_lstat(path):
            nonlocal injected
            if canonical_published and not injected and Path(path) == reference:
                injected = True
                raise OSError(errno.EIO, "post-publish lstat failed")
            return original_lstat(path)

        with patch.object(workflow.os, "replace", side_effect=tracking_replace), patch.object(
            workflow.Path,
            "lstat",
            autospec=True,
            side_effect=fail_first_post_publish_lstat,
        ):
            with self.assertRaises(workflow.WorkflowError):
                workflow.register_reference(self.config, replacement)

        self.assertTrue(injected)
        self.assertEqual(reference.read_bytes(), old_png)
        with Image.open(reference) as image:
            self.assertEqual(image.getpixel((0, 0)), old_pixel)
        self.assert_state_unchanged(old_state)
        self.assertEqual(list(self.image_dir.glob(".*.tmp")), [])
        self.assertEqual(list(self.image_dir.glob(".*.backup")), [])

    def test_register_reference_png_encode_failure_cleans_temp_and_preserves_state(self):
        before_state = self.state_file.read_bytes()

        with patch.object(
            workflow.Image.Image,
            "save",
            side_effect=OSError("png encode failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "PNG"):
                workflow.register_reference(self.config, self.source_image)

        self.assert_state_unchanged(before_state)
        self.assertFalse((self.image_dir / workflow.REFERENCE_FILE).exists())
        self.assertEqual(list(self.image_dir.glob(".*.tmp")), [])

    def test_register_reference_translates_pillow_decompression_bomb_error(self):
        before_state = self.state_file.read_bytes()

        with patch.object(
            workflow.Image,
            "open",
            side_effect=workflow.Image.DecompressionBombError("too large"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "解码"):
                workflow.register_reference(self.config, self.source_image)

        self.assert_state_unchanged(before_state)
        self.assertFalse((self.image_dir / workflow.REFERENCE_FILE).exists())

    def test_register_reference_translates_real_decompression_bomb_warning(self):
        before_state = self.state_file.read_bytes()

        with patch.object(workflow.Image, "MAX_IMAGE_PIXELS", 10_000):
            with self.assertRaisesRegex(workflow.WorkflowError, "解码"):
                workflow.register_reference(self.config, self.source_image)

        self.assert_state_unchanged(before_state)
        self.assertFalse((self.image_dir / workflow.REFERENCE_FILE).exists())
        self.assertEqual(list(self.image_dir.glob(".*.tmp")), [])

    def test_image_prompt_constants_and_exact_baseline_snapshot(self):
        self.assertEqual(
            workflow.IMAGE_PROMPTS,
            {
                1: "01-start-prompt.txt",
                2: "02-bridge-prompt.txt",
                3: "03-end-prompt.txt",
            },
        )
        self.assertEqual(
            workflow.FRAME_FILES,
            {1: "01-start.png", 2: "02-bridge.png", 3: "03-end.png"},
        )
        self.assertEqual(
            workflow.FORBIDDEN_PROMPT_TEXT,
            ("veo 3.1", "gpt image 2 pro"),
        )
        self.assertEqual(
            workflow.IMAGE_STAGE_FLOW,
            {
                1: ("reference-ready", "awaiting-image-1", "image-1-ready"),
                2: ("image-1-ready", "awaiting-image-2", "image-2-ready"),
                3: ("image-2-ready", "awaiting-image-3", "frames-ready"),
            },
        )
        reference = self.prepare_reference()
        prompt = self.write_prompt(1, "保留这段中文提示词，不要自动改写。")
        candidate = self.image_dir / "existing-download.JPG"
        Image.new("RGB", (90, 160), "navy").save(candidate)
        canonical = self.write_frame(1)
        candidate_stat = candidate.stat()

        result = workflow.mark_image_prompt(self.config, 1)

        self.assertEqual(prompt.read_text(encoding="utf-8"), "保留这段中文提示词，不要自动改写。")
        self.assertEqual(result["stage"], "awaiting-image-1")
        self.assertEqual(
            result["image_baseline"],
            {
                candidate.name: {
                    "device": candidate_stat.st_dev,
                    "inode": candidate_stat.st_ino,
                    "size": candidate_stat.st_size,
                    "mtime_ns": candidate_stat.st_mtime_ns,
                }
            },
        )
        self.assertNotIn(reference.name, result["image_baseline"])
        self.assertNotIn(canonical.name, result["image_baseline"])
        self.assertEqual(result, self.read_state()["active_task"])

    def test_mark_image_prompt_rejects_invalid_index_and_wrong_stage(self):
        self.prepare_reference()
        self.write_prompt(1)
        before = self.state_file.read_bytes()
        for index in (0, 4, True, "1"):
            with self.subTest(index=index):
                with self.assertRaisesRegex(workflow.WorkflowError, "索引"):
                    workflow.mark_image_prompt(self.config, index)
                self.assert_state_unchanged(before)

        self.set_task(stage="image-1-ready")
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.mark_image_prompt(self.config, 1)
        self.assert_state_unchanged(before)

    def test_image_attempts_enforce_api_then_website_retry_budget(self):
        self.prepare_awaiting_first()

        first_api = workflow.claim_image_attempt(self.config, 1, "api")
        second_api = workflow.claim_image_attempt(self.config, 1, "api")

        self.assertEqual(first_api["claimed"], {
            "index": 1,
            "surface": "api",
            "attempt": 1,
            "limit": 2,
        })
        self.assertEqual(first_api["next_on_failure"], "api")
        self.assertEqual(second_api["claimed"]["attempt"], 2)
        self.assertEqual(second_api["next_on_failure"], "website")
        with self.assertRaisesRegex(workflow.WorkflowError, "接口生图 2 次机会已用尽"):
            workflow.claim_image_attempt(self.config, 1, "api")

        website_results = [
            workflow.claim_image_attempt(self.config, 1, "website")
            for _ in range(3)
        ]
        self.assertEqual(
            [result["claimed"]["attempt"] for result in website_results],
            [1, 2, 3],
        )
        self.assertEqual(
            [result["next_on_failure"] for result in website_results],
            ["website", "website", "manual"],
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "用户手工生成"):
            workflow.claim_image_attempt(self.config, 1, "website")
        self.assertEqual(
            self.read_state()["active_task"]["image_attempts"]["1"],
            {"api": 2, "website": 3},
        )

    def test_image_attempts_reject_website_before_api_budget_is_used(self):
        self.prepare_awaiting_first()
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "必须先完成 2 次接口"):
            workflow.claim_image_attempt(self.config, 1, "website")

        self.assert_state_unchanged(before)

    def test_image_attempts_lazily_migrate_legacy_task_state(self):
        self.prepare_awaiting_first()
        state = self.read_state()
        state["active_task"].pop("image_attempts")
        workflow.save_state_atomic(self.config, state)

        result = workflow.claim_image_attempt(self.config, 1, "api")

        self.assertEqual(
            result["active_task"]["image_attempts"],
            {"1": {"api": 1, "website": 0}},
        )

    def test_image_attempts_are_not_reset_after_reject_and_remark(self):
        self.prepare_awaiting_first()
        workflow.claim_image_attempt(self.config, 1, "api")
        self.write_download()
        workflow.ingest_image(self.config, 1)
        workflow.retry_image(self.config, 1)
        workflow.mark_image_prompt(self.config, 1)

        result = workflow.claim_image_attempt(self.config, 1, "api")

        self.assertEqual(result["claimed"]["attempt"], 2)
        self.assertEqual(
            result["active_task"]["image_attempts"]["1"],
            {"api": 2, "website": 0},
        )

    def test_concurrent_image_attempt_claims_cannot_exceed_api_limit(self):
        self.prepare_awaiting_first()

        def claim_once(_):
            try:
                return workflow.claim_image_attempt(self.config, 1, "api")
            except workflow.WorkflowError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(claim_once, range(5)))

        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(successes), 2)
        self.assertEqual(len(failures), 3)
        self.assertEqual(
            self.read_state()["active_task"]["image_attempts"]["1"],
            {"api": 2, "website": 0},
        )

    def test_image_attempt_cli_emits_claim_and_chinese_errors(self):
        self.prepare_awaiting_first()
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(
                self.cli_roots() + ["claim-image-attempt", "1", "api"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["claimed"]["attempt"], 1)

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(
                self.cli_roots() + ["claim-image-attempt", "2", "website"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertRegex(stderr.getvalue(), r"[\u4e00-\u9fff]")

    def test_mark_image_prompt_rejects_missing_empty_non_utf8_symlink_and_forbidden_text(self):
        self.prepare_reference()
        prompt = self.image_dir / workflow.IMAGE_PROMPTS[1]
        external = self.root / "external-prompt.txt"
        external.write_text("合法提示词", encoding="utf-8")
        invalid_cases = (
            ("missing", None),
            ("empty", b" \n\t"),
            ("non-utf8", b"\xff\xfe"),
            ("symlink", external),
            ("veo", b"Use VeO 3.1 for this image"),
            ("gpt", b"Use GPT IMAGE 2 PRO for this image"),
        )

        for label, payload in invalid_cases:
            with self.subTest(case=label):
                if prompt.exists() or prompt.is_symlink():
                    prompt.unlink()
                if isinstance(payload, Path):
                    prompt.symlink_to(payload)
                elif payload is not None:
                    prompt.write_bytes(payload)
                before = self.state_file.read_bytes()
                with self.assertRaises(workflow.WorkflowError):
                    workflow.mark_image_prompt(self.config, 1)
                self.assert_state_unchanged(before)

    def test_mark_image_prompt_requires_canonical_valid_reference(self):
        reference = self.prepare_reference()
        self.write_prompt(1)
        alternate = self.image_dir / "alternate.png"
        Image.new("RGB", (90, 160), "purple").save(alternate)

        invalid_references = (
            None,
            str(alternate.resolve()),
            str(reference.resolve()),
        )
        for index, reference_value in enumerate(invalid_references):
            with self.subTest(reference=reference_value):
                if index == 2:
                    reference.write_bytes(b"broken-reference")
                self.set_task(reference=reference_value)
                before = self.state_file.read_bytes()
                with self.assertRaises(workflow.WorkflowError):
                    workflow.mark_image_prompt(self.config, 1)
                self.assert_state_unchanged(before)

    def test_mark_later_prompt_requires_all_previous_canonical_frames(self):
        reference = self.prepare_reference()
        prompt2 = self.write_prompt(2)
        frame1 = self.image_dir / workflow.FRAME_FILES[1]
        self.set_task(stage="image-1-ready", frames={})
        before = self.state_file.read_bytes()
        with self.assertRaises(workflow.WorkflowError):
            workflow.mark_image_prompt(self.config, 2)
        self.assert_state_unchanged(before)

        self.write_frame(1)
        alternate = self.image_dir / "alternate-frame.png"
        Image.new("RGB", (90, 160), "orange").save(alternate)
        self.set_task(frames={"1": str(alternate.resolve())})
        before = self.state_file.read_bytes()
        with self.assertRaises(workflow.WorkflowError):
            workflow.mark_image_prompt(self.config, 2)
        self.assert_state_unchanged(before)

        self.set_task(frames={"1": str(frame1.resolve())})
        frame1.write_bytes(b"broken-frame")
        before = self.state_file.read_bytes()
        with self.assertRaises(workflow.WorkflowError):
            workflow.mark_image_prompt(self.config, 2)
        self.assert_state_unchanged(before)
        self.assertTrue(reference.exists())
        self.assertTrue(prompt2.exists())

    def test_mark_image_prompt_rejects_image_candidate_symlink(self):
        self.prepare_reference()
        self.write_prompt(1)
        external = self.root / "external.png"
        Image.new("RGB", (90, 160), "black").save(external)
        candidate = self.image_dir / "download.webp"
        candidate.symlink_to(external)
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow.mark_image_prompt(self.config, 1)

        self.assert_state_unchanged(before)

    def test_ingest_rejects_invalid_index_wrong_stage_and_zero_candidate(self):
        self.prepare_reference()
        self.write_prompt(1)
        before = self.state_file.read_bytes()
        for index in (0, 4, True, "1"):
            with self.subTest(index=index):
                with self.assertRaisesRegex(workflow.WorkflowError, "索引"):
                    workflow.ingest_image(self.config, index)
                self.assert_state_unchanged(before)

        workflow.mark_image_prompt(self.config, 1)
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.ingest_image(self.config, 2)
        self.assert_state_unchanged(before)
        with self.assertRaisesRegex(workflow.WorkflowError, "恰好一个"):
            workflow.ingest_image(self.config, 1)
        self.assert_state_unchanged(before)

    def test_ingest_one_candidate_writes_clean_png_and_deletes_only_download(self):
        reference, prompt = self.prepare_awaiting_first()
        preserved = self.image_dir / "keep.txt"
        preserved.write_text("keep", encoding="utf-8")
        download = self.image_dir / "browser-download.jpeg"
        exif = Image.Exif()
        exif[0x010E] = "remove-me"
        Image.new("RGB", (90, 160), "cyan").save(
            download,
            format="JPEG",
            exif=exif,
        )

        result = workflow.ingest_image(self.config, 1)

        canonical = self.image_dir / workflow.FRAME_FILES[1]
        self.assertEqual(result["stage"], "image-1-ready")
        self.assertEqual(result["image_baseline"], {})
        self.assertEqual(result["frames"], {"1": str(canonical.resolve())})
        self.assertEqual(result, self.read_state()["active_task"])
        self.assertFalse(download.exists())
        self.assertTrue(reference.exists())
        self.assertTrue(prompt.exists())
        self.assertEqual(preserved.read_text(encoding="utf-8"), "keep")
        with Image.open(canonical) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (90, 160))
            self.assertEqual(len(image.getexif()), 0)

    def test_ingest_rejects_multiple_candidates_without_mutation(self):
        self.prepare_awaiting_first()
        first = self.write_download("first.png")
        second = self.write_download("second.webp", image_format="WEBP")
        before_state = self.state_file.read_bytes()
        before_tree = tree_snapshot(self.image_dir)

        with self.assertRaisesRegex(workflow.WorkflowError, "恰好一个"):
            workflow.ingest_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(tree_snapshot(self.image_dir), before_tree)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_ingest_accepts_same_filename_when_identity_changes_without_sleep(self):
        self.prepare_reference()
        self.write_prompt(1)
        download = self.write_download("same-name.png", color="red")
        workflow.mark_image_prompt(self.config, 1)
        baseline = self.read_state()["active_task"]["image_baseline"][download.name]
        replacement = self.image_dir / "replacement.tmp"
        Image.new("RGB", (90, 160), "lime").save(replacement, format="PNG")
        os.replace(replacement, download)
        self.assertNotEqual(download.stat().st_ino, baseline["inode"])

        result = workflow.ingest_image(self.config, 1)

        self.assertEqual(result["stage"], "image-1-ready")
        self.assertFalse(download.exists())
        with Image.open(self.image_dir / workflow.FRAME_FILES[1]) as image:
            image.load()
            self.assertEqual(image.getpixel((0, 0)), (0, 255, 0))

    def test_ingest_accepts_user_confirmed_baseline_candidate(self):
        self.prepare_reference()
        self.write_prompt(1)
        download = self.write_download("already-generated.png", color="purple")
        workflow.mark_image_prompt(self.config, 1)

        result = workflow.ingest_image(self.config, 1, download)

        canonical = self.image_dir / workflow.FRAME_FILES[1]
        self.assertEqual(result["stage"], "image-1-ready")
        self.assertEqual(result["frames"], {"1": str(canonical.resolve())})
        self.assertFalse(download.exists())
        with Image.open(canonical) as image:
            image.load()
            self.assertEqual(image.getpixel((0, 0)), (128, 0, 128))

    def test_ingest_accepts_user_confirmed_retry_archive_candidate(self):
        self.prepare_awaiting_first()
        archive = self.image_dir / workflow.RETRY_ARCHIVE_DIR
        archive.mkdir()
        rejected = archive / "01-start-rejected.png"
        Image.new("RGB", (90, 160), "purple").save(rejected)

        result = workflow.ingest_image(self.config, 1, rejected)

        canonical = self.image_dir / workflow.FRAME_FILES[1]
        self.assertEqual(result["stage"], "image-1-ready")
        self.assertFalse(rejected.exists())
        self.assertTrue(canonical.is_file())

    def test_ingest_rejects_unsafe_confirmed_source_without_mutation(self):
        self.prepare_reference()
        self.write_prompt(1)
        baseline = self.write_download("baseline.png")
        workflow.mark_image_prompt(self.config, 1)
        before = self.state_file.read_bytes()
        outside = self.root / "outside.png"
        Image.new("RGB", (90, 160), "red").save(outside)

        for source, message in (
            (Path("relative.png"), "绝对路径"),
            (outside, "图片制作目录或退回归档"),
            (self.image_dir / workflow.REFERENCE_FILE, "非规范"),
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(workflow.WorkflowError, message):
                    workflow.ingest_image(self.config, 1, source)
                self.assert_state_unchanged(before)
                self.assertTrue(baseline.exists())

    def test_ingest_rejects_symlink_broken_wrong_format_and_unstable_candidate(self):
        self.prepare_awaiting_first()
        before_state = self.state_file.read_bytes()
        external = self.root / "external.png"
        Image.new("RGB", (90, 160), "red").save(external)

        symlink = self.image_dir / "symlink.png"
        symlink.symlink_to(external)
        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow.ingest_image(self.config, 1)
        self.assert_state_unchanged(before_state)
        symlink.unlink()

        broken = self.image_dir / "broken.png"
        broken.write_bytes(b"not-an-image")
        with self.assertRaises(workflow.WorkflowError):
            workflow.ingest_image(self.config, 1)
        self.assert_state_unchanged(before_state)
        broken.unlink()

        wrong_format = self.write_download("disguised.png", image_format="GIF")
        with self.assertRaisesRegex(workflow.WorkflowError, "格式"):
            workflow.ingest_image(self.config, 1)
        self.assert_state_unchanged(before_state)
        wrong_format.unlink()

        unstable = self.write_download("unstable.png")

        def mutate_candidate(path):
            Image.new("RGB", (100, 160), "yellow").save(path, format="PNG")

        with patch.object(
            workflow,
            "_after_image_candidate_open",
            side_effect=mutate_candidate,
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "变化"):
                workflow.ingest_image(self.config, 1)
        self.assert_state_unchanged(before_state)
        self.assertTrue(unstable.exists())
        self.assertFalse((self.image_dir / workflow.FRAME_FILES[1]).exists())

    def test_ingest_translates_real_decompression_bomb_warning_without_consuming(self):
        self.prepare_awaiting_first()
        download = self.write_download("warning.png")
        canonical = self.image_dir / workflow.FRAME_FILES[1]
        before_state = self.state_file.read_bytes()

        with patch.object(workflow.Image, "MAX_IMAGE_PIXELS", 10_000):
            with self.assertRaisesRegex(workflow.WorkflowError, "解码"):
                workflow.ingest_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertTrue(download.exists())
        self.assertFalse(canonical.exists())
        self.assertEqual(list(self.image_dir.glob(".*.tmp")), [])

    def test_ingest_accepts_exact_ratio_tolerance_boundary(self):
        self.prepare_awaiting_first()
        download = self.write_download("boundary.png", size=(233, 400))

        result = workflow.ingest_image(self.config, 1)

        self.assertEqual(result["stage"], "image-1-ready")
        self.assertFalse(download.exists())
        with Image.open(self.image_dir / workflow.FRAME_FILES[1]) as image:
            self.assertEqual(image.size, (233, 400))

    def test_ingest_rejects_ratio_beyond_tolerance(self):
        self.prepare_awaiting_first()
        download = self.write_download("too-wide.png", size=(234, 400))
        before_state = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "9:16"):
            workflow.ingest_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertTrue(download.exists())
        self.assertFalse((self.image_dir / workflow.FRAME_FILES[1]).exists())

    def test_ingest_refuses_to_overwrite_existing_canonical(self):
        self.prepare_awaiting_first()
        canonical = self.write_frame(1, color="red")
        old_canonical = canonical.read_bytes()
        download = self.write_download("new-download.png", color="green")
        before_state = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "覆盖"):
            workflow.ingest_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(canonical.read_bytes(), old_canonical)
        self.assertTrue(download.exists())

    def test_ingest_state_save_failure_rolls_back_canonical_and_keeps_download(self):
        self.prepare_awaiting_first()
        download = self.write_download("download.png")
        before_state = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=workflow.WorkflowError("state save failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "state save failed"):
                workflow.ingest_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertTrue(download.exists())
        self.assertFalse((self.image_dir / workflow.FRAME_FILES[1]).exists())
        self.assertEqual(list(self.image_dir.glob(".*.tmp")), [])

    def test_ingest_image_keeps_published_media_when_state_committed_then_save_raises(self):
        self.prepare_awaiting_first()
        download = self.write_download("committed-download.png", color="cyan")
        canonical = self.image_dir / workflow.FRAME_FILES[1]
        real_save = workflow.save_state_atomic
        committed_state = None

        def commit_then_raise(config, state_value):
            nonlocal committed_state
            real_save(config, state_value)
            committed_state = copy.deepcopy(state_value)
            raise workflow.WorkflowError("raised after committed state")

        with patch.object(workflow, "save_state_atomic", side_effect=commit_then_raise):
            with self.assertRaisesRegex(workflow.WorkflowError, "after committed"):
                workflow.ingest_image(self.config, 1)

        self.assertEqual(self.read_state(), committed_state)
        self.assertEqual(self.read_state()["active_task"]["stage"], "image-1-ready")
        self.assertTrue(canonical.is_file())
        self.assertTrue(download.is_file())
        with Image.open(canonical) as image:
            image.load()
            self.assertEqual(image.getpixel((0, 0)), (0, 255, 255))

    def test_ingest_publish_check_failure_rolls_back_and_remains_retryable(self):
        self.prepare_awaiting_first()
        download = self.write_download("download.png", color="cyan")
        canonical = self.image_dir / workflow.FRAME_FILES[1]
        before_state = self.state_file.read_bytes()
        original_rename_noreplace = workflow._rename_noreplace
        original_lstat = workflow.Path.lstat
        canonical_published = False
        injected = False

        def tracking_rename(source_fd, source_name, target_fd, target_name):
            nonlocal canonical_published
            result = original_rename_noreplace(
                source_fd,
                source_name,
                target_fd,
                target_name,
            )
            if result and target_name == canonical.name:
                canonical_published = True
            return result

        def fail_first_post_publish_lstat(path):
            nonlocal injected
            if canonical_published and not injected and Path(path) == canonical:
                injected = True
                raise OSError(errno.EIO, "post-publish lstat failed")
            return original_lstat(path)

        with patch.object(
            workflow,
            "_rename_noreplace",
            side_effect=tracking_rename,
        ), patch.object(
            workflow.Path,
            "lstat",
            autospec=True,
            side_effect=fail_first_post_publish_lstat,
        ):
            with self.assertRaises(workflow.WorkflowError):
                workflow.ingest_image(self.config, 1)

        self.assertTrue(injected)
        self.assertFalse(canonical.exists())
        self.assertTrue(download.exists())
        self.assert_state_unchanged(before_state)
        self.assertEqual(self.read_state()["active_task"]["stage"], "awaiting-image-1")
        self.assertEqual(list(self.image_dir.glob(".*.tmp")), [])
        self.assertEqual(list(self.image_dir.glob(".*.backup")), [])

        result = workflow.ingest_image(self.config, 1)
        self.assertEqual(result["stage"], "image-1-ready")
        self.assertTrue(canonical.exists())
        self.assertFalse(download.exists())

    def test_ingest_never_deletes_candidate_replaced_before_final_unlink(self):
        self.prepare_awaiting_first()
        download = self.write_download("download.png", color="blue")
        consumed_copy = self.root / "consumed-original.png"
        foreign_bytes = None

        def replace_before_delete(path):
            nonlocal foreign_bytes
            os.replace(path, consumed_copy)
            Image.new("RGB", (90, 160), "magenta").save(path, format="PNG")
            foreign_bytes = path.read_bytes()

        with patch.object(
            workflow,
            "_before_image_candidate_delete",
            side_effect=replace_before_delete,
            create=True,
        ):
            result = workflow.ingest_image(self.config, 1)

        self.assertEqual(result["stage"], "image-1-ready")
        self.assertTrue(consumed_copy.exists())
        self.assertTrue(download.exists())
        self.assertEqual(download.read_bytes(), foreign_bytes)

    def test_three_images_follow_exact_serial_stages_and_preserve_inputs(self):
        reference = self.prepare_reference()
        prompts = []
        expected_stages = ("image-1-ready", "image-2-ready", "frames-ready")

        for index, expected_stage in enumerate(expected_stages, start=1):
            prompt = self.write_prompt(index, f"第 {index} 张图提示词")
            prompts.append(prompt)
            marked = workflow.mark_image_prompt(self.config, index)
            self.assertEqual(marked["stage"], f"awaiting-image-{index}")
            download = self.write_download(f"download-{index}.png", color=(index, 2, 3))

            result = workflow.ingest_image(self.config, index)

            self.assertEqual(result["stage"], expected_stage)
            self.assertEqual(set(result["frames"]), {str(i) for i in range(1, index + 1)})
            self.assertFalse(download.exists())
            self.assertTrue(reference.exists())
            for retained_prompt in prompts:
                self.assertTrue(retained_prompt.exists())
            for retained_index in range(1, index + 1):
                self.assertTrue((self.image_dir / workflow.FRAME_FILES[retained_index]).exists())

    def prepare_retry_stage(self, index: int) -> tuple[Path, dict[str, Path]]:
        reference = self.prepare_reference()
        frames = {
            str(frame_index): self.write_frame(
                frame_index,
                color=(frame_index, frame_index + 10, frame_index + 20),
            )
            for frame_index in range(1, index + 1)
        }
        stages = {1: "image-1-ready", 2: "image-2-ready", 3: "frames-ready"}
        self.set_task(
            stage=stages[index],
            frames={key: str(path.resolve()) for key, path in frames.items()},
            image_baseline={},
        )
        return reference, frames

    def test_retry_image_exact_stage_transitions_archive_only_current_frame(self):
        expected_predecessors = {
            1: "reference-ready",
            2: "image-1-ready",
            3: "image-2-ready",
        }

        for index, predecessor in expected_predecessors.items():
            with self.subTest(index=index):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                catalog = root / "catalog"
                done_root = root / "done"
                state_file = root / "state" / "state.json"
                product = catalog / "001_product"
                product.mkdir(parents=True)
                done_root.mkdir()
                source = product / "source.png"
                Image.new("RGB", (90, 160), "black").save(source)
                config = workflow.Config(catalog, done_root, state_file)
                task = workflow.select_product(
                    config, FirstChoice(), date(2026, 7, 15)
                )
                image_dir = Path(task["image_dir"])
                workflow.register_reference(config, source)
                frame_paths = {}
                frame_bytes = {}
                for frame_index in range(1, index + 1):
                    frame = image_dir / workflow.FRAME_FILES[frame_index]
                    Image.new(
                        "RGB",
                        (90, 160),
                        (frame_index, frame_index + 10, frame_index + 20),
                    ).save(frame)
                    frame_paths[str(frame_index)] = str(frame.resolve())
                    frame_bytes[frame_index] = frame.read_bytes()
                state = json.loads(state_file.read_text(encoding="utf-8"))
                state["active_task"].update(
                    {
                        "stage": {
                            1: "image-1-ready",
                            2: "image-2-ready",
                            3: "frames-ready",
                        }[index],
                        "frames": frame_paths,
                    }
                )
                workflow.save_state_atomic(config, state)
                reference = image_dir / workflow.REFERENCE_FILE
                reference_bytes = reference.read_bytes()

                result = workflow.retry_image(config, index)

                self.assertEqual(result["stage"], predecessor)
                self.assertEqual(
                    result["frames"],
                    {
                        str(previous): frame_paths[str(previous)]
                        for previous in range(1, index)
                    },
                )
                self.assertEqual(result["image_baseline"], {})
                self.assertEqual(
                    json.loads(state_file.read_text(encoding="utf-8"))["active_task"],
                    result,
                )
                self.assertEqual(reference.read_bytes(), reference_bytes)
                for previous in range(1, index):
                    self.assertEqual(
                        (image_dir / workflow.FRAME_FILES[previous]).read_bytes(),
                        frame_bytes[previous],
                    )
                self.assertFalse(
                    (image_dir / workflow.FRAME_FILES[index]).exists()
                )
                archive = image_dir / workflow.RETRY_ARCHIVE_DIR
                archived = list(archive.iterdir())
                self.assertEqual(len(archived), 1)
                self.assertTrue(archived[0].is_file())
                self.assertEqual(archived[0].read_bytes(), frame_bytes[index])

    def test_retry_image_recovers_state_when_current_canonical_frame_is_missing(self):
        reference = self.prepare_reference()
        canonical = self.image_dir / workflow.FRAME_FILES[1]
        self.set_task(
            stage="image-1-ready",
            frames={"1": str(canonical.resolve())},
            image_baseline={},
        )
        reference_bytes = reference.read_bytes()

        result = workflow.retry_image(self.config, 1)

        self.assertEqual(result["stage"], "reference-ready")
        self.assertEqual(result["frames"], {})
        self.assertEqual(result["image_baseline"], {})
        self.assertEqual(result, self.read_state()["active_task"])
        self.assertEqual(reference.read_bytes(), reference_bytes)
        self.assertFalse((self.image_dir / workflow.RETRY_ARCHIVE_DIR).exists())

    def test_retry_image_rejects_archive_collision_without_overwriting(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        canonical_bytes = canonical.read_bytes()
        archive = self.image_dir / workflow.RETRY_ARCHIVE_DIR
        archive.mkdir()
        collision = archive / "fixed-rejected.png"
        collision.write_bytes(b"foreign-archive")
        before_state = self.state_file.read_bytes()
        before_tree = tree_snapshot(self.image_dir)

        with patch.object(
            workflow,
            "_retry_image_archive_name",
            return_value=collision.name,
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "冲突|覆盖"):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(tree_snapshot(self.image_dir), before_tree)
        self.assertEqual(canonical.read_bytes(), canonical_bytes)
        self.assertEqual(collision.read_bytes(), b"foreign-archive")

    def test_retry_image_state_save_failure_restores_canonical_frame(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        canonical_bytes = canonical.read_bytes()
        before_state = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=workflow.WorkflowError("state save failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "state save failed"):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(canonical.read_bytes(), canonical_bytes)
        archive = self.image_dir / workflow.RETRY_ARCHIVE_DIR
        self.assertEqual(list(archive.iterdir()), [])

    def test_retry_image_keeps_archive_when_state_committed_then_save_raises(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        canonical_bytes = canonical.read_bytes()
        real_save = workflow.save_state_atomic
        committed_state = None

        def commit_then_raise(config, state_value):
            nonlocal committed_state
            real_save(config, state_value)
            committed_state = copy.deepcopy(state_value)
            raise workflow.WorkflowError("raised after committed state")

        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=commit_then_raise,
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "after committed"):
                workflow.retry_image(self.config, 1)

        self.assertEqual(self.read_state(), committed_state)
        self.assertEqual(
            self.read_state()["active_task"]["stage"], "reference-ready"
        )
        self.assertFalse(canonical.exists())
        archived = list(
            (self.image_dir / workflow.RETRY_ARCHIVE_DIR).iterdir()
        )
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), canonical_bytes)

    def test_retry_image_never_overwrites_foreign_file_that_occupies_rollback_path(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        canonical_bytes = canonical.read_bytes()
        foreign_bytes = b"foreign-canonical-owner"
        before_state = self.state_file.read_bytes()

        def occupy_then_fail(config, state_value):
            canonical.write_bytes(foreign_bytes)
            raise workflow.WorkflowError("state save failed")

        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=occupy_then_fail,
        ):
            with self.assertRaisesRegex(
                workflow.WorkflowError,
                "回滚失败|占用|覆盖",
            ):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(canonical.read_bytes(), foreign_bytes)
        archived = list(
            (self.image_dir / workflow.RETRY_ARCHIVE_DIR).iterdir()
        )
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), canonical_bytes)

    def test_retry_image_hook_failure_rolls_back_before_state_commit(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        canonical_bytes = canonical.read_bytes()
        before_state = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "_after_retry_image_moved",
            side_effect=workflow.WorkflowError("post-move verification failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "post-move"):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(canonical.read_bytes(), canonical_bytes)
        archive = self.image_dir / workflow.RETRY_ARCHIVE_DIR
        self.assertEqual(list(archive.iterdir()), [])

    def test_retry_image_oserror_after_move_rolls_back_and_translates_error(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        canonical_bytes = canonical.read_bytes()
        before_state = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "_after_retry_image_moved",
            side_effect=OSError(errno.EIO, "post-move I/O failure"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "无法退回规范图片"):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(canonical.read_bytes(), canonical_bytes)
        archive = self.image_dir / workflow.RETRY_ARCHIVE_DIR
        self.assertEqual(list(archive.iterdir()), [])

    def test_retry_image_baseexception_after_move_rolls_back_and_reraises(self):
        class SimulatedInterrupt(BaseException):
            pass

        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        canonical_bytes = canonical.read_bytes()
        before_state = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "_after_retry_image_moved",
            side_effect=SimulatedInterrupt("post-move interrupt"),
        ):
            with self.assertRaisesRegex(SimulatedInterrupt, "post-move interrupt"):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(canonical.read_bytes(), canonical_bytes)
        archive = self.image_dir / workflow.RETRY_ARCHIVE_DIR
        self.assertEqual(list(archive.iterdir()), [])

    def test_retry_image_refuses_to_restore_archive_modified_in_place(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        original_bytes = canonical.read_bytes()
        modified_bytes = b"foreign-modification-after-archive"
        before_state = self.state_file.read_bytes()

        def modify_archive_then_fail(source, archive):
            archive.write_bytes(modified_bytes)
            raise workflow.WorkflowError("post-move verification failed")

        with patch.object(
            workflow,
            "_after_retry_image_moved",
            side_effect=modify_archive_then_fail,
        ):
            with self.assertRaisesRegex(
                workflow.WorkflowError,
                "回滚失败|替换|修改|身份",
            ):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertFalse(canonical.exists())
        archived = list(
            (self.image_dir / workflow.RETRY_ARCHIVE_DIR).iterdir()
        )
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), modified_bytes)
        self.assertNotEqual(archived[0].read_bytes(), original_bytes)

    def test_retry_image_rejects_nonempty_image_baseline_without_mutation(self):
        _, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        baseline_candidate = self.image_dir / "stale-download.png"
        Image.new("RGB", (90, 160), "white").save(baseline_candidate)
        snapshot = baseline_candidate.stat()
        self.set_task(
            image_baseline={
                baseline_candidate.name: {
                    "device": snapshot.st_dev,
                    "inode": snapshot.st_ino,
                    "size": snapshot.st_size,
                    "mtime_ns": snapshot.st_mtime_ns,
                }
            }
        )
        before_state = self.state_file.read_bytes()
        before_tree = tree_snapshot(self.image_dir)

        with self.assertRaisesRegex(workflow.WorkflowError, "基线"):
            workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(tree_snapshot(self.image_dir), before_tree)
        self.assertTrue(canonical.is_file())

    def test_retry_image_rejects_real_image_directory_replacement(self):
        reference, frames = self.prepare_retry_stage(1)
        canonical = frames["1"]
        reference_bytes = reference.read_bytes()
        canonical_bytes = canonical.read_bytes()
        before_state = self.state_file.read_bytes()
        original_image_dir = self.root / "original-image-dir"
        foreign_reference_bytes = b"foreign-reference"
        foreign_frame_bytes = b"foreign-frame"
        real_validate = workflow._validate_canonical_image
        swapped = False

        def validate_then_swap(task, state_key, index):
            nonlocal swapped
            real_validate(task, state_key, index)
            if not swapped:
                swapped = True
                self.image_dir.rename(original_image_dir)
                self.image_dir.mkdir()
                (self.image_dir / workflow.REFERENCE_FILE).write_bytes(
                    foreign_reference_bytes
                )
                (self.image_dir / workflow.FRAME_FILES[1]).write_bytes(
                    foreign_frame_bytes
                )

        with patch.object(
            workflow,
            "_validate_canonical_image",
            side_effect=validate_then_swap,
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "图片目录.*替换"):
                workflow.retry_image(self.config, 1)

        self.assert_state_unchanged(before_state)
        self.assertEqual(
            (original_image_dir / workflow.REFERENCE_FILE).read_bytes(),
            reference_bytes,
        )
        self.assertEqual(
            (original_image_dir / workflow.FRAME_FILES[1]).read_bytes(),
            canonical_bytes,
        )
        self.assertEqual(
            (self.image_dir / workflow.REFERENCE_FILE).read_bytes(),
            foreign_reference_bytes,
        )
        self.assertEqual(
            (self.image_dir / workflow.FRAME_FILES[1]).read_bytes(),
            foreign_frame_bytes,
        )
        self.assertFalse((self.image_dir / workflow.RETRY_ARCHIVE_DIR).exists())

    def test_retry_image_rejects_invalid_index_wrong_stage_frames_and_symlink(self):
        self.prepare_reference()
        before = self.state_file.read_bytes()
        for index in (0, 4, True, "1"):
            with self.subTest(index=index):
                with self.assertRaisesRegex(workflow.WorkflowError, "索引"):
                    workflow.retry_image(self.config, index)
                self.assert_state_unchanged(before)

        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.retry_image(self.config, 1)
        self.assert_state_unchanged(before)

        frame1 = self.write_frame(1)
        frame2 = self.write_frame(2)
        self.set_task(
            stage="image-2-ready",
            frames={"2": str(frame2.resolve())},
        )
        before = self.state_file.read_bytes()
        before_tree = tree_snapshot(self.image_dir)
        with self.assertRaisesRegex(workflow.WorkflowError, "帧状态"):
            workflow.retry_image(self.config, 2)
        self.assert_state_unchanged(before)
        self.assertEqual(tree_snapshot(self.image_dir), before_tree)

        external = self.root / "external-frame.png"
        Image.new("RGB", (90, 160), "orange").save(external)
        canonical_frame1 = str(frame1.resolve())
        self.set_task(
            stage="image-1-ready",
            frames={"1": canonical_frame1},
        )
        frame1.unlink()
        frame1.symlink_to(external)
        before = self.state_file.read_bytes()
        external_bytes = external.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接|越界"):
            workflow.retry_image(self.config, 1)
        self.assert_state_unchanged(before)
        self.assertTrue(frame1.is_symlink())
        self.assertEqual(external.read_bytes(), external_bytes)

    def test_retry_image_cli_emits_json_and_chinese_errors(self):
        self.prepare_retry_stage(1)
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["retry-image", "1"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["stage"], "reference-ready")
        self.assertEqual(result["frames"], {})

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["retry-image", "4"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("错误：", stderr.getvalue())
        self.assertRegex(stderr.getvalue(), r"[\u4e00-\u9fff]")

    def test_image_lifecycle_cli_commands_emit_json_and_chinese_errors(self):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(
                self.cli_roots()
                + [
                    "register-reference",
                    str(self.source_image),
                    "--crop",
                    "0",
                    "0",
                    "90",
                    "160",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "reference-ready")

        self.write_prompt(1)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["mark-image-prompt", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "awaiting-image-1")

        self.write_download()
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["ingest-image", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "image-1-ready")

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["ingest-image", "4"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertRegex(stderr.getvalue(), r"[\u4e00-\u9fff]")


class VideoLifecycleTests(unittest.TestCase):
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    FFPROBE = shutil.which("ffprobe") or "ffprobe"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = self.root / "catalog"
        self.done_root = self.root / "done"
        self.state_file = self.root / "state" / "state.json"
        self.catalog.mkdir()
        self.done_root.mkdir()
        self.product = self.catalog / "001_product's space"
        self.product.mkdir()
        self.source_image = self.product / "source.png"
        Image.new("RGB", (90, 160), "navy").save(self.source_image)
        self.config = workflow.Config(
            self.catalog,
            self.done_root,
            self.state_file,
            ffmpeg=self.FFMPEG,
            ffprobe=self.FFPROBE,
        )
        self.task = workflow.select_product(
            self.config,
            FirstChoice(),
            date(2026, 7, 15),
        )
        self.image_dir = Path(self.task["image_dir"])
        self.video_dir = Path(self.task["video_dir"])

    def read_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def set_task(self, **updates) -> dict:
        state = self.read_state()
        state["active_task"].update(updates)
        workflow.save_state_atomic(self.config, state)
        return state["active_task"]

    def prepare_frames(self) -> tuple[Path, list[Path]]:
        reference = self.image_dir / workflow.REFERENCE_FILE
        Image.new("RGB", (90, 160), "black").save(reference)
        frames = []
        for index, color in enumerate(("red", "green", "blue"), start=1):
            frame = self.image_dir / workflow.FRAME_FILES[index]
            Image.new("RGB", (90, 160), color).save(frame)
            frames.append(frame)
        self.set_task(
            stage="frames-ready",
            reference=str(reference.resolve()),
            frames={str(index): str(path.resolve()) for index, path in enumerate(frames, 1)},
        )
        return reference, frames

    def write_video_prompts(self, text: str = "生成竖屏泳装产品视频") -> list[Path]:
        required = (
            "英语口播：自然口语；人物嘴型同步；开头形成视觉爆点；"
            "有效口播至少覆盖三分之二片段。"
        )
        prompts = []
        for name in workflow.VIDEO_PROMPTS:
            prompt = self.video_dir / name
            prompt.write_text(f"{text}\n{required}", encoding="utf-8")
            prompts.append(prompt)
        return prompts

    def prepare_awaiting_videos(self) -> None:
        self.prepare_frames()
        self.write_video_prompts()
        workflow.mark_video_prompts(self.config)

    def prepare_segments(
        self,
        *,
        first_size: tuple[int, int] = (32, 48),
        second_size: tuple[int, int] = (32, 48),
        first_audio: bool = True,
        second_audio: bool = True,
    ) -> tuple[Path, Path]:
        self.prepare_awaiting_videos()
        first = self.make_video(
            "first download.MP4",
            color="red",
            size=first_size,
            audio=first_audio,
        )
        second = self.make_video(
            "second download.MOV",
            color="blue",
            size=second_size,
            audio=second_audio,
        )
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        result = workflow.ingest_videos(self.config)
        return Path(result["videos"][0]), Path(result["videos"][1])

    def make_video(
        self,
        name: str,
        *,
        color: str = "red",
        size: tuple[int, int] = (32, 48),
        duration: float = 0.4,
        audio: bool = True,
        audio_duration: float | None = None,
        video_codec: str | None = None,
        audio_codec: str | None = None,
    ) -> Path:
        output = self.video_dir / name
        command = [
            self.FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size[0]}x{size[1]}:r=10:d={duration}",
        ]
        if audio:
            actual_audio_duration = duration if audio_duration is None else audio_duration
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=440:sample_rate=44100:duration={actual_audio_duration}",
                ]
            )
            if audio_duration is None:
                command.append("-shortest")
        if video_codec is not None:
            command.extend(["-c:v", video_codec, "-pix_fmt", "yuv420p"])
            if audio and audio_codec is not None:
                command.extend(["-c:a", audio_codec])
        elif output.suffix.casefold() == ".webm":
            command.extend(["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p"])
            if audio:
                command.extend(["-c:a", audio_codec or "libopus"])
        else:
            command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
            if audio:
                command.extend(["-c:a", "aac"])
        command.append(str(output))
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            self.fail(f"test video generation failed: {completed.stderr}")
        return output

    def audio_stream_duration(self, path: Path) -> float:
        completed = subprocess.run(
            [
                self.FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return float(json.loads(completed.stdout)["streams"][0]["duration"])

    def video_stream_duration(self, path: Path) -> float:
        completed = subprocess.run(
            [
                self.FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return float(json.loads(completed.stdout)["streams"][0]["duration"])

    @staticmethod
    def set_mtime(path: Path, value: int) -> None:
        os.utime(path, ns=(value, value))

    def assert_state_unchanged(self, before: bytes) -> None:
        self.assertEqual(self.state_file.read_bytes(), before)

    def cli_roots(self) -> list[str]:
        return [
            "--catalog",
            str(self.catalog),
            "--done-root",
            str(self.done_root),
            "--state-file",
            str(self.state_file),
            "--ffmpeg",
            self.FFMPEG,
            "--ffprobe",
            self.FFPROBE,
        ]

    def test_video_constants_and_exact_baseline_snapshot(self):
        self.assertEqual(
            workflow.VIDEO_PROMPTS,
            ("01-video-prompt.txt", "02-video-prompt.txt"),
        )
        self.assertEqual(workflow.VIDEO_SUFFIXES, {".mp4", ".mov", ".m4v", ".webm"})
        self.assertEqual(workflow.VIDEO_PROMPT_STAGE, ("frames-ready", "awaiting-videos"))
        self.assertEqual(workflow.VIDEO_INGEST_STAGE, ("awaiting-videos", "videos-ready"))
        self.assertEqual(workflow.COPY_CONCAT_ARGS, ("-f", "concat", "-safe", "0", "-c", "copy"))
        self.assertEqual(workflow.NORMALIZE_VIDEO_ARGS, ("-c:v", "libx264", "-pix_fmt", "yuv420p"))
        self.assertEqual(workflow.NORMALIZE_AUDIO_ARGS, ("-c:a", "aac"))
        reference, frames = self.prepare_frames()
        prompts = self.write_video_prompts("保留两段独立提示词原文")
        candidate = self.video_dir / "existing-download.MOV"
        candidate.write_bytes(b"baseline-video")
        candidate_info = candidate.stat()
        for excluded in (
            "01-segment.mp4",
            "01-segment.backup.mp4",
            "02-segment.mov",
            "final.mp4",
            ".hidden.mp4",
            ".partial-video.webm",
        ):
            (self.video_dir / excluded).write_bytes(b"excluded")

        result = workflow.mark_video_prompts(self.config)

        self.assertEqual(result["stage"], "awaiting-videos")
        self.assertEqual(
            result["video_baseline"],
            {
                candidate.name: {
                    "device": candidate_info.st_dev,
                    "inode": candidate_info.st_ino,
                    "size": candidate_info.st_size,
                    "mtime_ns": candidate_info.st_mtime_ns,
                }
            },
        )
        self.assertEqual(result, self.read_state()["active_task"])
        self.assertTrue(reference.exists())
        self.assertTrue(all(path.exists() for path in frames))
        self.assertTrue(all(path.exists() for path in prompts))

    def test_video_attempts_are_persisted_per_segment_and_enforce_budget(self):
        self.prepare_awaiting_videos()
        state = self.read_state()
        state["active_task"].pop("video_attempts")
        workflow.save_state_atomic(self.config, state)

        first = workflow.claim_video_attempt(self.config, 1)
        self.assertEqual(first["claimed"]["attempt"], 1)
        self.assertEqual(first["claimed"]["limit"], 3)
        self.assertEqual(first["claimed"]["surface"], "toapi-video-web")
        self.assertEqual(first["next_on_failure"], "website")
        second = workflow.claim_video_attempt(self.config, 1)
        self.assertEqual(second["claimed"]["attempt"], 2)
        third = workflow.claim_video_attempt(self.config, 1)
        self.assertEqual(third["claimed"]["attempt"], 3)
        self.assertEqual(third["next_on_failure"], "manual")
        with self.assertRaisesRegex(workflow.WorkflowError, "用尽"):
            workflow.claim_video_attempt(self.config, 1)

        other = workflow.claim_video_attempt(self.config, 2)
        self.assertEqual(other["claimed"]["attempt"], 1)
        self.assertEqual(
            self.read_state()["active_task"]["video_attempts"],
            {"1": 3, "2": 1},
        )

    def test_claim_video_attempt_requires_awaiting_stage_and_valid_index(self):
        self.prepare_frames()
        self.write_video_prompts()
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.claim_video_attempt(self.config, 1)
        with self.assertRaisesRegex(workflow.WorkflowError, "索引"):
            workflow.claim_video_attempt(self.config, 3)
        self.assert_state_unchanged(before)

    def test_mark_video_prompts_requires_frames_ready_and_valid_canonical_images(self):
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.mark_video_prompts(self.config)
        self.assert_state_unchanged(before)

        reference, frames = self.prepare_frames()
        self.write_video_prompts()
        valid_state = self.state_file.read_bytes()
        reference.unlink()
        with self.assertRaises(workflow.WorkflowError):
            workflow.mark_video_prompts(self.config)
        self.assert_state_unchanged(valid_state)
        Image.new("RGB", (90, 160), "black").save(reference)

        frames[1].write_bytes(b"not-an-image")
        with self.assertRaises(workflow.WorkflowError):
            workflow.mark_video_prompts(self.config)
        self.assert_state_unchanged(valid_state)
        Image.new("RGB", (90, 160), "green").save(frames[1])

        self.set_task(frames={**self.read_state()["active_task"]["frames"], "2": str(frames[0])})
        wrong_path_state = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "规范路径"):
            workflow.mark_video_prompts(self.config)
        self.assert_state_unchanged(wrong_path_state)

    def test_mark_video_prompts_rejects_missing_empty_non_utf8_symlink_and_forbidden_text(self):
        self.prepare_frames()
        prompts = self.write_video_prompts()

        cases = (
            ("missing", None),
            ("empty", b" \n"),
            ("non-utf8", b"\xff\xfe"),
            ("forbidden-veo", b"Use VeO 3.1 here"),
            ("forbidden-gpt", b"use GPT IMAGE 2 PRO here"),
        )
        for label, content in cases:
            with self.subTest(case=label):
                prompts[0].unlink(missing_ok=True)
                if content is not None:
                    prompts[0].write_bytes(content)
                before = self.state_file.read_bytes()
                with self.assertRaises(workflow.WorkflowError):
                    workflow.mark_video_prompts(self.config)
                self.assert_state_unchanged(before)
                prompts[0].write_text(
                    "恢复有效提示词。英语口播：自然口语；人物嘴型同步；视觉爆点；"
                    "有效口播至少覆盖三分之二片段。",
                    encoding="utf-8",
                )

        outside = self.root / "outside-prompt.txt"
        outside.write_text("外部提示词", encoding="utf-8")
        prompts[1].unlink()
        prompts[1].symlink_to(outside)
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow.mark_video_prompts(self.config)
        self.assert_state_unchanged(before)

    def test_mark_video_prompts_rejects_two_paths_hard_linked_to_same_file(self):
        self.prepare_frames()
        prompts = self.write_video_prompts("相同文本内容本身允许")
        prompts[1].unlink()
        os.link(prompts[0], prompts[1])
        self.assertEqual(
            (prompts[0].stat().st_dev, prompts[0].stat().st_ino),
            (prompts[1].stat().st_dev, prompts[1].stat().st_ino),
        )
        before_state = self.state_file.read_bytes()
        before_files = tree_snapshot(self.video_dir)

        with self.assertRaisesRegex(workflow.WorkflowError, "独立"):
            workflow.mark_video_prompts(self.config)

        self.assert_state_unchanged(before_state)
        self.assertEqual(tree_snapshot(self.video_dir), before_files)

    def test_mark_video_prompts_requires_speech_sync_visual_hook_and_speech_coverage(self):
        self.prepare_frames()
        prompts = self.write_video_prompts()
        valid = prompts[0].read_text(encoding="utf-8")
        cases = (
            ("英语口播", "口播"),
            ("嘴型同步", "嘴型"),
            ("视觉爆点", "爆点"),
            ("有效口播至少覆盖三分之二", "三分之二"),
        )
        for marker, expected_error in cases:
            with self.subTest(marker=marker):
                prompts[0].write_text(valid.replace(marker, "缺失项"), encoding="utf-8")
                before = self.state_file.read_bytes()
                with self.assertRaisesRegex(workflow.WorkflowError, expected_error):
                    workflow.mark_video_prompts(self.config)
                self.assert_state_unchanged(before)
                prompts[0].write_text(valid, encoding="utf-8")

    def test_redo_videos_archives_completed_version_and_returns_to_frames_ready(self):
        self.prepare_frames()
        prompts = self.write_video_prompts()
        manual = self.make_video("manual finished.mp4", audio=True)
        publish = self.video_dir / workflow.PUBLISH_COPY_FILE
        link_title = self.video_dir / workflow.VIDEO_LINK_TITLE_FILE
        publish.write_text("Old publish copy\n#OldTag\n", encoding="utf-8")
        link_title.write_text("Old link title\n", encoding="utf-8")
        self.set_task(video_attempts={"1": 2, "2": 1})
        workflow.register_manual_video(self.config, manual)
        originals = {
            path.name: path.read_bytes()
            for path in (*prompts, manual, publish, link_title)
        }

        result = workflow.redo_videos(self.config)
        task = result["active_task"]
        archive = Path(result["archived_video_version"])

        self.assertEqual(task["stage"], "frames-ready")
        self.assertEqual(task["video_attempts"], {})
        self.assertEqual(task["video_baseline"], {})
        self.assertEqual(task["videos"], [])
        self.assertIsNone(task["merge"])
        self.assertEqual(archive.name, "001")
        for name, content in originals.items():
            self.assertEqual((archive / name).read_bytes(), content)
            self.assertEqual((self.video_dir / name).read_bytes(), content)
        self.assertEqual(self.read_state()["active_task"], task)

    def test_redo_videos_rejects_unfinished_task_and_rolls_back_save_failure(self):
        self.prepare_frames()
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "已完成"):
            workflow.redo_videos(self.config)
        self.assert_state_unchanged(before)

        manual = self.make_video("manual.mp4", audio=True)
        workflow.register_manual_video(self.config, manual)
        before = self.state_file.read_bytes()
        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=workflow.WorkflowError("state save failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "state save failed"):
                workflow.redo_videos(self.config)
        self.assert_state_unchanged(before)
        archive_root = self.video_dir / workflow.VIDEO_REDO_ARCHIVE_DIR
        self.assertEqual(
            [path for path in archive_root.iterdir() if path.is_dir()],
            [],
        )

    def test_mark_video_prompts_rejects_video_candidate_symlink(self):
        self.prepare_frames()
        self.write_video_prompts()
        external = self.root / "external.mp4"
        external.write_bytes(b"outside")
        candidate = self.video_dir / "download.mp4"
        candidate.symlink_to(external)
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow.mark_video_prompts(self.config)

        self.assert_state_unchanged(before)

    def test_probe_media_real_files_and_media_compatible(self):
        first = self.make_video("first.mp4", audio=True)
        same = self.make_video("same.mov", color="green", audio=True)
        different_geometry = self.make_video("different.mp4", size=(48, 64), audio=True)
        no_audio = self.make_video("silent.mp4", audio=False)

        first_media = workflow.probe_media(self.config, first)
        same_media = workflow.probe_media(self.config, same)
        geometry_media = workflow.probe_media(self.config, different_geometry)
        silent_media = workflow.probe_media(self.config, no_audio)

        self.assertGreater(first_media["duration"], 0)
        self.assertEqual(
            set(first_media["video"]),
            {"codec_name", "width", "height", "pix_fmt", "r_frame_rate"},
        )
        self.assertEqual((first_media["video"]["width"], first_media["video"]["height"]), (32, 48))
        self.assertIsNotNone(first_media["audio"])
        self.assertIn("codec_name", first_media["audio"])
        self.assertIn("sample_rate", first_media["audio"])
        self.assertTrue(
            "channel_layout" in first_media["audio"] or "channels" in first_media["audio"]
        )
        self.assertIsNone(silent_media["audio"])
        self.assertTrue(workflow.media_compatible(first_media, same_media))
        self.assertFalse(workflow.media_compatible(first_media, geometry_media))
        self.assertFalse(workflow.media_compatible(first_media, silent_media))

    def test_probe_media_translates_tool_process_json_and_schema_errors(self):
        media = self.make_video("valid.mp4")
        missing_tool = workflow.Config(
            self.catalog,
            self.done_root,
            self.state_file,
            ffmpeg=self.FFMPEG,
            ffprobe=str(self.root / "missing-ffprobe"),
        )
        with self.assertRaises(workflow.WorkflowError):
            workflow.probe_media(missing_tool, media)

        cases = (
            subprocess.CompletedProcess([], 1, stdout="", stderr="probe failed"),
            subprocess.CompletedProcess([], 0, stdout="not-json", stderr=""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"format": {"duration": "1"}, "streams": []}),
                stderr="",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {
                        "format": {"duration": "1"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 32,
                                "height": 48,
                                "r_frame_rate": "10/1",
                            }
                        ],
                    }
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {
                        "format": {"duration": "nan"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 32,
                                "height": 48,
                                "pix_fmt": "yuv420p",
                                "r_frame_rate": "10/1",
                            }
                        ],
                    }
                ),
                stderr="",
            ),
        )
        for completed in cases:
            with self.subTest(stdout=completed.stdout):
                with patch.object(workflow.subprocess, "run", return_value=completed) as run:
                    with self.assertRaises(workflow.WorkflowError):
                        workflow.probe_media(self.config, media)
                self.assertFalse(run.call_args.kwargs["shell"])
                self.assertIsInstance(run.call_args.args[0], list)

    def test_probe_media_falls_back_to_format_when_video_duration_is_missing_or_invalid(self):
        media = self.make_video("fallback-duration.mp4")
        base_stream = {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 32,
            "height": 48,
            "pix_fmt": "yuv420p",
            "r_frame_rate": "10/1",
        }
        for stream_duration in (None, "nan"):
            with self.subTest(stream_duration=stream_duration):
                stream = dict(base_stream)
                if stream_duration is not None:
                    stream["duration"] = stream_duration
                completed = subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        {
                            "format": {"duration": "1.25"},
                            "streams": [stream],
                        }
                    ),
                    stderr="",
                )
                with patch.object(workflow.subprocess, "run", return_value=completed):
                    result = workflow.probe_media(self.config, media)
                self.assertEqual(result["duration"], 1.25)

    def test_ingest_videos_requires_exactly_two_changed_candidates(self):
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)

        self.prepare_awaiting_videos()
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "恰好两个"):
            workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)

        first = self.make_video("one.mp4")
        with self.assertRaisesRegex(workflow.WorkflowError, "恰好两个"):
            workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)

        second = self.make_video("two.mov")
        third = self.make_video("three.m4v")
        with self.assertRaisesRegex(workflow.WorkflowError, "恰好两个"):
            workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)
        self.assertTrue(all(path.exists() for path in (first, second, third)))

    def test_ingest_videos_orders_by_distinct_mtime_and_normalizes_suffixes(self):
        reference, frames = self.prepare_frames()
        prompts = self.write_video_prompts()
        unchanged = self.make_video("unchanged.mp4", color="white")
        workflow.mark_video_prompts(self.config)
        first = self.make_video("browser-new.MP4", color="red", audio=True)
        second = self.make_video("browser-latest.MOV", color="blue", audio=True)
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()

        result = workflow.ingest_videos(self.config)

        canonical_first = self.video_dir / "01-segment.mp4"
        canonical_second = self.video_dir / "02-segment.mov"
        self.assertEqual(result["stage"], "videos-ready")
        self.assertEqual(result["video_baseline"], {})
        self.assertEqual(
            result["videos"],
            [str(canonical_first.resolve()), str(canonical_second.resolve())],
        )
        self.assertEqual(canonical_first.read_bytes(), first_bytes)
        self.assertEqual(canonical_second.read_bytes(), second_bytes)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(unchanged.exists())
        self.assertTrue(reference.exists())
        self.assertTrue(all(path.exists() for path in frames))
        self.assertTrue(all(path.exists() for path in prompts))
        self.assertEqual(result, self.read_state()["active_task"])

    def test_ingest_videos_rejects_equal_mtime_symlink_unstable_and_invalid_media(self):
        self.prepare_awaiting_videos()
        before = self.state_file.read_bytes()
        first = self.make_video("first.mp4")
        second = self.make_video("second.mp4", color="green")
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 1_000_000_000)
        with self.assertRaisesRegex(workflow.WorkflowError, "mtime"):
            workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)
        first.unlink()
        second.unlink()

        outside = self.make_video("outside.mp4")
        linked = self.video_dir / "linked.mov"
        linked.symlink_to(outside)
        valid = self.make_video("valid.mp4")
        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)
        linked.unlink()
        outside.unlink()
        valid.unlink()

        unstable = self.make_video("unstable.mp4")
        stable = self.make_video("stable.mov", color="blue")
        self.set_mtime(unstable, 2_000_000_000)
        self.set_mtime(stable, 3_000_000_000)

        def mutate(path):
            if Path(path) == unstable:
                with path.open("ab") as handle:
                    handle.write(b"changed")

        with patch.object(workflow, "_after_video_candidate_open", side_effect=mutate):
            with self.assertRaisesRegex(workflow.WorkflowError, "变化"):
                workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)
        unstable.unlink()
        stable.unlink()

        invalid = self.video_dir / "invalid.mp4"
        invalid.write_bytes(b"not-media")
        valid = self.make_video("valid.mov")
        self.set_mtime(invalid, 4_000_000_000)
        self.set_mtime(valid, 5_000_000_000)
        with self.assertRaises(workflow.WorkflowError):
            workflow.ingest_videos(self.config)
        self.assert_state_unchanged(before)
        self.assertTrue(invalid.exists())
        self.assertTrue(valid.exists())

    def test_ingest_videos_accepts_changed_baseline_name_and_rejects_canonical_conflict(self):
        self.prepare_frames()
        self.write_video_prompts()
        replaced = self.make_video("same-name.mp4", color="red")
        workflow.mark_video_prompts(self.config)
        baseline_inode = self.read_state()["active_task"]["video_baseline"][replaced.name]["inode"]
        replacement = self.make_video("replacement.mp4", color="green")
        os.replace(replacement, replaced)
        self.assertNotEqual(replaced.stat().st_ino, baseline_inode)
        second = self.make_video("second.mov", color="blue")
        self.set_mtime(replaced, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        conflict = self.video_dir / "01-segment.mov"
        conflict.write_bytes(b"foreign-canonical")
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "拒绝覆盖"):
            workflow.ingest_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertEqual(conflict.read_bytes(), b"foreign-canonical")
        self.assertTrue(replaced.exists())
        self.assertTrue(second.exists())

    def test_ingest_videos_rolls_back_second_rename_and_state_save_failures(self):
        for failure in ("rename", "state"):
            with self.subTest(failure=failure):
                if self.read_state()["active_task"]["stage"] != "awaiting-videos":
                    self.set_task(stage="awaiting-videos", video_baseline={}, videos=[])
                first = self.make_video(f"{failure}-first.mp4", color="red")
                second = self.make_video(f"{failure}-second.mov", color="blue")
                self.set_mtime(first, 1_000_000_000)
                self.set_mtime(second, 2_000_000_000)
                first_bytes = first.read_bytes()
                second_bytes = second.read_bytes()
                before = self.state_file.read_bytes()

                if failure == "rename":
                    context = patch.object(
                        workflow,
                        "_before_video_candidate_rename",
                        side_effect=lambda index, source, destination: (
                            (_ for _ in ()).throw(OSError("second rename failed"))
                            if index == 2
                            else None
                        ),
                    )
                else:
                    context = patch.object(
                        workflow,
                        "save_state_atomic",
                        side_effect=workflow.WorkflowError("state save failed"),
                    )
                with context:
                    with self.assertRaises(workflow.WorkflowError):
                        workflow.ingest_videos(self.config)

                self.assert_state_unchanged(before)
                self.assertEqual(first.read_bytes(), first_bytes)
                self.assertEqual(second.read_bytes(), second_bytes)
                self.assertFalse((self.video_dir / "01-segment.mp4").exists())
                self.assertFalse((self.video_dir / "02-segment.mov").exists())
                first.unlink()
                second.unlink()

    def test_ingest_videos_keeps_segments_when_state_committed_then_save_raises(self):
        self.prepare_awaiting_videos()
        first = self.make_video("committed-first.mp4", color="red")
        second = self.make_video("committed-second.mov", color="blue")
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()
        real_save = workflow.save_state_atomic
        committed_state = None

        def commit_then_raise(config, state_value):
            nonlocal committed_state
            real_save(config, state_value)
            committed_state = copy.deepcopy(state_value)
            raise workflow.WorkflowError("raised after committed state")

        with patch.object(workflow, "save_state_atomic", side_effect=commit_then_raise):
            with self.assertRaisesRegex(workflow.WorkflowError, "after committed"):
                workflow.ingest_videos(self.config)

        canonical_first = self.video_dir / "01-segment.mp4"
        canonical_second = self.video_dir / "02-segment.mov"
        self.assertEqual(self.read_state(), committed_state)
        self.assertEqual(self.read_state()["active_task"]["stage"], "videos-ready")
        self.assertEqual(canonical_first.read_bytes(), first_bytes)
        self.assertEqual(canonical_second.read_bytes(), second_bytes)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_ingest_videos_rolls_back_failure_after_first_rename(self):
        self.prepare_awaiting_videos()
        first = self.make_video("post-first.mp4", color="red")
        second = self.make_video("post-second.mov", color="blue")
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()
        before = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "_after_video_candidate_renamed",
            create=True,
            side_effect=OSError("post rename failed"),
        ):
            with self.assertRaises(workflow.WorkflowError):
                workflow.ingest_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_bytes(), second_bytes)
        self.assertFalse((self.video_dir / "01-segment.mp4").exists())
        self.assertFalse((self.video_dir / "02-segment.mov").exists())

    def test_ingest_videos_continues_rollback_after_one_restore_fails(self):
        self.prepare_awaiting_videos()
        first = self.make_video("rollback-first.mp4", color="red")
        second = self.make_video("rollback-second.mov", color="blue")
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        first_bytes = first.read_bytes()
        before = self.state_file.read_bytes()
        original_rename = workflow._rename_noreplace

        def fail_second_restore(source_fd, source_name, target_fd, target_name):
            if source_name == "02-segment.mov" and target_name == second.name:
                return False
            return original_rename(source_fd, source_name, target_fd, target_name)

        with patch.object(
            workflow,
            "_after_video_candidate_renamed",
            side_effect=lambda index, source, destination: (
                (_ for _ in ()).throw(OSError("force rollback"))
                if index == 2
                else None
            ),
        ), patch.object(
            workflow,
            "_rename_noreplace",
            side_effect=fail_second_restore,
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "回滚失败"):
                workflow.ingest_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertFalse((self.video_dir / "01-segment.mp4").exists())
        self.assertFalse(second.exists())
        self.assertTrue((self.video_dir / "02-segment.mov").exists())

    def test_merge_videos_compatible_copy_preserves_sources_and_handles_quoted_path(self):
        first, second = self.prepare_segments(first_audio=True, second_audio=True)
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()

        with patch.object(workflow, "_run_ffmpeg", wraps=workflow._run_ffmpeg) as run:
            result = workflow.merge_videos(self.config)

        final = self.video_dir / "final.mp4"
        self.assertEqual(result["stage"], "merged")
        self.assertEqual(result["merge"]["output"], str(final.resolve()))
        self.assertEqual(result["merge"]["mode"], "copy")
        self.assertGreater(result["merge"]["duration"], 0)
        self.assertEqual(result, self.read_state()["active_task"])
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_bytes(), second_bytes)
        self.assertTrue(final.is_file())
        self.assertEqual(len(run.call_args_list), 1)
        copy_command = run.call_args_list[0].args[0]
        self.assertIsInstance(copy_command, list)
        self.assertIn("concat", copy_command)
        self.assertEqual(copy_command[-3:-1], ["-c", "copy"])
        self.assertEqual(list(self.video_dir.glob(".merge-*")), [])

    def test_merge_videos_transcodes_incompatible_geometry(self):
        first, second = self.prepare_segments(
            first_size=(32, 48),
            second_size=(48, 64),
            first_audio=True,
            second_audio=True,
        )

        with patch.object(workflow, "_run_ffmpeg", wraps=workflow._run_ffmpeg) as run:
            result = workflow.merge_videos(self.config)

        final_media = workflow.probe_media(self.config, self.video_dir / "final.mp4")
        self.assertEqual(result["merge"]["mode"], "transcode")
        self.assertEqual((final_media["video"]["width"], final_media["video"]["height"]), (32, 48))
        self.assertIsNotNone(final_media["audio"])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        normalize_commands = [call.args[0] for call in run.call_args_list[:-1]]
        self.assertEqual(len(normalize_commands), 2)
        for command in normalize_commands:
            video_filter = command[command.index("-vf") + 1]
            self.assertIn("force_original_aspect_ratio=decrease", video_filter)
            self.assertIn("pad=", video_filter)

    def test_ingest_videos_rejects_silent_segments(self):
        for first_audio, second_audio in ((False, False), (True, False)):
            with self.subTest(first_audio=first_audio, second_audio=second_audio):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                catalog = root / "catalog"
                done_root = root / "done"
                state_file = root / "state" / "state.json"
                product = catalog / "001_product"
                product.mkdir(parents=True)
                done_root.mkdir()
                source = product / "source.png"
                Image.new("RGB", (90, 160), "black").save(source)
                config = workflow.Config(
                    catalog,
                    done_root,
                    state_file,
                    ffmpeg=self.FFMPEG,
                    ffprobe=self.FFPROBE,
                )
                task = workflow.select_product(
                    config, FirstChoice(), date(2026, 7, 15)
                )
                image_dir = Path(task["image_dir"])
                video_dir = Path(task["video_dir"])
                reference = image_dir / workflow.REFERENCE_FILE
                Image.new("RGB", (90, 160), "black").save(reference)
                frames = {}
                for index in workflow.FRAME_FILES:
                    frame = image_dir / workflow.FRAME_FILES[index]
                    Image.new("RGB", (90, 160), "blue").save(frame)
                    frames[str(index)] = str(frame.resolve())
                state = json.loads(state_file.read_text(encoding="utf-8"))
                state["active_task"].update(
                    {
                        "stage": "frames-ready",
                        "reference": str(reference.resolve()),
                        "frames": frames,
                    }
                )
                workflow.save_state_atomic(config, state)
                for name in workflow.VIDEO_PROMPTS:
                    (video_dir / name).write_text(
                        "英语口播：自然口语；人物嘴型同步；视觉爆点；"
                        "有效口播至少覆盖三分之二片段。",
                        encoding="utf-8",
                    )
                workflow.mark_video_prompts(config)

                def make(path: Path, color: str, audio: bool) -> None:
                    command = [
                        self.FFMPEG,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=32x48:r=10:d=0.4",
                    ]
                    if audio:
                        command.extend(
                            [
                                "-f",
                                "lavfi",
                                "-i",
                                "sine=frequency=440:sample_rate=44100:duration=0.4",
                                "-shortest",
                            ]
                        )
                    command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
                    if audio:
                        command.extend(["-c:a", "aac"])
                    command.append(str(path))
                    subprocess.run(command, check=True, capture_output=True)

                first = video_dir / "first.mp4"
                second = video_dir / "second.mov"
                make(first, "red", first_audio)
                make(second, "blue", second_audio)
                self.set_mtime(first, 1_000_000_000)
                self.set_mtime(second, 2_000_000_000)
                before = state_file.read_bytes()

                with self.assertRaisesRegex(workflow.WorkflowError, "口播音轨"):
                    workflow.ingest_videos(config)

                self.assertEqual(state_file.read_bytes(), before)
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())

    def test_merge_transcode_pads_short_audio_to_each_video_duration(self):
        self.prepare_awaiting_videos()
        first = self.make_video(
            "short-audio-first.mp4",
            size=(32, 48),
            duration=2.0,
            audio=True,
            audio_duration=1.6,
        )
        second = self.make_video(
            "short-audio-second.mov",
            size=(48, 64),
            duration=2.0,
            audio=True,
            audio_duration=1.6,
        )
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        source_media = [workflow.probe_media(self.config, path) for path in (first, second)]
        for path, media in zip((first, second), source_media):
            self.assertAlmostEqual(media["duration"], 2.0, delta=0.1)
            self.assertAlmostEqual(self.audio_stream_duration(path), 1.6, delta=0.1)
        segments = workflow.ingest_videos(self.config)["videos"]

        result = workflow.merge_videos(self.config)

        final = self.video_dir / "final.mp4"
        final_media = workflow.probe_media(self.config, final)
        expected_duration = sum(media["duration"] for media in source_media)
        self.assertEqual(result["merge"]["mode"], "transcode")
        self.assertAlmostEqual(final_media["duration"], expected_duration, delta=0.15)
        self.assertAlmostEqual(
            self.audio_stream_duration(final),
            final_media["duration"],
            delta=0.15,
        )
        self.assertTrue(all(Path(path).is_file() for path in segments))

    def test_merge_uses_video_stream_duration_when_audio_is_longer(self):
        self.prepare_awaiting_videos()
        first = self.make_video(
            "long-audio-first.mp4",
            size=(32, 48),
            duration=2.0,
            audio=True,
            audio_duration=3.0,
        )
        second = self.make_video(
            "long-audio-second.mov",
            size=(48, 64),
            duration=2.0,
            audio=True,
            audio_duration=3.0,
        )
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        for path in (first, second):
            self.assertAlmostEqual(self.video_stream_duration(path), 2.0, delta=0.1)
            self.assertAlmostEqual(self.audio_stream_duration(path), 3.0, delta=0.1)
        source_media = [workflow.probe_media(self.config, path) for path in (first, second)]
        segments = workflow.ingest_videos(self.config)["videos"]

        result = workflow.merge_videos(self.config)

        final = self.video_dir / "final.mp4"
        final_media = workflow.probe_media(self.config, final)
        self.assertTrue(all(abs(media["duration"] - 2.0) <= 0.1 for media in source_media))
        self.assertEqual(result["merge"]["mode"], "transcode")
        self.assertAlmostEqual(final_media["duration"], 4.0, delta=0.15)
        self.assertAlmostEqual(self.audio_stream_duration(final), 4.0, delta=0.15)
        self.assertEqual(final_media["video"]["codec_name"], "h264")
        self.assertEqual(final_media["video"]["pix_fmt"], "yuv420p")
        self.assertEqual(final_media["audio"]["codec_name"], "aac")
        self.assertEqual(final_media["audio"]["sample_rate"], 48000)
        self.assertTrue(all(Path(path).is_file() for path in segments))

    def test_merge_transcodes_compatible_vp8_vorbis_webm_for_mp4_output(self):
        self.prepare_awaiting_videos()
        first = self.make_video(
            "legacy-first.webm",
            color="red",
            audio=True,
            video_codec="libvpx",
            audio_codec="libvorbis",
        )
        second = self.make_video(
            "legacy-second.webm",
            color="blue",
            audio=True,
            video_codec="libvpx",
            audio_codec="libvorbis",
        )
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        source_media = [workflow.probe_media(self.config, path) for path in (first, second)]
        self.assertTrue(workflow.media_compatible(source_media[0], source_media[1]))
        self.assertEqual(source_media[0]["video"]["codec_name"], "vp8")
        self.assertEqual(source_media[0]["audio"]["codec_name"], "vorbis")
        segments = workflow.ingest_videos(self.config)["videos"]

        result = workflow.merge_videos(self.config)

        final_media = workflow.probe_media(self.config, self.video_dir / "final.mp4")
        self.assertEqual(result["merge"]["mode"], "transcode")
        self.assertEqual(final_media["video"]["codec_name"], "h264")
        self.assertEqual(final_media["video"]["pix_fmt"], "yuv420p")
        self.assertEqual(final_media["audio"]["codec_name"], "aac")
        self.assertEqual(final_media["audio"]["sample_rate"], 48000)
        self.assertTrue(all(Path(path).is_file() for path in segments))

    def test_merge_videos_rejects_duration_error_and_unknown_final(self):
        first, second = self.prepare_segments()
        before = self.state_file.read_bytes()
        real_probe = workflow.probe_media

        def wrong_final_duration(config, path):
            media = real_probe(config, path)
            if Path(path).name == "merged.mp4":
                media["duration"] += 2.0
            return media

        with patch.object(workflow, "probe_media", side_effect=wrong_final_duration):
            with self.assertRaisesRegex(workflow.WorkflowError, "时长"):
                workflow.merge_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertFalse((self.video_dir / "final.mp4").exists())
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(list(self.video_dir.glob(".merge-*")), [])

        foreign = self.video_dir / "final.mp4"
        foreign.write_bytes(b"unknown-final")
        with self.assertRaisesRegex(workflow.WorkflowError, "拒绝覆盖"):
            workflow.merge_videos(self.config)
        self.assertEqual(foreign.read_bytes(), b"unknown-final")
        self.assert_state_unchanged(before)

    def test_merge_videos_state_failure_removes_final_and_preserves_segments(self):
        first, second = self.prepare_segments()
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()
        before = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=workflow.WorkflowError("state save failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "state save failed"):
                workflow.merge_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertFalse((self.video_dir / "final.mp4").exists())
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_bytes(), second_bytes)
        self.assertEqual(list(self.video_dir.glob(".merge-*")), [])

    def test_merge_keeps_final_when_state_committed_then_save_raises_base_exception(self):
        first, second = self.prepare_segments()
        real_save = workflow.save_state_atomic
        committed_state = None

        def commit_then_interrupt(config, state_value):
            nonlocal committed_state
            real_save(config, state_value)
            committed_state = copy.deepcopy(state_value)
            raise KeyboardInterrupt("interrupted after committed state")

        with patch.object(workflow, "save_state_atomic", side_effect=commit_then_interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "after committed"):
                workflow.merge_videos(self.config)

        final = self.video_dir / "final.mp4"
        self.assertEqual(self.read_state(), committed_state)
        self.assertEqual(self.read_state()["active_task"]["stage"], "merged")
        self.assertTrue(final.is_file())
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())
        workflow.probe_media(self.config, final)

    def test_merge_videos_rolls_back_failure_immediately_after_final_publish(self):
        first, second = self.prepare_segments()
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()
        before = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "_after_final_published",
            create=True,
            side_effect=OSError("post final publish failed"),
        ):
            with self.assertRaises(workflow.WorkflowError):
                workflow.merge_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertFalse((self.video_dir / "final.mp4").exists())
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_bytes(), second_bytes)
        self.assertEqual(list(self.video_dir.glob(".merge-*")), [])

    def test_merge_videos_rolls_back_final_modified_in_place_after_publish(self):
        first, second = self.prepare_segments()
        before = self.state_file.read_bytes()

        def modify_published(path):
            with Path(path).open("ab") as handle:
                handle.write(b"modified-after-publish")

        with patch.object(
            workflow,
            "_after_final_published",
            side_effect=modify_published,
        ):
            with self.assertRaises(workflow.WorkflowError):
                workflow.merge_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertFalse((self.video_dir / "final.mp4").exists())
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(list(self.video_dir.glob(".merge-*")), [])

    def test_merge_videos_merged_is_idempotent_after_validating_final(self):
        first, second = self.prepare_segments()
        initial = workflow.merge_videos(self.config)
        final = self.video_dir / "final.mp4"
        final_bytes = final.read_bytes()

        with patch.object(
            workflow,
            "_run_ffmpeg",
            side_effect=AssertionError("ffmpeg must not run twice"),
        ):
            repeated = workflow.merge_videos(self.config)

        self.assertEqual(repeated, initial)
        self.assertEqual(final.read_bytes(), final_bytes)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_merged_idempotence_rejects_record_source_and_mode_drift(self):
        first, second = self.prepare_segments()
        merged = workflow.merge_videos(self.config)
        recorded_duration = merged["merge"]["duration"]

        changed_merge = dict(merged["merge"])
        changed_merge["duration"] = recorded_duration + 5.0
        self.set_task(merge=changed_merge)
        with self.assertRaises(workflow.WorkflowError):
            workflow.merge_videos(self.config)

        changed_merge["duration"] = recorded_duration
        self.set_task(merge=changed_merge)
        second_bytes = second.read_bytes()
        replacement = self.make_video("longer-replacement.mov", duration=2.4)
        os.replace(replacement, second)
        with self.assertRaises(workflow.WorkflowError):
            workflow.merge_videos(self.config)

        second.write_bytes(second_bytes)
        changed_merge["mode"] = "transcode"
        self.set_task(merge=changed_merge)
        with self.assertRaises(workflow.WorkflowError):
            workflow.merge_videos(self.config)

    def test_merge_videos_rejects_wrong_stage_symlink_and_unstable_segment(self):
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "阶段"):
            workflow.merge_videos(self.config)
        self.assert_state_unchanged(before)

        first, second = self.prepare_segments()
        videos_ready = self.state_file.read_bytes()
        outside = self.make_video("outside.mp4", color="green")
        first.unlink()
        first.symlink_to(outside)
        with self.assertRaisesRegex(workflow.WorkflowError, "符号链接"):
            workflow.merge_videos(self.config)
        self.assert_state_unchanged(videos_ready)
        first.unlink()
        os.replace(outside, first)

        def mutate(path):
            if Path(path) == second:
                with second.open("ab") as handle:
                    handle.write(b"changed")

        with patch.object(workflow, "_after_merge_segment_open", side_effect=mutate):
            with self.assertRaisesRegex(workflow.WorkflowError, "变化"):
                workflow.merge_videos(self.config)
        self.assert_state_unchanged(videos_ready)
        self.assertFalse((self.video_dir / "final.mp4").exists())

    def test_merge_videos_ffmpeg_failure_cleans_temp_and_preserves_sources(self):
        first, second = self.prepare_segments()
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()
        before = self.state_file.read_bytes()

        with patch.object(
            workflow,
            "_run_ffmpeg",
            side_effect=workflow.WorkflowError("ffmpeg failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ffmpeg failed"):
                workflow.merge_videos(self.config)

        self.assert_state_unchanged(before)
        self.assertFalse((self.video_dir / "final.mp4").exists())
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_bytes(), second_bytes)
        self.assertEqual(list(self.video_dir.glob(".merge-*")), [])

    def test_register_manual_video_finishes_any_active_stage_and_is_idempotent(self):
        manual = self.make_video("manual finished video.mp4", audio=True)
        manual_bytes = manual.read_bytes()
        unfinished_stages = tuple(
            stage for stage in workflow.STAGES if stage != "merged"
        )

        for stage in unfinished_stages:
            with self.subTest(stage=stage):
                self.set_task(
                    stage=stage,
                    video_baseline={},
                    merge=None,
                )
                result = workflow.register_manual_video(self.config, manual)

                self.assertEqual(result["stage"], "merged")
                self.assertEqual(result["merge"]["mode"], "manual")
                self.assertEqual(result["merge"]["output"], str(manual.resolve()))
                self.assertGreater(result["merge"]["duration"], 0)
                self.assertEqual(manual.read_bytes(), manual_bytes)
                self.assertEqual(self.read_state()["active_task"], result)

        self.assertEqual(workflow.merge_videos(self.config), result)
        self.assertEqual(
            workflow.register_manual_video(self.config, manual),
            result,
        )

    def test_register_manual_video_rejects_unsafe_inputs_and_rolls_back_state_failure(self):
        manual = self.make_video("manual.mp4")
        before = self.state_file.read_bytes()
        manual_bytes = manual.read_bytes()
        with patch.object(
            workflow,
            "save_state_atomic",
            side_effect=workflow.WorkflowError("state save failed"),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "state save failed"):
                workflow.register_manual_video(self.config, manual)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(manual.read_bytes(), manual_bytes)

        outside = self.root / "outside.mp4"
        os.replace(manual, outside)
        with self.assertRaises(workflow.WorkflowError):
            workflow.register_manual_video(self.config, outside)
        manual.symlink_to(outside)
        with self.assertRaises(workflow.WorkflowError):
            workflow.register_manual_video(self.config, manual)
        manual.unlink()
        manual.write_bytes(b"not-media")
        with self.assertRaises(workflow.WorkflowError):
            workflow.register_manual_video(self.config, manual)

    def test_register_manual_video_rejects_missing_spoken_audio_track(self):
        silent = self.make_video("silent-manual.mp4", audio=False)
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(workflow.WorkflowError, "口播音轨"):
            workflow.register_manual_video(self.config, silent)

        self.assert_state_unchanged(before)
        self.assertTrue(silent.exists())

    def test_validate_product_copy_accepts_shop_title_content_and_size(self):
        reference = self.image_dir / workflow.REFERENCE_FILE
        Image.new("RGB", (90, 160), "black").save(reference)
        self.set_task(stage="reference-ready", reference=str(reference.resolve()))
        task_dir = Path(self.task["task_dir"])
        title = task_dir / workflow.SHOP_PRODUCT_TITLE_FILE
        content = task_dir / workflow.SHOP_PRODUCT_CONTENT_FILE
        size = task_dir / workflow.SHOP_PRODUCT_SIZE_FILE
        title.write_text(
            "Women's Black and Pink Colorblock High-Waist Bikini Set\n",
            encoding="utf-8",
        )
        content.write_text(
            "Color: Black and pink\n"
            "Style: Two-piece swimsuit\n"
            "Details: Wrap-style top and high-waisted bottoms\n",
            encoding="utf-8",
        )
        size.write_text("未提供尺码信息\n", encoding="utf-8")

        result = workflow.validate_product_copy(self.config)

        self.assertEqual(result["product_title"], title.read_text().strip())
        self.assertEqual(result["product_content"], content.read_text().strip())
        self.assertEqual(result["product_size"], "未提供尺码信息")
        self.assertEqual(result["product_title_file"], str(title.resolve()))
        self.assertEqual(result["product_content_file"], str(content.resolve()))
        self.assertEqual(result["product_size_file"], str(size.resolve()))

    def test_validate_product_copy_rejects_unready_or_invalid_copy(self):
        task_dir = Path(self.task["task_dir"])
        title = task_dir / workflow.SHOP_PRODUCT_TITLE_FILE
        content = task_dir / workflow.SHOP_PRODUCT_CONTENT_FILE
        size = task_dir / workflow.SHOP_PRODUCT_SIZE_FILE
        title.write_text("有效店铺标题\n", encoding="utf-8")
        content.write_text("Color: Black\n", encoding="utf-8")
        size.write_text("未提供尺码信息\n", encoding="utf-8")

        with self.assertRaisesRegex(workflow.WorkflowError, "参考图"):
            workflow.validate_product_copy(self.config)

        reference = self.image_dir / workflow.REFERENCE_FILE
        Image.new("RGB", (90, 160), "black").save(reference)
        self.set_task(stage="reference-ready", reference=str(reference.resolve()))
        invalid_cases = (
            ("两行\n标题\n", "Color: Black\n", "未提供尺码信息\n", "一行"),
            ("店铺标题\n", "带有 #标签 的内容\n", "未提供尺码信息\n", "标签"),
            ("店铺标题\n", "这不是参数清单。\n", "未提供尺码信息\n", "Field: Value"),
            ("店铺标题\n", "Color: Black\n", "\n", "尺寸"),
        )
        for title_text, content_text, size_text, message in invalid_cases:
            with self.subTest(message=message):
                title.write_text(title_text, encoding="utf-8")
                content.write_text(content_text, encoding="utf-8")
                size.write_text(size_text, encoding="utf-8")
                with self.assertRaisesRegex(workflow.WorkflowError, message):
                    workflow.validate_product_copy(self.config)

    def test_registered_manual_video_rejects_same_duration_content_replacement(self):
        manual = self.make_video("manual.mp4", color="red")
        workflow.register_manual_video(self.config, manual)
        replacement = self.make_video("replacement.mp4", color="blue")
        os.replace(replacement, manual)

        with self.assertRaisesRegex(workflow.WorkflowError, "内容|指纹|变化"):
            workflow.register_manual_video(self.config, manual)

    def test_validate_publish_copy_accepts_copy_ready_text_and_five_trailing_tags(self):
        manual = self.make_video("manual.mp4", audio=True)
        workflow.register_manual_video(self.config, manual)
        content = self.video_dir / workflow.PUBLISH_COPY_FILE
        title = self.video_dir / workflow.PRODUCT_TITLE_FILE
        content.write_text(
            "海边度假的明亮一刻，撞色连体泳衣让画面更有活力。\n"
            "#连体泳衣 #度假穿搭 #泳装推荐 #海边度假 #夏日穿搭\n",
            encoding="utf-8",
        )
        title.write_text("撞色印花高领连体泳衣\n", encoding="utf-8")

        result = workflow.validate_publish_copy(self.config)

        self.assertEqual(result["tag_count"], 5)
        self.assertIn("content", result)
        self.assertEqual(
            result["content"],
            "海边度假的明亮一刻，撞色连体泳衣让画面更有活力。\n"
            "#连体泳衣 #度假穿搭 #泳装推荐 #海边度假 #夏日穿搭",
        )
        self.assertEqual(result["title"], "撞色印花高领连体泳衣")
        self.assertEqual(result["video"], str(manual.resolve()))
        self.assertEqual(result["content_file"], str(content.resolve()))
        self.assertEqual(result["title_file"], str(title.resolve()))

    def test_validate_publish_copy_prefers_new_video_link_title_file(self):
        manual = self.make_video("manual.mp4", audio=True)
        workflow.register_manual_video(self.config, manual)
        content = self.video_dir / workflow.PUBLISH_COPY_FILE
        link_title = self.video_dir / workflow.VIDEO_LINK_TITLE_FILE
        legacy_title = self.video_dir / workflow.PRODUCT_TITLE_FILE
        content.write_text("真实泳池边穿搭。\n#泳装 #度假\n", encoding="utf-8")
        link_title.write_text("Black & Pink Wrap Bikini\n", encoding="utf-8")
        legacy_title.write_text("旧标题" * 20 + "\n", encoding="utf-8")

        result = workflow.validate_publish_copy(self.config)

        self.assertEqual(result["link_title"], "Black & Pink Wrap Bikini")
        self.assertEqual(result["title"], result["link_title"])
        self.assertEqual(result["link_title_file"], str(link_title.resolve()))
        self.assertEqual(result["title_file"], result["link_title_file"])

    def test_validate_publish_copy_rejects_invalid_title_tags_order_and_files(self):
        manual = self.make_video("manual.mp4")
        workflow.register_manual_video(self.config, manual)
        content = self.video_dir / workflow.PUBLISH_COPY_FILE
        title = self.video_dir / workflow.PRODUCT_TITLE_FILE

        valid_content = "真实度假泳装画面。\n#泳装 #度假\n"
        valid_title = "撞色连体泳衣\n"
        invalid_cases = (
            ("泳" * 31 + "\n", valid_content, "30"),
            ("两行\n标题\n", valid_content, "一行"),
            ("#标题\n", valid_content, "标题"),
            (valid_title, "#泳装 #度假\n正文放在标签后。\n", "标签"),
            (valid_title, "正文。\n#一 #二 #三 #四 #五 #六\n", "5"),
            (valid_title, "#泳装 #度假\n", "正文"),
        )
        for title_text, content_text, message in invalid_cases:
            with self.subTest(title=title_text, content=content_text):
                title.write_text(title_text, encoding="utf-8")
                content.write_text(content_text, encoding="utf-8")
                with self.assertRaisesRegex(workflow.WorkflowError, message):
                    workflow.validate_publish_copy(self.config)

        title.write_text(valid_title, encoding="utf-8")
        content.write_text(valid_content, encoding="utf-8")
        outside = self.root / "outside-copy.txt"
        outside.write_text(valid_content, encoding="utf-8")
        content.unlink()
        content.symlink_to(outside)
        with self.assertRaises(workflow.WorkflowError):
            workflow.validate_publish_copy(self.config)

    def test_validate_publish_copy_rejects_video_directory_symlink_swap(self):
        manual = self.make_video("manual.mp4")
        workflow.register_manual_video(self.config, manual)
        (self.video_dir / workflow.PUBLISH_COPY_FILE).write_text(
            "当前目录中的正文。\n#泳装 #度假\n",
            encoding="utf-8",
        )
        (self.video_dir / workflow.PRODUCT_TITLE_FILE).write_text(
            "当前目录中的标题\n",
            encoding="utf-8",
        )
        outside = self.root / "outside-video-dir"
        outside.mkdir()
        (outside / workflow.PUBLISH_COPY_FILE).write_text(
            "外部目录中的正文。\n#泳装 #度假\n",
            encoding="utf-8",
        )
        (outside / workflow.PRODUCT_TITLE_FILE).write_text(
            "外部目录中的标题\n",
            encoding="utf-8",
        )
        original_video_dir = self.root / "original-video-dir"
        original_validate = workflow._validate_idempotent_merge

        def swap_after_merge_validation(config, task):
            result = original_validate(config, task)
            self.video_dir.rename(original_video_dir)
            self.video_dir.symlink_to(outside, target_is_directory=True)
            return result

        try:
            with patch.object(
                workflow,
                "_validate_idempotent_merge",
                side_effect=swap_after_merge_validation,
            ):
                with self.assertRaisesRegex(workflow.WorkflowError, "视频目录|替换"):
                    workflow.validate_publish_copy(self.config)
        finally:
            if self.video_dir.is_symlink():
                self.video_dir.unlink()
            if original_video_dir.exists():
                original_video_dir.rename(self.video_dir)

    def test_validate_publish_copy_rejects_real_video_directory_replacement(self):
        manual = self.make_video("manual.mp4")
        workflow.register_manual_video(self.config, manual)
        replacement = self.root / "replacement-video-dir"
        replacement.mkdir()
        (replacement / workflow.PUBLISH_COPY_FILE).write_text(
            "替换目录中的正文。\n#泳装 #度假\n",
            encoding="utf-8",
        )
        (replacement / workflow.PRODUCT_TITLE_FILE).write_text(
            "替换目录中的标题\n",
            encoding="utf-8",
        )
        original_video_dir = self.root / "original-video-dir"
        original_validate = workflow._validate_idempotent_merge

        def replace_after_merge_validation(config, task):
            result = original_validate(config, task)
            self.video_dir.rename(original_video_dir)
            replacement.rename(self.video_dir)
            return result

        try:
            with patch.object(
                workflow,
                "_validate_idempotent_merge",
                side_effect=replace_after_merge_validation,
            ):
                with self.assertRaisesRegex(
                    workflow.WorkflowError,
                    "视频目录|任务目录|替换",
                ):
                    workflow.validate_publish_copy(self.config)
        finally:
            if self.video_dir.exists() and not self.video_dir.is_symlink():
                shutil.rmtree(self.video_dir)
            if original_video_dir.exists():
                original_video_dir.rename(self.video_dir)

    def test_validate_publish_copy_rejects_parent_task_directory_symlink_swap(self):
        manual = self.make_video("manual.mp4")
        workflow.register_manual_video(self.config, manual)
        task_dir = Path(self.task["task_dir"])
        outside_task = self.root / "outside-task"
        outside_video = outside_task / "视频制作"
        outside_video.mkdir(parents=True)
        (outside_video / workflow.PUBLISH_COPY_FILE).write_text(
            "外部任务中的正文。\n#泳装 #度假\n",
            encoding="utf-8",
        )
        (outside_video / workflow.PRODUCT_TITLE_FILE).write_text(
            "外部任务中的标题\n",
            encoding="utf-8",
        )
        original_task_dir = self.root / "original-task-dir"
        original_validate = workflow._validate_idempotent_merge

        def swap_parent_after_merge_validation(config, task):
            result = original_validate(config, task)
            task_dir.rename(original_task_dir)
            task_dir.symlink_to(outside_task, target_is_directory=True)
            return result

        try:
            with patch.object(
                workflow,
                "_validate_idempotent_merge",
                side_effect=swap_parent_after_merge_validation,
            ):
                with self.assertRaisesRegex(
                    workflow.WorkflowError,
                    "视频目录|任务目录|替换",
                ):
                    workflow.validate_publish_copy(self.config)
        finally:
            if task_dir.is_symlink():
                task_dir.unlink()
            if original_task_dir.exists():
                original_task_dir.rename(task_dir)

    def test_validate_publish_copy_rejects_content_changed_after_first_read(self):
        manual = self.make_video("manual.mp4")
        workflow.register_manual_video(self.config, manual)
        content = self.video_dir / workflow.PUBLISH_COPY_FILE
        title = self.video_dir / workflow.PRODUCT_TITLE_FILE
        content.write_text("有效正文。\n#泳装 #度假\n", encoding="utf-8")
        title.write_text("撞色连体泳衣\n", encoding="utf-8")

        def mutate_after_read(name):
            if name == workflow.PUBLISH_COPY_FILE:
                content.write_text("#泳装 #度假\n标签后的无效正文。\n", encoding="utf-8")

        with patch.object(
            workflow,
            "_after_publish_text_read",
            create=True,
            side_effect=mutate_after_read,
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "变化"):
                workflow.validate_publish_copy(self.config)

    def test_validate_publish_copy_rejects_video_directory_swap_after_open(self):
        manual = self.make_video("manual.mp4")
        workflow.register_manual_video(self.config, manual)
        (self.video_dir / workflow.PUBLISH_COPY_FILE).write_text(
            "有效正文。\n#泳装 #度假\n",
            encoding="utf-8",
        )
        (self.video_dir / workflow.PRODUCT_TITLE_FILE).write_text(
            "撞色连体泳衣\n",
            encoding="utf-8",
        )
        outside = self.root / "outside-video-dir"
        outside.mkdir()
        original_video_dir = self.root / "original-video-dir"

        def swap_after_directory_open(path):
            Path(path).rename(original_video_dir)
            Path(path).symlink_to(outside, target_is_directory=True)

        try:
            with patch.object(
                workflow,
                "_after_publish_directory_open",
                create=True,
                side_effect=swap_after_directory_open,
            ):
                with self.assertRaisesRegex(workflow.WorkflowError, "视频目录.*替换"):
                    workflow.validate_publish_copy(self.config)
        finally:
            if self.video_dir.is_symlink():
                self.video_dir.unlink()
            if original_video_dir.exists():
                original_video_dir.rename(self.video_dir)

    def test_manual_video_and_publish_copy_cli_commands_emit_json(self):
        manual = self.make_video("manual cli.mp4", audio=True)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(
                self.cli_roots() + ["register-manual-video", str(manual)]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "merged")

        (self.video_dir / workflow.PUBLISH_COPY_FILE).write_text(
            "手工视频里的海边度假穿搭。\n#泳装 #度假穿搭\n",
            encoding="utf-8",
        )
        (self.video_dir / workflow.PRODUCT_TITLE_FILE).write_text(
            "撞色连体泳衣\n",
            encoding="utf-8",
        )
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["validate-publish-copy"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["tag_count"], 2)

    def test_video_lifecycle_cli_commands_emit_json_and_chinese_errors(self):
        self.prepare_frames()
        self.write_video_prompts()
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["mark-video-prompts"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "awaiting-videos")

        first = self.make_video("cli first.mp4", color="red")
        second = self.make_video("cli second.mov", color="blue")
        self.set_mtime(first, 1_000_000_000)
        self.set_mtime(second, 2_000_000_000)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["ingest-videos"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "videos-ready")

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["merge"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "merged")

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main(self.cli_roots() + ["mark-video-prompts"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertRegex(stderr.getvalue(), r"[\u4e00-\u9fff]")
        self.assertNotIn("Traceback", stderr.getvalue())



if __name__ == "__main__":
    unittest.main()
