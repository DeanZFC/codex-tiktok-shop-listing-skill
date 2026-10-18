import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError


DEFAULT_WORKSPACE_ROOT = Path(
    os.environ.get("TIKTOK_VIDEO_WORKSPACE", Path.home() / "TikTokVideoWorkspace")
).expanduser()
DEFAULT_CATALOG = Path(
    os.environ.get("TIKTOK_VIDEO_CATALOG", DEFAULT_WORKSPACE_ROOT / "产品")
).expanduser()
LEGACY_CATALOG = DEFAULT_WORKSPACE_ROOT / "旧产品"
DEFAULT_LINK_HISTORY = Path(
    os.environ.get(
        "TIKTOK_VIDEO_LINK_HISTORY",
        DEFAULT_WORKSPACE_ROOT / ".swimwear-link-selection" / "history.json",
    )
).expanduser()
DEFAULT_DONE_ROOT = Path(
    os.environ.get("TIKTOK_VIDEO_DONE_ROOT", DEFAULT_WORKSPACE_ROOT / "已做")
).expanduser()
DEFAULT_STATE_FILE = (
    DEFAULT_WORKSPACE_ROOT / ".swimwear-tiktok-video-workflow" / "state.json"
)
TASK_STATE_FILE_NAME = "工作流进度.json"

STAGES = (
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
)

REFERENCE_FILE = "00-product-reference.png"
REFERENCE_ALLOWED_STAGES = {"selected", "reference-ready"}
REFERENCE_NEXT_STAGE = "reference-ready"
IMAGE_PROMPTS = {
    1: "01-start-prompt.txt",
    2: "02-bridge-prompt.txt",
    3: "03-end-prompt.txt",
}
FRAME_FILES = {1: "01-start.png", 2: "02-bridge.png", 3: "03-end.png"}
RETRY_ARCHIVE_DIR = "退回归档"
FORBIDDEN_PROMPT_TEXT = ("veo 3.1", "gpt image 2 pro")
IMAGE_STAGE_FLOW = {
    1: ("reference-ready", "awaiting-image-1", "image-1-ready"),
    2: ("image-1-ready", "awaiting-image-2", "image-2-ready"),
    3: ("image-2-ready", "awaiting-image-3", "frames-ready"),
}
IMAGE_RETRY_FLOW = {
    index: (success, predecessor)
    for index, (predecessor, _, success) in IMAGE_STAGE_FLOW.items()
}
IMAGE_ATTEMPT_LIMITS = {"api": 2, "website": 3}
IMAGE_DOWNLOAD_EXTENSIONS = {".jpeg", ".jpg", ".png", ".webp"}
VIDEO_PROMPTS = ("01-video-prompt.txt", "02-video-prompt.txt")
VIDEO_ATTEMPT_LIMIT = 3
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
VIDEO_REDO_ARCHIVE_DIR = "重做归档"
SHOP_PRODUCT_TITLE_FILE = "店铺产品标题.txt"
SHOP_PRODUCT_CONTENT_FILE = "店铺产品内容.txt"
SHOP_PRODUCT_SIZE_FILE = "店铺产品尺寸.txt"
PUBLISH_COPY_FILE = "tk发布内容.txt"
PRODUCT_TITLE_FILE = "产品标题.txt"
VIDEO_LINK_TITLE_FILE = "视频链接标题.txt"
VIDEO_PROMPT_STAGE = ("frames-ready", "awaiting-videos")
VIDEO_INGEST_STAGE = ("awaiting-videos", "videos-ready")
COPY_CONCAT_ARGS = ("-f", "concat", "-safe", "0", "-c", "copy")
NORMALIZE_VIDEO_ARGS = ("-c:v", "libx264", "-pix_fmt", "yuv420p")
NORMALIZE_AUDIO_ARGS = ("-c:a", "aac")

PRODUCT_PATTERN = re.compile(r"^[0-9]{3}_")
LEGACY_INDEXED_PATTERN = re.compile(r"^[0-9]{3}_[0-9]{4}_(.+)$")
DATE_DIR_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TASK_DIR_PATTERN = re.compile(r"^([0-9]{3})-(.+)$")
LINK_PRODUCT_FILES = (
    "TK店铺标题和内容.txt",
    "扫同款产品图.jpg",
    "尺寸配置图.png",
    "供应商短链接.txt",
)
PENDING_PATTERN = re.compile(
    r"^\.swimwear-pending-([0-9a-f]{64})-([0-9a-f]{64})\.json$"
)
TASK_FIELDS = {
    "product_name",
    "source_dir",
    "task_dir",
    "image_dir",
    "video_dir",
    "stage",
    "reference",
    "image_baseline",
    "video_baseline",
    "frames",
    "videos",
    "merge",
    "created_at",
    "updated_at",
}
OPTIONAL_TASK_FIELDS = {"image_attempts", "video_attempts", "image_mode"}


class WorkflowError(Exception):
    """Raised when the workflow cannot continue."""


@dataclass(frozen=True)
class Config:
    catalog: Path
    done_root: Path
    state_file: Path
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    link_history: Path | None = None
    source_product: Path | None = None


def task_state_file(task_dir: Path) -> Path:
    try:
        directory = Path(task_dir).expanduser()
        if not directory.is_absolute():
            directory = Path.cwd() / directory
        directory = directory.resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as exc:
        raise WorkflowError(f"无法解析任务目录：{exc}") from exc
    return directory / TASK_STATE_FILE_NAME


def _config_for_task(config: Config, task_dir: Path) -> Config:
    return replace(config, state_file=task_state_file(task_dir))


@dataclass(frozen=True)
class _ArchiveCreation:
    task: dict
    pending_name: str
    target_name: str
    target_identity: tuple[int, int]
    owned_target: bool
    owned_date: bool


def default_state() -> dict:
    return {"version": 1, "active_task": None}


def _state_digest(state_value: dict) -> str:
    payload = json.dumps(
        state_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise WorkflowError(f"无法访问{label}：{path}：{exc}") from exc


def _assert_real_directory(path: Path, label: str) -> os.stat_result:
    info = _lstat(path, label)
    if stat.S_ISLNK(info.st_mode):
        raise WorkflowError(f"{label}不能是符号链接：{path}")
    if not stat.S_ISDIR(info.st_mode):
        raise WorkflowError(f"{label}必须是真实目录：{path}")
    return info


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_pinned_directory(path: Path, label: str) -> tuple[int, os.stat_result]:
    expected = _assert_real_directory(path, label)
    try:
        descriptor = os.open(path, _directory_flags())
    except OSError as exc:
        raise WorkflowError(f"无法固定{label}：{path}：{exc}") from exc
    try:
        current = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise WorkflowError(f"无法验证{label}目录描述符：{path}：{exc}") from exc
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise WorkflowError(f"{label}在打开时发生变化：{path}")
    return descriptor, current


def _open_pinned_directory_at(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[int, os.stat_result]:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise WorkflowError(f"{label}目录名非法：{name}")
    descriptor = None
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode):
            raise WorkflowError(f"{label}不能是符号链接：{name}")
        if not stat.S_ISDIR(expected.st_mode):
            raise WorkflowError(f"{label}必须是真实目录：{name}")
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise WorkflowError(f"{label}在打开时发生变化：{name}")
        result = (descriptor, current)
        descriptor = None
        return result
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法固定{label}：{name}：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_task_directory_chain(
    done_root: Path,
    task_dir: Path,
) -> tuple[
    int,
    os.stat_result,
    int,
    os.stat_result,
    int,
    os.stat_result,
]:
    done_fd = date_fd = task_fd = None
    try:
        done_fd, done_identity = _open_pinned_directory(done_root, "已做目录")
        date_fd, date_identity = _open_pinned_directory_at(
            done_fd,
            task_dir.parent.name,
            "日期目录",
        )
        task_fd, task_identity = _open_pinned_directory_at(
            date_fd,
            task_dir.name,
            "任务目录",
        )
        return (
            done_fd,
            done_identity,
            date_fd,
            date_identity,
            task_fd,
            task_identity,
        )
    except BaseException:
        for descriptor in (task_fd, date_fd, done_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise


def _verify_task_directory_chain(
    done_root: Path,
    task_dir: Path,
    done_fd: int,
    done_identity: os.stat_result,
    date_fd: int,
    date_identity: os.stat_result,
    task_fd: int,
    task_identity: os.stat_result,
) -> None:
    _verify_pinned_path(done_root, done_fd, done_identity, "已做目录")
    _verify_pinned_entry(
        done_fd,
        task_dir.parent.name,
        date_fd,
        date_identity,
        "日期目录",
    )
    _verify_pinned_entry(
        date_fd,
        task_dir.name,
        task_fd,
        task_identity,
        "任务目录",
    )


def _verify_pinned_path(
    path: Path, descriptor: int, identity: os.stat_result, label: str
) -> None:
    try:
        current_path = path.lstat()
        current_fd = os.fstat(descriptor)
    except OSError as exc:
        raise WorkflowError(f"{label}在操作期间不可用：{path}：{exc}") from exc
    expected = (identity.st_dev, identity.st_ino)
    if stat.S_ISLNK(current_path.st_mode) or (
        current_path.st_dev,
        current_path.st_ino,
    ) != expected or (current_fd.st_dev, current_fd.st_ino) != expected:
        raise WorkflowError(f"{label}在操作期间被替换：{path}")


def _verify_pinned_entry(
    parent_fd: int,
    name: str,
    descriptor: int,
    identity: os.stat_result,
    label: str,
) -> None:
    try:
        current_entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        current_fd = os.fstat(descriptor)
    except OSError as exc:
        raise WorkflowError(f"{label}在操作期间不可用：{name}：{exc}") from exc
    expected = (identity.st_dev, identity.st_ino)
    if stat.S_ISLNK(current_entry.st_mode) or (
        current_entry.st_dev,
        current_entry.st_ino,
    ) != expected or (current_fd.st_dev, current_fd.st_ino) != expected:
        raise WorkflowError(f"{label}在操作期间被替换：{name}")


def _canonical_existing_directory(path: Path, label: str) -> Path:
    _assert_real_directory(path, label)
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"无法解析{label}：{path}：{exc}") from exc


def _validate_root_relationship(config: Config) -> tuple[Path, Path]:
    catalog = _canonical_existing_directory(config.catalog, "商品目录")
    done_root = _canonical_existing_directory(config.done_root, "已做目录")
    source_root = catalog
    if config.source_product is not None:
        source_root = _canonical_existing_directory(config.source_product, "指定产品目录")
        if source_root.parent != catalog:
            raise WorkflowError("指定产品必须直接位于商品目录中")
    if source_root == done_root or source_root.is_relative_to(done_root) or done_root.is_relative_to(
        source_root
    ):
        raise WorkflowError("商品目录与已做目录不能相同、重叠或互相包含")
    return catalog, done_root


def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _valid_product_name(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not value.startswith(".")
        and Path(value).name == value
        and not any(ord(character) < 32 for character in value)
    )


def _canonical_product_key(value: str) -> str:
    if not _valid_product_name(value):
        raise WorkflowError(f"商品货号非法：{value}")
    match = LEGACY_INDEXED_PATTERN.fullmatch(value)
    key = match.group(1) if match is not None else value
    return unicodedata.normalize("NFKC", key).strip().casefold()


def discover_products(catalog: Path) -> list[Path]:
    _assert_real_directory(catalog, "商品目录")
    products = []
    try:
        entries = sorted(catalog.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise WorkflowError(f"无法扫描商品目录：{exc}") from exc
    for path in entries:
        if not PRODUCT_PATTERN.match(path.name):
            continue
        try:
            info = path.lstat()
        except OSError as exc:
            raise WorkflowError(f"无法检查商品目录项：{path}：{exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorkflowError(f"顶层商品不能是符号链接：{path}")
        if stat.S_ISDIR(info.st_mode):
            products.append(path)
    return products


def _read_link_history(history_file: Path) -> dict:
    descriptor = None
    history_path = Path(history_file).expanduser()
    if not history_path.is_absolute():
        history_path = Path.cwd() / history_path
    expected = _real_regular_file(history_path, "链接选品历史")
    try:
        descriptor = os.open(
            history_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError("链接选品历史在打开时被替换")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            history = json.load(handle)
    except WorkflowError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"链接选品历史无效：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        not isinstance(history, dict)
        or type(history.get("version")) is not int
        or history["version"] != 1
        or not isinstance(history.get("products"), list)
    ):
        raise WorkflowError("链接选品历史格式或版本无效")
    return history


def _link_product_path(
    catalog: Path,
    entry: dict,
) -> tuple[str, str, Path] | None:
    if not isinstance(entry, dict):
        raise WorkflowError("链接选品历史商品记录必须是对象")
    product_name = entry.get("product_key")
    if not _valid_product_name(product_name):
        raise WorkflowError("链接选品历史包含非法商品货号")
    canonical_key = _canonical_product_key(product_name)
    recorded_canonical = entry.get("canonical_key")
    if recorded_canonical is not None and (
        not isinstance(recorded_canonical, str)
        or recorded_canonical != canonical_key
    ):
        raise WorkflowError(f"链接选品历史货号去重键不一致：{product_name}")
    if entry.get("status") != "miaoshou_collected":
        return None
    output_value = entry.get("output_dir")
    if not isinstance(output_value, str) or not output_value:
        raise WorkflowError(f"已采集商品缺少链接输出目录：{product_name}")
    output_path = Path(output_value).expanduser()
    if not output_path.is_absolute():
        raise WorkflowError(f"链接输出目录必须是绝对路径：{product_name}")

    date_dir = output_path.parent
    task_match = TASK_DIR_PATTERN.fullmatch(output_path.name)
    if (
        DATE_DIR_PATTERN.fullmatch(date_dir.name) is None
        or not _valid_iso_date(date_dir.name)
        or task_match is None
        or int(task_match.group(1)) < 1
        or task_match.group(2) != product_name
    ):
        raise WorkflowError(
            f"链接输出目录必须使用 链接/日期/NNN-货号 格式：{product_name}"
        )
    try:
        date_info = date_dir.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowError(f"无法检查链接日期目录：{date_dir}：{exc}") from exc
    if stat.S_ISLNK(date_info.st_mode):
        raise WorkflowError(f"链接日期目录不能是符号链接：{date_dir}")
    if not stat.S_ISDIR(date_info.st_mode):
        return None
    try:
        if date_dir.parent.resolve(strict=True) != catalog:
            raise WorkflowError(f"链接输出目录不在商品目录内：{output_path}")
    except OSError as exc:
        raise WorkflowError(f"无法解析链接输出目录：{output_path}：{exc}") from exc

    try:
        product_info = output_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowError(f"无法检查链接商品目录：{output_path}：{exc}") from exc
    if stat.S_ISLNK(product_info.st_mode):
        raise WorkflowError(f"链接商品目录不能是符号链接：{output_path}")
    if not stat.S_ISDIR(product_info.st_mode):
        return None
    for filename in LINK_PRODUCT_FILES:
        required = output_path / filename
        try:
            info = required.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkflowError(f"无法检查链接商品资料：{required}：{exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorkflowError(f"链接商品资料不能是符号链接：{required}")
        if not stat.S_ISREG(info.st_mode):
            return None
    return product_name, canonical_key, output_path


def discover_link_products(catalog: Path, history_file: Path) -> list[Path]:
    canonical_catalog = _canonical_existing_directory(catalog, "商品目录")
    history = _read_link_history(history_file)
    products = []
    seen = set()
    for entry in history["products"]:
        candidate = _link_product_path(canonical_catalog, entry)
        if candidate is None:
            continue
        product_name, canonical_key, output_path = candidate
        if canonical_key in seen:
            raise WorkflowError(f"链接选品历史包含重复货号：{product_name}")
        seen.add(canonical_key)
        products.append(output_path)
    return sorted(products, key=lambda path: (path.parent.name, path.name))


def _workflow_id(config: Config) -> str:
    state_file = config.state_file.expanduser()
    if not state_file.is_absolute():
        state_file = Path.cwd() / state_file
    try:
        canonical = state_file.parent.resolve(strict=False) / state_file.name
    except OSError as exc:
        raise WorkflowError(f"无法规范状态文件路径：{exc}") from exc
    return hashlib.sha256(os.fsencode(str(canonical))).hexdigest()


def _pending_name(workflow_id: str, target_name: str) -> str:
    digest = hashlib.sha256(target_name.encode("utf-8")).hexdigest()
    return f".swimwear-pending-{workflow_id}-{digest}.json"


def _parse_archive_target(target_name: str) -> tuple[str, str, str]:
    if not isinstance(target_name, str):
        raise WorkflowError("归档目标路径非法")
    parts = target_name.split("/")
    if len(parts) != 2 or any(not part or Path(part).name != part for part in parts):
        raise WorkflowError("归档目标路径必须是日期目录下的单个产品目录")
    date_name, task_name = parts
    task_match = TASK_DIR_PATTERN.fullmatch(task_name)
    if (
        DATE_DIR_PATTERN.fullmatch(date_name) is None
        or not _valid_iso_date(date_name)
        or task_match is None
        or int(task_match.group(1)) < 1
        or not _valid_product_name(task_match.group(2))
    ):
        raise WorkflowError("归档目标路径必须使用 已做/日期/NNN-货号 格式")
    return date_name, task_name, task_match.group(2)


def _read_json_at(parent_fd: int, name: str, label: str):
    descriptor = None
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
            raise WorkflowError(f"{label}必须是普通文件且不能是符号链接")
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if not _same_identity(opened, expected) or not stat.S_ISREG(opened.st_mode):
            raise WorkflowError(f"{label}在打开时被替换")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            return json.load(handle)
    except WorkflowError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"{label}无效：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_pending_record(
    record, pending_name: str, expected_workflow_id: str | None = None
) -> tuple[str, dict, str, tuple[int, int] | None, str]:
    if not isinstance(record, dict) or set(record) != {
        "version",
        "phase",
        "base_state_digest",
        "workflow_id",
        "target_name",
        "target_identity",
        "task",
    }:
        raise WorkflowError("pending 记录字段或身份非法")
    if type(record["version"]) is not int or record["version"] != 1:
        raise WorkflowError("pending 记录版本非法")
    target_name = record["target_name"]
    task = record["task"]
    workflow_id = record["workflow_id"]
    pending_match = PENDING_PATTERN.fullmatch(pending_name)
    if (
        pending_match is None
        or not isinstance(workflow_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", workflow_id) is None
        or pending_match.group(1) != workflow_id
        or (
            expected_workflow_id is not None
            and workflow_id != expected_workflow_id
        )
    ):
        raise WorkflowError("pending 记录 workflow 身份非法或不匹配")
    if (
        not isinstance(target_name, str)
        or _pending_name(workflow_id, target_name) != pending_name
    ):
        raise WorkflowError("pending 记录名称与目标不一致")
    try:
        date_name, task_name, product_name = _parse_archive_target(target_name)
    except WorkflowError as exc:
        raise WorkflowError("pending 记录目标目录名非法") from exc
    if (
        not isinstance(task, dict)
        or not TASK_FIELDS.issubset(task)
        or not set(task).issubset(TASK_FIELDS | OPTIONAL_TASK_FIELDS)
    ):
        raise WorkflowError("pending 记录任务字段非法")
    if "image_attempts" in task:
        _validate_image_attempts(task["image_attempts"], task.get("image_mode", "api-first"))
    if "video_attempts" in task:
        _validate_video_attempts(task["video_attempts"])
    if task.get("product_name") != product_name:
        raise WorkflowError("pending 记录产品名与目标不一致")
    task_dir = Path(task.get("task_dir", ""))
    if (
        not task_dir.is_absolute()
        or task_dir.parent.name != date_name
        or task_dir.name != task_name
    ):
        raise WorkflowError("pending 记录任务路径与目标不一致")
    base_state_digest = record["base_state_digest"]
    if (
        not isinstance(base_state_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", base_state_digest) is None
    ):
        raise WorkflowError("pending 记录 base state digest 非法")
    phase = record["phase"]
    identity_value = record["target_identity"]
    if phase == "prepared":
        if identity_value is not None:
            raise WorkflowError("prepared pending 不能包含目标身份")
        identity = None
    elif phase == "published":
        if not isinstance(identity_value, dict) or set(identity_value) != {"dev", "ino"}:
            raise WorkflowError("published pending 缺少目标身份")
        dev = identity_value["dev"]
        ino = identity_value["ino"]
        if (
            type(dev) is not int
            or type(ino) is not int
            or dev < 0
            or ino <= 0
        ):
            raise WorkflowError("published pending 目标身份非法")
        identity = (dev, ino)
    else:
        raise WorkflowError("pending 发布阶段非法")
    return target_name, task, phase, identity, base_state_digest


def completed_product_names(done_root: Path) -> set[str]:
    done_fd = None
    completed = set()
    try:
        done_fd, _ = _open_pinned_directory(done_root, "已做目录")
        with os.scandir(done_fd) as entries:
            names = sorted(entry.name for entry in entries)
        pending_targets = set()
        for name in names:
            if PENDING_PATTERN.fullmatch(name):
                record = _read_json_at(done_fd, name, "pending 记录")
                target_name, _, _, _, _ = _validate_pending_record(record, name)
                pending_targets.add(target_name)
        for date_name in names:
            if (
                DATE_DIR_PATTERN.fullmatch(date_name) is None
                or not _valid_iso_date(date_name)
            ):
                continue
            date_info = os.stat(date_name, dir_fd=done_fd, follow_symlinks=False)
            if stat.S_ISLNK(date_info.st_mode):
                raise WorkflowError(f"日期目录不能是符号链接：{date_name}")
            if not stat.S_ISDIR(date_info.st_mode):
                continue
            date_fd, _ = _open_pinned_directory_at(done_fd, date_name, "日期目录")
            try:
                with os.scandir(date_fd) as entries:
                    task_names = sorted(entry.name for entry in entries)
                for task_name in task_names:
                    match = TASK_DIR_PATTERN.fullmatch(task_name)
                    if (
                        match is None
                        or int(match.group(1)) < 1
                        or not _valid_product_name(match.group(2))
                    ):
                        continue
                    task_info = os.stat(
                        task_name, dir_fd=date_fd, follow_symlinks=False
                    )
                    if stat.S_ISLNK(task_info.st_mode):
                        raise WorkflowError(
                            f"产品归档目录不能是符号链接：{date_name}/{task_name}"
                        )
                    relative_target = f"{date_name}/{task_name}"
                    if (
                        relative_target not in pending_targets
                        and stat.S_ISDIR(task_info.st_mode)
                    ):
                        completed.add(match.group(2))
            finally:
                os.close(date_fd)
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法扫描已做目录：{exc}") from exc
    finally:
        if done_fd is not None:
            os.close(done_fd)
    return completed


def _path_from_state(value, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise WorkflowError(f"状态字段 {label} 必须是非空绝对路径")
    path = Path(value)
    if not path.is_absolute():
        raise WorkflowError(f"状态字段 {label} 必须是绝对路径")
    return path


def _resolved_without_requirement(path: Path, label: str) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise WorkflowError(f"无法解析状态字段 {label}：{exc}") from exc


def _validate_timestamp(value, label: str) -> None:
    if not isinstance(value, str):
        raise WorkflowError(f"状态字段 {label} 必须是时间字符串")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowError(f"状态字段 {label} 不是合法时间") from exc


def _validate_contained_state_path(value, root: Path, label: str) -> None:
    path = _path_from_state(value, label)
    resolved = _resolved_without_requirement(path, label)
    if not resolved.is_relative_to(root):
        raise WorkflowError(f"活动任务 {label} 越界")


def _validate_image_attempts(value, image_mode="api-first") -> None:
    if image_mode not in {"api-first", "website"}:
        raise WorkflowError("生图模式非法")
    if not isinstance(value, dict):
        raise WorkflowError("活动任务 image_attempts 必须是对象")
    for index, attempts in value.items():
        if index not in {"1", "2", "3"}:
            raise WorkflowError("活动任务 image_attempts 的索引非法")
        if not isinstance(attempts, dict) or set(attempts) != set(
            IMAGE_ATTEMPT_LIMITS
        ):
            raise WorkflowError("活动任务 image_attempts 的计数结构非法")
        for surface, limit in IMAGE_ATTEMPT_LIMITS.items():
            count = attempts[surface]
            if type(count) is not int or not 0 <= count <= limit:
                raise WorkflowError(
                    f"活动任务 image_attempts[{index}].{surface} 超出范围"
                )
        if image_mode == "api-first" and attempts["website"] > 0 and attempts["api"] < IMAGE_ATTEMPT_LIMITS["api"]:
            raise WorkflowError("网页生图尝试不能早于接口重试完成")


def _validate_video_attempts(value) -> None:
    if not isinstance(value, dict):
        raise WorkflowError("活动任务 video_attempts 必须是对象")
    for index, count in value.items():
        if index not in {"1", "2"}:
            raise WorkflowError("活动任务 video_attempts 的索引非法")
        if type(count) is not int or not 0 <= count <= VIDEO_ATTEMPT_LIMIT:
            raise WorkflowError(f"活动任务 video_attempts[{index}] 超出范围")


def _validate_active_task(config: Config, task) -> None:
    if (
        not isinstance(task, dict)
        or not TASK_FIELDS.issubset(task)
        or not set(task).issubset(TASK_FIELDS | OPTIONAL_TASK_FIELDS)
    ):
        raise WorkflowError("活动任务字段不完整或包含未知字段")
    product_name = task["product_name"]
    if not _valid_product_name(product_name):
        raise WorkflowError("活动任务产品名非法")
    if task["stage"] not in STAGES:
        raise WorkflowError("活动任务阶段非法")

    catalog, done_root = _validate_root_relationship(config)
    source_dir = _path_from_state(task["source_dir"], "source_dir")
    task_dir = _path_from_state(task["task_dir"], "task_dir")
    image_dir = _path_from_state(task["image_dir"], "image_dir")
    video_dir = _path_from_state(task["video_dir"], "video_dir")
    resolved_source = _resolved_without_requirement(source_dir, "source_dir")
    resolved_task = _resolved_without_requirement(task_dir, "task_dir")
    resolved_image = _resolved_without_requirement(image_dir, "image_dir")
    resolved_video = _resolved_without_requirement(video_dir, "video_dir")

    source_matches_catalog = resolved_source == catalog / product_name
    if resolved_source.parent == catalog:
        source_matches_catalog = _direct_product_name(resolved_source) == product_name
    if resolved_source.is_relative_to(catalog):
        relative_source = resolved_source.relative_to(catalog)
        if len(relative_source.parts) == 2:
            source_date, source_task = relative_source.parts
            source_match = TASK_DIR_PATTERN.fullmatch(source_task)
            source_matches_catalog = (
                DATE_DIR_PATTERN.fullmatch(source_date) is not None
                and _valid_iso_date(source_date)
                and source_match is not None
                and int(source_match.group(1)) >= 1
                and source_match.group(2) == product_name
            )
    if config.source_product is not None:
        source_matches_catalog = (
            resolved_source == config.source_product.expanduser().resolve(strict=True)
            and _direct_product_name(resolved_source) == product_name
        )
    legacy_source = (
        LEGACY_CATALOG.expanduser().resolve(strict=False) / product_name
    )
    if not source_matches_catalog and resolved_source != legacy_source:
        raise WorkflowError("活动任务 source_dir 越界或与产品名不一致")
    if resolved_task.parent.parent != done_root:
        raise WorkflowError("活动任务 task_dir 不在已做目录内")
    date_name = resolved_task.parent.name
    match = TASK_DIR_PATTERN.fullmatch(resolved_task.name)
    if (
        DATE_DIR_PATTERN.fullmatch(date_name) is None
        or not _valid_iso_date(date_name)
        or match is None
        or int(match.group(1)) < 1
        or match.group(2) != product_name
    ):
        raise WorkflowError("活动任务目录必须使用 已做/日期/NNN-货号 格式")
    if resolved_image != resolved_task / "图片制作":
        raise WorkflowError("活动任务 image_dir 不是规范子目录")
    if resolved_video != resolved_task / "视频制作":
        raise WorkflowError("活动任务 video_dir 不是规范子目录")

    for path, label in (
        (source_dir, "source_dir"),
        (task_dir.parent, "date_dir"),
        (task_dir, "task_dir"),
        (image_dir, "image_dir"),
        (video_dir, "video_dir"),
    ):
        _assert_real_directory(path, f"状态字段 {label}")

    if task["reference"] is not None:
        reference = _path_from_state(task["reference"], "reference")
        resolved_reference = _resolved_without_requirement(reference, "reference")
        if not resolved_reference.is_relative_to(resolved_image):
            raise WorkflowError("活动任务 reference 越界")
    if not isinstance(task["image_baseline"], dict):
        raise WorkflowError("活动任务 image_baseline 必须是对象")
    if "image_attempts" in task:
        _validate_image_attempts(task["image_attempts"], task.get("image_mode", "api-first"))
    if "video_attempts" in task:
        _validate_video_attempts(task["video_attempts"])
    if not isinstance(task["video_baseline"], dict):
        raise WorkflowError("活动任务 video_baseline 必须是对象")
    if not isinstance(task["frames"], dict):
        raise WorkflowError("活动任务 frames 必须是对象")
    for key, value in task["frames"].items():
        if not isinstance(key, str):
            raise WorkflowError("活动任务 frames 的索引必须是字符串")
        _validate_contained_state_path(value, resolved_image, f"frames[{key}]")
    if not isinstance(task["videos"], list):
        raise WorkflowError("活动任务 videos 必须是数组")
    for index, value in enumerate(task["videos"]):
        _validate_contained_state_path(value, resolved_video, f"videos[{index}]")
    if task["merge"] is not None and not isinstance(task["merge"], (str, dict)):
        raise WorkflowError("活动任务 merge 类型非法")
    if isinstance(task["merge"], str):
        _validate_contained_state_path(task["merge"], resolved_video, "merge")
    elif isinstance(task["merge"], dict) and "output" in task["merge"]:
        _validate_contained_state_path(
            task["merge"]["output"], resolved_video, "merge.output"
        )
    _validate_timestamp(task["created_at"], "created_at")
    _validate_timestamp(task["updated_at"], "updated_at")


def _validate_state(config: Config, state_value) -> None:
    if not isinstance(state_value, dict) or set(state_value) != {"version", "active_task"}:
        raise WorkflowError("状态顶层字段非法")
    if type(state_value["version"]) is not int or state_value["version"] != 1:
        raise WorkflowError("不支持的状态版本")
    active = state_value["active_task"]
    if active is not None:
        _validate_active_task(config, active)


def load_state(config: Config) -> dict:
    state_file = config.state_file
    parent_fd = state_fd = None
    try:
        state_file.parent.lstat()
    except FileNotFoundError:
        return default_state()
    except OSError as exc:
        raise WorkflowError(f"无法访问状态目录：{exc}") from exc
    parent_fd, _ = _open_pinned_directory(state_file.parent, "状态目录")
    try:
        try:
            expected = os.stat(
                state_file.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return default_state()
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
            raise WorkflowError("状态文件必须是普通文件且不能是符号链接")
        state_fd = os.open(
            state_file.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(state_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise WorkflowError("状态文件在打开时被替换")
        with os.fdopen(state_fd, "r", encoding="utf-8") as handle:
            state_fd = None
            state_value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"状态文件无效：{exc}") from exc
    finally:
        if state_fd is not None:
            os.close(state_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    _validate_state(config, state_value)
    return state_value


def _fsync_directory(path: Path) -> None:
    descriptor = None
    try:
        descriptor = os.open(path, _directory_flags())
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _real_regular_file(path: Path, label: str) -> os.stat_result:
    info = _lstat(path, label)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkflowError(f"{label}必须是真实普通文件且不能是符号链接：{path}")
    return info


def _load_clean_image(source: Path, allowed_roots: tuple[Path, ...]) -> Image.Image:
    descriptor = None
    try:
        source_path = Path(source)
    except TypeError as exc:
        raise WorkflowError("图片源路径非法") from exc
    try:
        canonical_source = source_path.resolve(strict=True)
        canonical_roots = tuple(root.resolve(strict=True) for root in allowed_roots)
    except (OSError, RuntimeError) as exc:
        raise WorkflowError(f"无法解析图片源路径：{exc}") from exc
    if not any(canonical_source.is_relative_to(root) for root in canonical_roots):
        raise WorkflowError("图片源必须位于所选商品目录或任务目录内")

    expected = _real_regular_file(source_path, "图片源")
    try:
        descriptor = os.open(
            source_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError("图片源在打开时被替换")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(handle) as image:
                    image.load()
                    mode = "RGBA" if "A" in image.getbands() else "RGB"
                    converted = image.convert(mode)
                    clean = Image.new(mode, converted.size)
                    clean.paste(converted)
        current = source_path.lstat()
        final_opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or not _same_identity(current, expected)
            or not _same_identity(final_opened, expected)
            or current.st_size != expected.st_size
            or current.st_mtime_ns != expected.st_mtime_ns
            or final_opened.st_size != expected.st_size
            or final_opened.st_mtime_ns != expected.st_mtime_ns
        ):
            raise WorkflowError("图片源在读取期间发生变化")
        return clean
    except WorkflowError:
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise WorkflowError(f"图片源损坏或无法完整解码：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_crop(crop, image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    if crop is None:
        return None
    if (
        not isinstance(crop, tuple)
        or len(crop) != 4
        or any(type(value) is not int for value in crop)
    ):
        raise WorkflowError("裁剪参数必须是四个整数")
    x, y, width, height = crop
    image_width, image_height = image_size
    if (
        x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > image_width
        or y + height > image_height
    ):
        raise WorkflowError("裁剪区域必须严格位于原图边界内且尺寸为正")
    return x, y, width, height


def _write_png_temp(image_dir: Path, destination_name: str, image: Image.Image) -> Path:
    temporary_name = None
    completed = False
    try:
        _assert_real_directory(image_dir, "图片目录")
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=image_dir,
            prefix=f".{destination_name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            image.save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        completed = True
        return Path(temporary_name)
    except WorkflowError:
        raise
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"无法原子写入 PNG 图片：{exc}") from exc
    finally:
        if temporary_name is not None and not completed:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _state_was_committed(config: Config, expected_state: dict | None) -> bool:
    if expected_state is None:
        return False
    try:
        return load_state(config) == expected_state
    except (WorkflowError, OSError):
        return False


def _publish_png_with_state(
    config: Config,
    state_value: dict,
    temporary: Path,
    destination: Path,
    *,
    allow_replace: bool = True,
) -> None:
    backup = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.backup")
    backup_created = False
    published = False
    preserve_backup = False
    parent_fd = temporary_fd = None
    try:
        temporary_info = _real_regular_file(temporary, "临时规范图片")
        temporary_fd = os.open(
            temporary,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_temporary = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(opened_temporary.st_mode)
            or not _same_identity(opened_temporary, temporary_info)
        ):
            raise WorkflowError("临时规范图片在发布前被替换")
        published_identity = (opened_temporary.st_dev, opened_temporary.st_ino)
        if allow_replace:
            if destination.exists() or destination.is_symlink():
                _real_regular_file(destination, "规范图片")
                os.link(destination, backup, follow_symlinks=False)
                backup_created = True
            os.replace(temporary, destination)
            published = True
        else:
            if destination.exists() or destination.is_symlink():
                raise WorkflowError(f"规范图片已存在，拒绝覆盖：{destination.name}")
            parent_fd, _ = _open_pinned_directory(destination.parent, "图片目录")
            renamed = _rename_noreplace(
                parent_fd,
                temporary.name,
                parent_fd,
                destination.name,
            )
            if not renamed:
                raise WorkflowError(f"规范图片已存在，拒绝覆盖：{destination.name}")
            published = True
        published_info = destination.lstat()
        if (
            stat.S_ISLNK(published_info.st_mode)
            or not stat.S_ISREG(published_info.st_mode)
            or (published_info.st_dev, published_info.st_ino) != published_identity
        ):
            raise WorkflowError("规范图片发布后身份不匹配")
        _fsync_directory(destination.parent)
        save_state_atomic(config, state_value)
    except BaseException as exc:
        state_committed = _state_was_committed(config, state_value)
        if published and not state_committed:
            try:
                current = destination.lstat()
                if (current.st_dev, current.st_ino) != published_identity:
                    raise WorkflowError("规范图片在状态回滚前被替换")
                if backup_created:
                    os.rename(backup, destination)
                    backup_created = False
                else:
                    destination.unlink()
                _fsync_directory(destination.parent)
            except (OSError, WorkflowError) as rollback_exc:
                preserve_backup = True
                raise WorkflowError(f"状态保存失败且规范图片回滚失败：{rollback_exc}") from exc
        if isinstance(exc, WorkflowError):
            raise
        if isinstance(exc, OSError):
            raise WorkflowError(f"无法发布规范图片：{exc}") from exc
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if backup_created and not preserve_backup:
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
        if temporary_fd is not None:
            os.close(temporary_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def register_reference(config: Config, source, crop=None) -> dict:
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] not in REFERENCE_ALLOWED_STAGES:
            raise WorkflowError("当前阶段不能登记参考图")
        clean = _load_clean_image(
            Path(source),
            (Path(task["source_dir"]), Path(task["task_dir"])),
        )
        valid_crop = _validate_crop(crop, clean.size)
        if valid_crop is not None:
            x, y, width, height = valid_crop
            clean = clean.crop((x, y, x + width, y + height))
        image_dir = Path(task["image_dir"])
        destination = image_dir / REFERENCE_FILE
        temporary = _write_png_temp(image_dir, destination.name, clean)
        task["reference"] = str(destination.resolve(strict=False))
        task["stage"] = REFERENCE_NEXT_STAGE
        task["updated_at"] = _timestamp_now()
        _publish_png_with_state(config, state_value, temporary, destination)
        return task


def _validate_image_index(index) -> int:
    if type(index) is not int or index not in IMAGE_STAGE_FLOW:
        raise WorkflowError("图片索引只能是 1、2 或 3")
    return index


def _validate_canonical_image(task: dict, state_key: str, index: int | None) -> None:
    image_dir = Path(task["image_dir"])
    if state_key == "reference":
        expected = image_dir / REFERENCE_FILE
        state_path = task["reference"]
        label = "参考图"
    else:
        if index is None:
            raise WorkflowError("规范帧索引缺失")
        expected = image_dir / FRAME_FILES[index]
        state_path = task["frames"].get(str(index))
        label = f"第 {index} 张规范帧"
    canonical = expected.resolve(strict=False)
    if state_path != str(canonical):
        raise WorkflowError(f"{label}状态路径不是规范路径")
    _load_clean_image(expected, (image_dir,))


def _read_prompt_text(
    prompt: Path,
    label: str = "图片提示词",
    *,
    return_identity: bool = False,
) -> str | tuple[str, tuple[int, int]]:
    descriptor = None
    expected = _real_regular_file(prompt, label)
    try:
        descriptor = os.open(
            prompt,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError(f"{label}在打开时被替换")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        current = prompt.lstat()
        final_opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or not _same_identity(current, expected)
            or not _same_identity(final_opened, expected)
            or current.st_size != expected.st_size
            or current.st_mtime_ns != expected.st_mtime_ns
            or final_opened.st_size != expected.st_size
            or final_opened.st_mtime_ns != expected.st_mtime_ns
        ):
            raise WorkflowError(f"{label}在读取期间发生变化")
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowError(f"{label}必须是 UTF-8 文本") from exc
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法读取{label}：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not text.strip():
        raise WorkflowError(f"{label}不能为空")
    lowered = text.casefold()
    for forbidden in FORBIDDEN_PROMPT_TEXT:
        if forbidden.casefold() in lowered:
            raise WorkflowError(f"{label}不能包含模型名称：{forbidden}")
    if return_identity:
        return text, (final_opened.st_dev, final_opened.st_ino)
    return text


def _validate_video_prompt_content(text: str, label: str) -> None:
    requirements = {
        "明确的英语口播": ("英语口播", "英文口播"),
        "人物嘴型同步": ("嘴型同步", "嘴型、呼吸", "嘴型与"),
        "视觉爆点或钩子": ("视觉爆点", "视觉钩子", "近距离钩子"),
        "至少三分之二的口播覆盖要求": (
            "有效口播至少覆盖三分之二",
            "口播至少覆盖三分之二",
            "口播占比至少三分之二",
        ),
    }
    for requirement, markers in requirements.items():
        if not any(marker in text for marker in markers):
            raise WorkflowError(f"{label}缺少{requirement}")


def _image_baseline(image_dir: Path) -> dict:
    excluded = {REFERENCE_FILE, *FRAME_FILES.values()}
    baseline = {}
    try:
        _assert_real_directory(image_dir, "图片目录")
        entries = sorted(image_dir.iterdir(), key=lambda path: path.name)
        for path in entries:
            if path.name in excluded or path.suffix.casefold() not in IMAGE_DOWNLOAD_EXTENSIONS:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise WorkflowError(f"下载候选不能是符号链接：{path.name}")
            if not stat.S_ISREG(info.st_mode):
                raise WorkflowError(f"下载候选必须是普通文件：{path.name}")
            baseline[path.name] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法扫描图片下载候选：{exc}") from exc
    return baseline


def mark_image_prompt(config: Config, index) -> dict:
    valid_index = _validate_image_index(index)
    _validate_root_relationship(config)
    predecessor, awaiting, _ = IMAGE_STAGE_FLOW[valid_index]
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != predecessor:
            raise WorkflowError(f"当前阶段不能标记第 {valid_index} 张图片提示词")
        _validate_canonical_image(task, "reference", None)
        for previous in range(1, valid_index):
            _validate_canonical_image(task, "frames", previous)
        prompt = Path(task["image_dir"]) / IMAGE_PROMPTS[valid_index]
        _read_prompt_text(prompt)
        task["image_baseline"] = _image_baseline(Path(task["image_dir"]))
        task["stage"] = awaiting
        task["updated_at"] = _timestamp_now()
        save_state_atomic(config, state_value)
        return task


def claim_image_attempt(config: Config, index, surface: str) -> dict:
    valid_index = _validate_image_index(index)
    if surface not in IMAGE_ATTEMPT_LIMITS:
        raise WorkflowError("生图入口必须是 api 或 website")
    _validate_root_relationship(config)
    _, awaiting, _ = IMAGE_STAGE_FLOW[valid_index]
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != awaiting:
            raise WorkflowError(f"当前阶段不能登记第 {valid_index} 张图片生图尝试")

        attempts_by_image = task.setdefault("image_attempts", {})
        attempts = attempts_by_image.setdefault(
            str(valid_index), {"api": 0, "website": 0}
        )
        image_mode = task.get("image_mode", "api-first")
        _validate_image_attempts(attempts_by_image, image_mode)
        api_limit = 0 if image_mode == "website" else IMAGE_ATTEMPT_LIMITS["api"]
        website_limit = IMAGE_ATTEMPT_LIMITS["website"]

        if surface == "api":
            if image_mode == "website":
                raise WorkflowError("当前任务为网页生图模式，不调用接口")
            if attempts["website"] > 0:
                raise WorkflowError("已进入网页生图阶段，不能退回接口生图")
            if attempts["api"] >= api_limit:
                raise WorkflowError("接口生图 2 次机会已用尽，请切换网页生图")
        else:
            if attempts["api"] < api_limit:
                raise WorkflowError("必须先完成 2 次接口生图尝试，再使用网页生图")
            if attempts["website"] >= website_limit:
                raise WorkflowError("网页生图 3 次机会已用尽，请改为用户手工生成")

        attempts[surface] += 1
        if attempts["api"] < api_limit:
            next_on_failure = "api"
        elif attempts["website"] < website_limit:
            next_on_failure = "website"
        else:
            next_on_failure = "manual"
        task["updated_at"] = _timestamp_now()
        save_state_atomic(config, state_value)
        return {
            "active_task": task,
            "claimed": {
                "index": valid_index,
                "surface": surface,
                "attempt": attempts[surface],
                "limit": IMAGE_ATTEMPT_LIMITS[surface],
            },
            "next_on_failure": next_on_failure,
        }


def _video_baseline(video_dir: Path) -> dict:
    baseline = {}
    temporary_markers = (".tmp", ".temp", ".part", ".partial", ".crdownload")
    try:
        _assert_real_directory(video_dir, "视频目录")
        entries = sorted(video_dir.iterdir(), key=lambda path: path.name)
        for path in entries:
            name = path.name
            lowered = name.casefold()
            if (
                name.startswith(".")
                or path.suffix.casefold() not in VIDEO_SUFFIXES
                or lowered == "final.mp4"
                or lowered.startswith(("01-segment.", "02-segment."))
                or any(marker in lowered[: -len(path.suffix)] for marker in temporary_markers)
            ):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise WorkflowError(f"视频下载候选不能是符号链接：{name}")
            if not stat.S_ISREG(info.st_mode):
                raise WorkflowError(f"视频下载候选必须是普通文件：{name}")
            baseline[name] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法扫描视频下载候选：{exc}") from exc
    return baseline


def _next_video_redo_archive(video_dir: Path) -> tuple[Path, Path]:
    archive_root = video_dir / VIDEO_REDO_ARCHIVE_DIR
    try:
        info = archive_root.lstat()
    except FileNotFoundError:
        archive_root.mkdir(mode=0o700)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkflowError("视频重做归档必须是真实目录")

    indexes = []
    for entry in archive_root.iterdir():
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise WorkflowError("视频重做归档不能包含符号链接")
        if stat.S_ISDIR(info.st_mode) and re.fullmatch(r"[0-9]{3}", entry.name):
            indexes.append(int(entry.name))
    next_index = max(indexes, default=0) + 1
    if next_index > 999:
        raise WorkflowError("视频重做归档编号已用尽")
    return archive_root, archive_root / f"{next_index:03d}"


def redo_videos(config: Config) -> dict:
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] not in {"merged", "awaiting-videos"}:
            raise WorkflowError("只有已完成或待生成视频的任务才能重做同一产品视频")
        if task["stage"] == "merged":
            task = _validate_idempotent_merge(config, task)
        _validate_canonical_image(task, "reference", None)
        for frame_index in FRAME_FILES:
            _validate_canonical_image(task, "frames", frame_index)

        video_dir = Path(task["video_dir"])
        _assert_real_directory(video_dir, "视频目录")
        archive_root, archive_dir = _next_video_redo_archive(video_dir)
        temporary = Path(
            tempfile.mkdtemp(prefix=".pending-", dir=archive_root)
        )
        copied = []
        try:
            for source in sorted(video_dir.iterdir(), key=lambda path: path.name):
                if source.name.startswith(".") or source.name == VIDEO_REDO_ARCHIVE_DIR:
                    continue
                info = source.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise WorkflowError("视频目录不能包含符号链接文件")
                if not stat.S_ISREG(info.st_mode):
                    continue
                if source.suffix.casefold() not in VIDEO_SUFFIXES | {".txt"}:
                    continue
                destination = temporary / source.name
                shutil.copy2(source, destination, follow_symlinks=False)
                copied.append(source.name)
            if not copied:
                raise WorkflowError("当前任务没有可归档的视频版本")
            temporary.rename(archive_dir)
            temporary = None

            task["video_baseline"] = {}
            task["video_attempts"] = {}
            task["videos"] = []
            task["merge"] = None
            task["stage"] = VIDEO_PROMPT_STAGE[0]
            task["updated_at"] = _timestamp_now()
            try:
                save_state_atomic(config, state_value)
            except Exception:
                shutil.rmtree(archive_dir, ignore_errors=True)
                raise
            return {
                "active_task": task,
                "archived_video_version": str(archive_dir.resolve(strict=True)),
                "archived_files": copied,
            }
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)


def mark_video_prompts(config: Config) -> dict:
    _validate_root_relationship(config)
    predecessor, awaiting = VIDEO_PROMPT_STAGE
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != predecessor:
            raise WorkflowError("当前阶段不能标记视频提示词")
        _validate_canonical_image(task, "reference", None)
        for index in FRAME_FILES:
            _validate_canonical_image(task, "frames", index)
        video_dir = Path(task["video_dir"])
        prompt_identities = []
        for prompt_name in VIDEO_PROMPTS:
            text, identity = _read_prompt_text(
                video_dir / prompt_name,
                "视频提示词",
                return_identity=True,
            )
            _validate_video_prompt_content(text, prompt_name)
            prompt_identities.append(identity)
        if len(set(prompt_identities)) != len(prompt_identities):
            raise WorkflowError("两个视频提示词必须是相互独立的普通文件")
        task["video_baseline"] = _video_baseline(video_dir)
        task["stage"] = awaiting
        task["updated_at"] = _timestamp_now()
        save_state_atomic(config, state_value)
        return task


def claim_video_attempt(config: Config, index) -> dict:
    if type(index) is not int or index not in (1, 2):
        raise WorkflowError("视频片段索引只能是 1 或 2")
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != VIDEO_INGEST_STAGE[0]:
            raise WorkflowError(f"当前阶段不能登记第 {index} 段视频生成尝试")
        if task["videos"] or task["merge"] is not None:
            raise WorkflowError("当前任务已有规范视频或合并结果，不能登记生成尝试")
        _validate_canonical_image(task, "reference", None)
        for frame_index in FRAME_FILES:
            _validate_canonical_image(task, "frames", frame_index)
        prompt_name = VIDEO_PROMPTS[index - 1]
        prompt_text = _read_prompt_text(
            Path(task["video_dir"]) / prompt_name,
            "视频提示词",
        )
        _validate_video_prompt_content(prompt_text, prompt_name)

        attempts = task.setdefault("video_attempts", {})
        _validate_video_attempts(attempts)
        current = attempts.get(str(index), 0)
        if current >= VIDEO_ATTEMPT_LIMIT:
            raise WorkflowError(
                f"第 {index} 段视频 {VIDEO_ATTEMPT_LIMIT} 次自动生成机会已用尽"
            )
        current += 1
        attempts[str(index)] = current
        task["updated_at"] = _timestamp_now()
        save_state_atomic(config, state_value)
        return {
            "active_task": task,
            "claimed": {
                "index": index,
                "attempt": current,
                "limit": VIDEO_ATTEMPT_LIMIT,
                "surface": "toapi-video-web",
            },
            "next_on_failure": (
                "website" if current < VIDEO_ATTEMPT_LIMIT else "manual"
            ),
        }


def _positive_rate(value) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowError("媒体视频帧率字段无效")
    try:
        numerator_text, denominator_text = value.split("/", 1)
        numerator = float(numerator_text)
        denominator = float(denominator_text)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("媒体视频帧率字段无效") from exc
    if (
        not math.isfinite(numerator)
        or not math.isfinite(denominator)
        or numerator <= 0
        or denominator <= 0
    ):
        raise WorkflowError("媒体视频帧率字段无效")
    return value


def _positive_duration_or_none(value) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def probe_media(config: Config, path) -> dict:
    media_path = Path(path)
    expected = _real_regular_file(media_path, "媒体文件")
    command = [
        config.ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, UnicodeError) as exc:
        raise WorkflowError(f"无法运行媒体探测工具：{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"退出码 {completed.returncode}"
        raise WorkflowError(f"媒体探测失败：{detail}")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkflowError("媒体探测结果不是有效 JSON") from exc
    try:
        streams = payload["streams"]
    except (KeyError, TypeError) as exc:
        raise WorkflowError("媒体探测结果缺少流信息") from exc
    if not isinstance(streams, list):
        raise WorkflowError("媒体探测结果包含非法流信息")
    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise WorkflowError("媒体文件缺少视频流")
    duration = _positive_duration_or_none(video_stream.get("duration"))
    if duration is None:
        try:
            format_duration = payload["format"]["duration"]
        except (KeyError, TypeError) as exc:
            raise WorkflowError("媒体探测结果缺少有效视频时长") from exc
        duration = _positive_duration_or_none(format_duration)
    if duration is None:
        raise WorkflowError("媒体探测结果包含非法视频时长")
    try:
        codec_name = video_stream["codec_name"]
        width = video_stream["width"]
        height = video_stream["height"]
        pix_fmt = video_stream["pix_fmt"]
        frame_rate = _positive_rate(video_stream["r_frame_rate"])
    except (KeyError, TypeError) as exc:
        raise WorkflowError("媒体视频流字段不完整") from exc
    if (
        not isinstance(codec_name, str)
        or not codec_name
        or type(width) is not int
        or width <= 0
        or type(height) is not int
        or height <= 0
        or not isinstance(pix_fmt, str)
        or not pix_fmt
    ):
        raise WorkflowError("媒体视频流字段无效")
    video = {
        "codec_name": codec_name,
        "width": width,
        "height": height,
        "pix_fmt": pix_fmt,
        "r_frame_rate": frame_rate,
    }

    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    audio = None
    if audio_stream is not None:
        try:
            audio_codec = audio_stream["codec_name"]
            sample_rate = int(audio_stream["sample_rate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("媒体音频流字段不完整") from exc
        if not isinstance(audio_codec, str) or not audio_codec or sample_rate <= 0:
            raise WorkflowError("媒体音频流字段无效")
        audio = {"codec_name": audio_codec, "sample_rate": sample_rate}
        channel_layout = audio_stream.get("channel_layout")
        if isinstance(channel_layout, str) and channel_layout:
            audio["channel_layout"] = channel_layout
        else:
            channels = audio_stream.get("channels")
            if type(channels) is not int or channels <= 0:
                raise WorkflowError("媒体音频流缺少有效声道结构")
            audio["channels"] = channels

    try:
        current = media_path.lstat()
    except OSError as exc:
        raise WorkflowError(f"媒体文件在探测后不可用：{exc}") from exc
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or not _same_identity(current, expected)
        or current.st_size != expected.st_size
        or current.st_mtime_ns != expected.st_mtime_ns
    ):
        raise WorkflowError("媒体文件在探测期间发生变化")
    return {"duration": duration, "video": video, "audio": audio}


def media_compatible(first: dict, second: dict) -> bool:
    video_fields = ("codec_name", "width", "height", "pix_fmt", "r_frame_rate")
    try:
        return all(first["video"][field] == second["video"][field] for field in video_fields) and (
            first["audio"] == second["audio"]
        )
    except (KeyError, TypeError):
        return False


def _copy_passthrough_compatible(first: dict, second: dict) -> bool:
    if not media_compatible(first, second):
        return False
    video = first["video"]
    audio = first["audio"]
    return video["codec_name"] == "h264" and (
        audio is None or audio["codec_name"] == "aac"
    )


def _changed_video_candidates(video_dir: Path, baseline: dict) -> list[tuple[Path, dict]]:
    current = _video_baseline(video_dir)
    return [
        (video_dir / name, snapshot)
        for name, snapshot in current.items()
        if baseline.get(name) != snapshot
    ]


def _after_video_candidate_open(path: Path) -> None:
    return None


def _before_video_candidate_rename(index: int, source: Path, destination: Path) -> None:
    return None


def _after_video_candidate_renamed(index: int, source: Path, destination: Path) -> None:
    return None


def _inspect_video_candidate(
    config: Config, path: Path, snapshot: dict
) -> tuple[os.stat_result, dict]:
    descriptor = None
    expected = _real_regular_file(path, "视频下载候选")
    if not _snapshot_matches(expected, snapshot):
        raise WorkflowError("视频下载候选在扫描后发生变化")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError("视频下载候选在打开时被替换")
        _after_video_candidate_open(path)
        media = probe_media(config, path)
        current = path.lstat()
        final_opened = os.fstat(descriptor)
        for info in (current, final_opened):
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not _same_identity(info, expected)
                or info.st_size != expected.st_size
                or info.st_mtime_ns != expected.st_mtime_ns
            ):
                raise WorkflowError("视频下载候选在读取期间发生变化")
        return expected, media
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法读取视频下载候选：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _entry_exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _rollback_ingested_videos(parent_fd: int | None, renamed: list[tuple]) -> list[BaseException]:
    rollback_errors = []
    for source, destination, consumed in reversed(renamed):
        try:
            current = os.stat(
                destination.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or not _same_identity(current, consumed)
                or current.st_size != consumed.st_size
                or current.st_mtime_ns != consumed.st_mtime_ns
            ):
                raise WorkflowError("规范视频在事务回滚前被替换")
            if not _rename_noreplace(
                parent_fd,
                destination.name,
                parent_fd,
                source.name,
            ):
                raise WorkflowError("原视频下载名在事务回滚时已被占用")
        except (OSError, WorkflowError) as current_error:
            rollback_errors.append(current_error)
    if parent_fd is not None:
        _fsync_fd_best_effort(parent_fd)
    return rollback_errors


def ingest_videos(config: Config) -> dict:
    _validate_root_relationship(config)
    awaiting, success = VIDEO_INGEST_STAGE
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != awaiting:
            raise WorkflowError("当前阶段不能接收视频")
        video_dir = Path(task["video_dir"])
        candidates = _changed_video_candidates(video_dir, task["video_baseline"])
        if len(candidates) != 2:
            raise WorkflowError(
                f"相对提示词 baseline 必须恰好两个新建或变化的视频下载候选，当前为 {len(candidates)} 个"
            )
        if candidates[0][1]["mtime_ns"] == candidates[1][1]["mtime_ns"]:
            raise WorkflowError("两个视频下载候选的 mtime_ns 必须不同")
        candidates.sort(key=lambda item: item[1]["mtime_ns"])
        inspected = [
            (path, snapshot, *_inspect_video_candidate(config, path, snapshot))
            for path, snapshot in candidates
        ]
        silent = [
            path.name for path, _, _, media in inspected if media["audio"] is None
        ]
        if silent:
            raise WorkflowError(
                f"视频下载候选必须包含清楚口播音轨，缺少音轨：{', '.join(silent)}"
            )

        parent_fd = None
        renamed = []
        expected_committed_state = None
        try:
            parent_fd, parent_identity = _open_pinned_directory(video_dir, "视频目录")
            with os.scandir(parent_fd) as entries:
                canonical_conflicts = sorted(
                    entry.name
                    for entry in entries
                    if entry.name.casefold() == "final.mp4"
                    or entry.name.casefold().startswith(("01-segment.", "02-segment."))
                )
            if canonical_conflicts:
                raise WorkflowError(
                    f"规范视频已存在，拒绝覆盖：{canonical_conflicts[0]}"
                )
            destinations = [
                video_dir / f"{index:02d}-segment{source.suffix.casefold()}"
                for index, (source, _, _, _) in enumerate(inspected, start=1)
            ]
            for destination in destinations:
                if _entry_exists_at(parent_fd, destination.name):
                    raise WorkflowError(f"规范视频已存在，拒绝覆盖：{destination.name}")
            for index, ((source, snapshot, consumed, _), destination) in enumerate(
                zip(inspected, destinations), start=1
            ):
                _before_video_candidate_rename(index, source, destination)
                _verify_pinned_path(video_dir, parent_fd, parent_identity, "视频目录")
                current = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
                if not _snapshot_matches(current, snapshot):
                    raise WorkflowError("视频下载候选在规范命名前发生变化")
                if not _rename_noreplace(
                    parent_fd,
                    source.name,
                    parent_fd,
                    destination.name,
                ):
                    raise WorkflowError(f"规范视频已存在，拒绝覆盖：{destination.name}")
                renamed.append((source, destination, consumed))
                _after_video_candidate_renamed(index, source, destination)
                published = os.stat(
                    destination.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISLNK(published.st_mode)
                    or not stat.S_ISREG(published.st_mode)
                    or not _same_identity(published, consumed)
                    or published.st_size != consumed.st_size
                    or published.st_mtime_ns != consumed.st_mtime_ns
                ):
                    raise WorkflowError("规范视频发布后身份不匹配")
            _fsync_fd_best_effort(parent_fd)
            task["videos"] = [str(path.resolve(strict=False)) for path in destinations]
            task["video_baseline"] = {}
            task["stage"] = success
            task["updated_at"] = _timestamp_now()
            expected_committed_state = state_value
            save_state_atomic(config, state_value)
            return task
        except BaseException as exc:
            if not _state_was_committed(config, expected_committed_state):
                rollback_errors = _rollback_ingested_videos(parent_fd, renamed)
                if rollback_errors:
                    details = "；".join(str(error) for error in rollback_errors)
                    raise WorkflowError(f"视频接收失败且事务回滚失败：{details}") from exc
            if isinstance(exc, WorkflowError):
                raise
            if isinstance(exc, OSError):
                raise WorkflowError(f"无法事务化接收视频：{exc}") from exc
            raise
        finally:
            if parent_fd is not None:
                os.close(parent_fd)


def _run_ffmpeg(command: list[str], label: str) -> None:
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, UnicodeError) as exc:
        raise WorkflowError(f"无法运行 ffmpeg（{label}）：{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"退出码 {completed.returncode}"
        raise WorkflowError(f"ffmpeg {label}失败：{detail}")


def _after_merge_segment_open(path: Path) -> None:
    return None


def _after_final_published(path: Path) -> None:
    return None


def _canonical_video_segments(task: dict) -> list[Path]:
    videos = task["videos"]
    if len(videos) != 2:
        raise WorkflowError("状态必须包含恰好两个规范视频段")
    video_dir = Path(task["video_dir"])
    segments = []
    for index, value in enumerate(videos, start=1):
        path = _path_from_state(value, f"videos[{index - 1}]")
        if (
            path.parent != video_dir
            or path.suffix not in VIDEO_SUFFIXES
            or path.name != f"{index:02d}-segment{path.suffix}"
        ):
            raise WorkflowError(f"第 {index} 段视频状态路径不是规范路径")
        segments.append(path)
    return segments


def _open_merge_segment(
    config: Config, path: Path
) -> tuple[int, os.stat_result, dict]:
    descriptor = None
    expected = _real_regular_file(path, "规范视频段")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError("规范视频段在打开时被替换")
        _after_merge_segment_open(path)
        media = probe_media(config, path)
        current = path.lstat()
        final_opened = os.fstat(descriptor)
        for info in (current, final_opened):
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not _same_identity(info, expected)
                or info.st_size != expected.st_size
                or info.st_mtime_ns != expected.st_mtime_ns
            ):
                raise WorkflowError("规范视频段在探测期间发生变化")
        return descriptor, expected, media
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _verify_open_merge_segment(path: Path, descriptor: int, expected: os.stat_result) -> None:
    try:
        current = path.lstat()
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise WorkflowError(f"规范视频段在合并期间不可用：{exc}") from exc
    for info in (current, opened):
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or not _same_identity(info, expected)
            or info.st_size != expected.st_size
            or info.st_mtime_ns != expected.st_mtime_ns
        ):
            raise WorkflowError("规范视频段在合并期间发生变化")


def _write_concat_inputs(directory: Path, inputs: list[Path]) -> Path:
    lines = []
    for index, source in enumerate(inputs, start=1):
        controlled_name = f"input-{index}{source.suffix.casefold()}"
        controlled = directory / controlled_name
        os.symlink(source, controlled)
        lines.append(f"file '{controlled_name}'\n")
    concat_file = directory / "concat.txt"
    concat_file.write_text("".join(lines), encoding="utf-8")
    return concat_file


def _concat_videos(config: Config, directory: Path, inputs: list[Path], output: Path) -> None:
    concat_file = _write_concat_inputs(directory, inputs)
    command = [
        config.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *COPY_CONCAT_ARGS[:4],
        "-i",
        str(concat_file),
        *COPY_CONCAT_ARGS[4:],
        str(output),
    ]
    _run_ffmpeg(command, "拼接")


def _normalize_videos(
    config: Config,
    directory: Path,
    segments: list[Path],
    media: list[dict],
) -> list[Path]:
    first_video = media[0]["video"]
    target_width = max(2, first_video["width"] - first_video["width"] % 2)
    target_height = max(2, first_video["height"] - first_video["height"] % 2)
    target_rate = first_video["r_frame_rate"]
    keep_audio = True
    normalized = []
    for index, source in enumerate(segments, start=1):
        output = directory / f"normalized-{index}.mp4"
        command = [
            config.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
        ]
        if keep_audio and media[index - 1]["audio"] is None:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000",
                ]
            )
        command.extend(
            [
            "-map",
            "0:v:0",
            "-vf",
            (
                f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1,fps={target_rate}"
            ),
            *NORMALIZE_VIDEO_ARGS,
            ]
        )
        if keep_audio:
            audio_input = "0:a:0" if media[index - 1]["audio"] is not None else "1:a:0"
            command.extend(
                [
                    "-map",
                    audio_input,
                    "-af",
                    "apad",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    *NORMALIZE_AUDIO_ARGS,
                    "-shortest",
                ]
            )
        else:
            command.append("-an")
        command.append(str(output))
        _run_ffmpeg(command, f"规范化第 {index} 段")
        probe_media(config, output)
        normalized.append(output)
    return normalized


def _validate_idempotent_merge(config: Config, task: dict) -> dict:
    merge = task["merge"]
    if (
        not isinstance(merge, dict)
        or merge.get("mode") not in {"copy", "transcode", "manual"}
        or type(merge.get("duration")) not in {int, float}
        or not math.isfinite(merge["duration"])
        or merge["duration"] <= 0
    ):
        raise WorkflowError("merged 状态缺少有效合并记录")
    if merge["mode"] == "manual":
        recorded_sha256 = merge.get("sha256")
        if (
            not isinstance(recorded_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", recorded_sha256) is None
        ):
            raise WorkflowError("手工成片记录缺少有效内容指纹")
        output = _path_from_state(merge.get("output"), "merge.output")
        video_dir = Path(task["video_dir"])
        if output.parent != video_dir:
            raise WorkflowError("手工成片不在当前视频目录内")
        video_fd = None
        try:
            video_fd, video_identity = _open_pinned_directory(video_dir, "视频目录")
            output_media = probe_media(config, output)
            actual_sha256, _ = _sha256_regular_file_at(
                video_fd,
                output.name,
                "手工成片",
            )
            _verify_pinned_path(video_dir, video_fd, video_identity, "视频目录")
            if abs(output_media["duration"] - merge["duration"]) > 0.05:
                raise WorkflowError("手工成片实际时长与登记记录不一致")
            if actual_sha256 != recorded_sha256:
                raise WorkflowError("手工成片内容指纹与登记记录不一致")
        finally:
            if video_fd is not None:
                os.close(video_fd)
        return task

    final = Path(task["video_dir"]) / "final.mp4"
    if merge.get("output") != str(final.resolve(strict=False)):
        raise WorkflowError("merged 状态缺少规范 final.mp4 记录")
    final_media = probe_media(config, final)
    if abs(final_media["duration"] - merge["duration"]) > 0.05:
        raise WorkflowError("final.mp4 实际时长与合并记录不一致")
    segments = _canonical_video_segments(task)
    source_media = [probe_media(config, segment) for segment in segments]
    expected_duration = sum(media["duration"] for media in source_media)
    if abs(final_media["duration"] - expected_duration) > 1.0:
        raise WorkflowError("final.mp4 实际时长与两段源视频总时长不一致")
    if merge["mode"] == "copy":
        if not _copy_passthrough_compatible(source_media[0], source_media[1]):
            raise WorkflowError("copy 合并记录与当前源媒体不兼容")
    elif (
        final_media["video"]["codec_name"] != "h264"
        or final_media["video"]["pix_fmt"] != "yuv420p"
        or final_media["audio"] is None
        or final_media["audio"]["codec_name"] != "aac"
        or final_media["audio"]["sample_rate"] != 48000
    ):
        raise WorkflowError("transcode 合并结果不符合 H.264/yuv420p/AAC 规范")
    return task


def _manual_video_source(task: dict, source) -> Path:
    source_path = Path(source)
    expected = _real_regular_file(source_path, "手工成片")
    if source_path.suffix.casefold() not in VIDEO_SUFFIXES:
        raise WorkflowError("手工成片格式必须是 MP4、MOV、M4V 或 WEBM")
    try:
        resolved_source = source_path.resolve(strict=True)
        video_dir = Path(task["video_dir"]).resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"无法解析手工成片路径：{exc}") from exc
    if resolved_source.parent != video_dir:
        raise WorkflowError("手工成片必须位于当前任务的视频制作目录")
    current = source_path.lstat()
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or not _same_identity(current, expected)
    ):
        raise WorkflowError("手工成片不能是符号链接或已被替换")
    return source_path


def _sha256_regular_file_at(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[str, os.stat_result]:
    descriptor = None
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
            raise WorkflowError(f"{label}必须是普通文件且不能是符号链接")
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError(f"{label}在打开时被替换")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        final_opened = os.fstat(descriptor)
        for info in (current, final_opened):
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not _same_identity(info, expected)
                or info.st_size != expected.st_size
                or info.st_mtime_ns != expected.st_mtime_ns
            ):
                raise WorkflowError(f"{label}在读取期间发生变化")
        return digest.hexdigest(), expected
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法读取{label}：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def register_manual_video(config: Config, source) -> dict:
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None:
            raise WorkflowError("当前没有活动任务，不能登记手工成片")
        video_dir = Path(task["video_dir"])
        video_fd = None
        try:
            video_fd, video_identity = _open_pinned_directory(video_dir, "视频目录")
            source_path = _manual_video_source(task, source)
            if task["stage"] == "merged":
                merge = task.get("merge")
                if (
                    isinstance(merge, dict)
                    and merge.get("mode") == "manual"
                    and merge.get("output") == str(source_path.resolve(strict=True))
                ):
                    return _validate_idempotent_merge(config, task)
                raise WorkflowError("当前任务已经完成，不能登记另一条手工成片")

            expected = _real_regular_file(source_path, "手工成片")
            media = probe_media(config, source_path)
            if media["audio"] is None:
                raise WorkflowError("手工成片必须包含清楚口播音轨")
            content_sha256, hashed = _sha256_regular_file_at(
                video_fd,
                source_path.name,
                "手工成片",
            )
            current = source_path.lstat()
            _verify_pinned_path(video_dir, video_fd, video_identity, "视频目录")
            if (
                not _same_identity(current, expected)
                or not _same_identity(hashed, expected)
                or current.st_size != expected.st_size
                or current.st_mtime_ns != expected.st_mtime_ns
            ):
                raise WorkflowError("手工成片在登记期间发生变化")
            task["video_baseline"] = {}
            task["merge"] = {
                "output": str(source_path.resolve(strict=True)),
                "mode": "manual",
                "duration": media["duration"],
                "sha256": content_sha256,
            }
            task["stage"] = "merged"
            task["updated_at"] = _timestamp_now()
            save_state_atomic(config, state_value)
            return task
        finally:
            if video_fd is not None:
                os.close(video_fd)


def _after_publish_text_read(name: str) -> None:
    return None


def _after_publish_directory_open(path: Path) -> None:
    return None


def _open_publish_text_at(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[int, os.stat_result, bytes, str]:
    descriptor = None
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
            raise WorkflowError(f"{label}必须是普通文件且不能是符号链接")
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError(f"{label}在打开时被替换")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowError(f"{label}必须是 UTF-8 文本") from exc
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        final_opened = os.fstat(descriptor)
        for info in (current, final_opened):
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not _same_identity(info, expected)
                or info.st_size != expected.st_size
                or info.st_mtime_ns != expected.st_mtime_ns
            ):
                raise WorkflowError(f"{label}在读取期间发生变化")
        result = (descriptor, expected, raw, text)
        descriptor = None
        return result
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法读取{label}：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_publish_text_at(
    parent_fd: int,
    name: str,
    descriptor: int,
    expected: os.stat_result,
    raw: bytes,
    label: str,
) -> None:
    def verify_metadata() -> None:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(descriptor)
        except OSError as exc:
            raise WorkflowError(f"{label}在校验期间不可用：{exc}") from exc
        for info in (current, opened):
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not _same_identity(info, expected)
                or info.st_size != expected.st_size
                or info.st_mtime_ns != expected.st_mtime_ns
            ):
                raise WorkflowError(f"{label}在校验期间发生变化")

    verify_metadata()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise WorkflowError(f"无法复核{label}：{exc}") from exc
    if b"".join(chunks) != raw:
        raise WorkflowError(f"{label}在校验期间发生变化")
    verify_metadata()


def _validate_single_line_title(
    value: str,
    label: str,
    *,
    max_chars: int | None = None,
) -> None:
    if not value:
        raise WorkflowError(f"{label}不能为空")
    if "\n" in value or "\r" in value:
        raise WorkflowError(f"{label}必须只有一行")
    if max_chars is not None and len(value) > max_chars:
        raise WorkflowError(f"{label}不能超过 {max_chars} 个字符")
    if "#" in value:
        raise WorkflowError(f"{label}不能包含标签")


def validate_product_copy(config: Config) -> dict:
    _, done_root = _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if (
            task is None
            or task["stage"] == "selected"
            or not isinstance(task.get("reference"), str)
        ):
            raise WorkflowError("当前任务尚未确认产品参考图，不能校验店铺产品资料")
        task_dir = Path(task["task_dir"])
        title_path = task_dir / SHOP_PRODUCT_TITLE_FILE
        content_path = task_dir / SHOP_PRODUCT_CONTENT_FILE
        size_path = task_dir / SHOP_PRODUCT_SIZE_FILE
        done_fd = date_fd = task_fd = title_fd = content_fd = size_fd = None
        try:
            (
                done_fd,
                done_identity,
                date_fd,
                date_identity,
                task_fd,
                task_identity,
            ) = _open_task_directory_chain(
                done_root,
                task_dir,
            )

            def verify_directory_chain() -> None:
                _verify_task_directory_chain(
                    done_root,
                    task_dir,
                    done_fd,
                    done_identity,
                    date_fd,
                    date_identity,
                    task_fd,
                    task_identity,
                )

            verify_directory_chain()
            title_fd, title_identity, title_raw, title_text = _open_publish_text_at(
                task_fd,
                SHOP_PRODUCT_TITLE_FILE,
                "店铺产品标题",
            )
            _after_publish_text_read(SHOP_PRODUCT_TITLE_FILE)
            content_fd, content_identity, content_raw, content_text = (
                _open_publish_text_at(
                    task_fd,
                    SHOP_PRODUCT_CONTENT_FILE,
                    "店铺产品内容",
                )
            )
            _after_publish_text_read(SHOP_PRODUCT_CONTENT_FILE)
            size_fd, size_identity, size_raw, size_text = _open_publish_text_at(
                task_fd,
                SHOP_PRODUCT_SIZE_FILE,
                "店铺产品尺寸",
            )
            _after_publish_text_read(SHOP_PRODUCT_SIZE_FILE)

            title = title_text.strip()
            content = content_text.strip()
            size = size_text.strip()
            _validate_single_line_title(title, "店铺产品标题", max_chars=150)
            if not content:
                raise WorkflowError("店铺产品内容不能为空")
            if "#" in content:
                raise WorkflowError("店铺产品内容不能包含发布标签")
            if any(
                re.fullmatch(r"[^:\r\n]+:\s+\S.*", line) is None
                for line in content.splitlines()
            ):
                raise WorkflowError("店铺产品内容每行必须使用 Field: Value 格式")
            if not size:
                raise WorkflowError("店铺产品尺寸不能为空")
            if "#" in size:
                raise WorkflowError("店铺产品尺寸不能包含发布标签")

            for name, descriptor, identity, raw, label in (
                (
                    SHOP_PRODUCT_TITLE_FILE,
                    title_fd,
                    title_identity,
                    title_raw,
                    "店铺产品标题",
                ),
                (
                    SHOP_PRODUCT_CONTENT_FILE,
                    content_fd,
                    content_identity,
                    content_raw,
                    "店铺产品内容",
                ),
                (
                    SHOP_PRODUCT_SIZE_FILE,
                    size_fd,
                    size_identity,
                    size_raw,
                    "店铺产品尺寸",
                ),
            ):
                _verify_publish_text_at(
                    task_fd,
                    name,
                    descriptor,
                    identity,
                    raw,
                    label,
                )
            verify_directory_chain()

            return {
                "product_title_file": str(title_path),
                "product_content_file": str(content_path),
                "product_size_file": str(size_path),
                "product_title": title,
                "product_content": content,
                "product_size": size,
            }
        finally:
            for descriptor in (
                size_fd,
                content_fd,
                title_fd,
                task_fd,
                date_fd,
                done_fd,
            ):
                if descriptor is not None:
                    os.close(descriptor)


def _video_link_title_name_at(video_fd: int) -> str:
    for name in (VIDEO_LINK_TITLE_FILE, PRODUCT_TITLE_FILE):
        try:
            os.stat(name, dir_fd=video_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WorkflowError(f"无法检查视频链接标题：{exc}") from exc
        return name
    return VIDEO_LINK_TITLE_FILE


def validate_publish_copy(config: Config) -> dict:
    _, done_root = _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != "merged":
            raise WorkflowError("当前任务尚无已完成视频，不能校验发布内容")
        task_dir = Path(task["task_dir"])
        video_dir = Path(task["video_dir"])
        content_path = video_dir / PUBLISH_COPY_FILE
        done_fd = date_fd = task_fd = video_fd = content_fd = title_fd = None
        try:
            (
                done_fd,
                done_identity,
                date_fd,
                date_identity,
                task_fd,
                task_identity,
            ) = _open_task_directory_chain(
                done_root,
                task_dir,
            )
            video_fd, video_identity = _open_pinned_directory_at(
                task_fd,
                video_dir.name,
                "视频目录",
            )
            _after_publish_directory_open(video_dir)

            def verify_directory_chain() -> None:
                _verify_task_directory_chain(
                    done_root,
                    task_dir,
                    done_fd,
                    done_identity,
                    date_fd,
                    date_identity,
                    task_fd,
                    task_identity,
                )
                _verify_pinned_entry(
                    task_fd,
                    video_dir.name,
                    video_fd,
                    video_identity,
                    "视频目录",
                )

            verify_directory_chain()
            task = _validate_idempotent_merge(config, task)
            verify_directory_chain()
            title_name = _video_link_title_name_at(video_fd)
            title_path = video_dir / title_name
            content_fd, content_identity, content_raw, content_text = (
                _open_publish_text_at(
                    video_fd,
                    PUBLISH_COPY_FILE,
                    "TikTok 发布内容",
                )
            )
            _after_publish_text_read(PUBLISH_COPY_FILE)
            title_fd, title_identity, title_raw, title_text = _open_publish_text_at(
                video_fd,
                title_name,
                "视频链接标题",
            )
            _after_publish_text_read(title_name)
            content = content_text.strip()
            title = title_text.strip()

            _validate_single_line_title(title, "视频链接标题", max_chars=30)

            lines = content.splitlines()
            if len(lines) < 2:
                raise WorkflowError("发布内容必须包含正文，并把标签放在正文后")
            tag_line = lines[-1].strip()
            tags = tag_line.split()
            if not tags or any(
                re.fullmatch(r"#[^\s#]+", tag) is None for tag in tags
            ):
                raise WorkflowError("发布内容最后一行必须只包含标签")
            if len(tags) > 5:
                raise WorkflowError("发布标签最多 5 个")
            body = "\n".join(lines[:-1]).strip()
            if not body:
                raise WorkflowError("发布内容正文不能为空")
            if "#" in body:
                raise WorkflowError("发布标签必须统一放在正文后面")

            _verify_publish_text_at(
                video_fd,
                PUBLISH_COPY_FILE,
                content_fd,
                content_identity,
                content_raw,
                "TikTok 发布内容",
            )
            _verify_publish_text_at(
                video_fd,
                title_name,
                title_fd,
                title_identity,
                title_raw,
                "视频链接标题",
            )
            verify_directory_chain()

            return {
                "video": task["merge"]["output"],
                "content_file": str(content_path),
                "title_file": str(title_path),
                "link_title_file": str(title_path),
                "content": content,
                "title": title,
                "link_title": title,
                "tag_count": len(tags),
            }
        finally:
            for descriptor in (
                title_fd,
                content_fd,
                video_fd,
                task_fd,
                date_fd,
                done_fd,
            ):
                if descriptor is not None:
                    os.close(descriptor)


def _rollback_published_final(
    final: Path, published_identity: tuple[int, int], video_dir: Path
) -> None:
    try:
        current = final.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != published_identity
        ):
            raise WorkflowError("final.mp4 在事务回滚前被替换")
        final.unlink()
        _fsync_directory(video_dir)
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法回滚本次 final.mp4：{exc}") from exc


def _verify_published_final(
    config: Config,
    final: Path,
    expected: os.stat_result,
    expected_media: dict,
) -> dict:
    current = _real_regular_file(final, "final.mp4")
    if (
        not _same_identity(current, expected)
        or current.st_size != expected.st_size
        or current.st_mtime_ns != expected.st_mtime_ns
    ):
        raise WorkflowError("final.mp4 发布后发生变化")
    media = probe_media(config, final)
    if (
        media["video"] != expected_media["video"]
        or media["audio"] != expected_media["audio"]
        or abs(media["duration"] - expected_media["duration"]) > 0.05
    ):
        raise WorkflowError("final.mp4 发布后的媒体结构与已验证输出不一致")
    return media


def merge_videos(config: Config) -> dict:
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None:
            raise WorkflowError("当前没有活动任务，不能合并视频")
        if task["stage"] == "merged":
            return _validate_idempotent_merge(config, task)
        if task["stage"] != VIDEO_INGEST_STAGE[1]:
            raise WorkflowError("当前阶段不能合并视频")

        video_dir = Path(task["video_dir"])
        final = video_dir / "final.mp4"
        if final.exists() or final.is_symlink():
            raise WorkflowError("final.mp4 已存在且不属于当前合并，拒绝覆盖")
        segments = _canonical_video_segments(task)
        opened = []
        published = False
        published_identity = None
        expected_committed_state = None
        try:
            for segment in segments:
                opened.append((segment, *_open_merge_segment(config, segment)))
            media = [item[3] for item in opened]
            compatible = _copy_passthrough_compatible(media[0], media[1])
            mode = "copy" if compatible else "transcode"
            with tempfile.TemporaryDirectory(dir=video_dir, prefix=".merge-") as temporary_name:
                temporary = Path(temporary_name)
                merge_inputs = segments
                if not compatible:
                    merge_inputs = _normalize_videos(
                        config,
                        temporary,
                        segments,
                        media,
                    )
                output = temporary / "merged.mp4"
                _concat_videos(config, temporary, merge_inputs, output)
                output_media = probe_media(config, output)
                expected_duration = media[0]["duration"] + media[1]["duration"]
                if abs(output_media["duration"] - expected_duration) > 1.0:
                    raise WorkflowError("合并视频时长与两段源视频总时长相差超过 1 秒")
                for segment, descriptor, expected, _ in opened:
                    _verify_open_merge_segment(segment, descriptor, expected)

                output_info = _real_regular_file(output, "合并输出")
                published_identity = (output_info.st_dev, output_info.st_ino)

                video_fd = output_fd = None
                try:
                    video_fd, video_identity = _open_pinned_directory(video_dir, "视频目录")
                    output_fd, _ = _open_pinned_directory(temporary, "合并临时目录")
                    _verify_pinned_path(video_dir, video_fd, video_identity, "视频目录")
                    if not _rename_noreplace(
                        output_fd,
                        output.name,
                        video_fd,
                        final.name,
                    ):
                        raise WorkflowError("final.mp4 已存在，拒绝覆盖")
                    published = True
                    _after_final_published(final)
                    published_info = os.stat(
                        final.name,
                        dir_fd=video_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISLNK(published_info.st_mode) or not stat.S_ISREG(
                        published_info.st_mode
                    ) or not _same_identity(
                        published_info, output_info
                    ) or published_info.st_size != output_info.st_size or (
                        published_info.st_mtime_ns != output_info.st_mtime_ns
                    ):
                        raise WorkflowError("final.mp4 发布后不是规范普通文件")
                    _verify_published_final(
                        config,
                        final,
                        output_info,
                        output_media,
                    )
                    _fsync_fd_best_effort(video_fd)
                finally:
                    if output_fd is not None:
                        os.close(output_fd)
                    if video_fd is not None:
                        os.close(video_fd)

            output_media = _verify_published_final(
                config,
                final,
                output_info,
                output_media,
            )
            for segment, descriptor, expected, _ in opened:
                _verify_open_merge_segment(segment, descriptor, expected)
            task["merge"] = {
                "output": str(final.resolve(strict=False)),
                "mode": mode,
                "duration": output_media["duration"],
            }
            task["stage"] = "merged"
            task["updated_at"] = _timestamp_now()
            expected_committed_state = state_value
            save_state_atomic(config, state_value)
            return task
        except BaseException as exc:
            if published and not _state_was_committed(config, expected_committed_state):
                try:
                    _rollback_published_final(final, published_identity, video_dir)
                    published = False
                except WorkflowError as rollback_exc:
                    raise WorkflowError(
                        f"视频合并失败且 final.mp4 回滚失败：{rollback_exc}"
                    ) from exc
            if isinstance(exc, WorkflowError):
                raise
            if isinstance(exc, OSError):
                raise WorkflowError(f"无法合并视频：{exc}") from exc
            raise
        finally:
            for _, descriptor, _, _ in opened:
                os.close(descriptor)


def _snapshot_matches(info: os.stat_result, snapshot: dict) -> bool:
    return snapshot == {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }


def _after_image_candidate_open(path: Path) -> None:
    return None


def _load_download_image(path: Path, snapshot: dict) -> tuple[Image.Image, os.stat_result]:
    descriptor = None
    expected = _real_regular_file(path, "图片下载候选")
    if not _snapshot_matches(expected, snapshot):
        raise WorkflowError("图片下载候选在扫描后发生变化")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError("图片下载候选在打开时被替换")
        _after_image_candidate_open(path)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(handle) as image:
                    image_format = image.format
                    image.load()
                    mode = "RGBA" if "A" in image.getbands() else "RGB"
                    converted = image.convert(mode)
                    clean = Image.new(mode, converted.size)
                    clean.paste(converted)
        current = path.lstat()
        final_opened = os.fstat(descriptor)
        for info in (current, final_opened):
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not _same_identity(info, expected)
                or info.st_size != expected.st_size
                or info.st_mtime_ns != expected.st_mtime_ns
            ):
                raise WorkflowError("图片下载候选在读取期间发生变化")
        if image_format not in {"JPEG", "PNG", "WEBP"}:
            raise WorkflowError("图片下载候选格式必须是 JPEG、PNG 或 WEBP")
        width, height = clean.size
        if abs(width * 800 - height * 450) > height * 16:
            raise WorkflowError("图片下载候选宽高比必须接近 9:16")
        return clean, final_opened
    except WorkflowError:
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise WorkflowError(f"图片下载候选损坏或无法完整解码：{exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _changed_image_candidates(image_dir: Path, baseline: dict) -> list[tuple[Path, dict]]:
    current = _image_baseline(image_dir)
    return [
        (image_dir / name, snapshot)
        for name, snapshot in current.items()
        if baseline.get(name) != snapshot
    ]


def _before_image_candidate_delete(path: Path) -> None:
    return None


def _delete_consumed_candidate(path: Path, consumed: os.stat_result) -> None:
    parent_fd = None
    quarantine_name = None
    try:
        parent_fd, parent_identity = _open_pinned_directory(path.parent, "图片目录")
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or not _same_identity(current, consumed)
            or current.st_size != consumed.st_size
            or current.st_mtime_ns != consumed.st_mtime_ns
        ):
            return
        _before_image_candidate_delete(path)
        _verify_pinned_path(path.parent, parent_fd, parent_identity, "图片目录")
        quarantine_name = f".{path.name}.{uuid.uuid4().hex}.consumed"
        if not _rename_noreplace(
            parent_fd,
            path.name,
            parent_fd,
            quarantine_name,
        ):
            raise WorkflowError("无法隔离已消费图片下载候选")
        quarantined = os.stat(
            quarantine_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            stat.S_ISLNK(quarantined.st_mode)
            or not stat.S_ISREG(quarantined.st_mode)
            or not _same_identity(quarantined, consumed)
            or quarantined.st_size != consumed.st_size
            or quarantined.st_mtime_ns != consumed.st_mtime_ns
        ):
            if not _rename_noreplace(
                parent_fd,
                quarantine_name,
                parent_fd,
                path.name,
            ):
                raise WorkflowError("候选在删除前被替换，且无法恢复外来文件")
            quarantine_name = None
            return
        os.unlink(quarantine_name, dir_fd=parent_fd)
        quarantine_name = None
        _fsync_fd_best_effort(parent_fd)
        _verify_pinned_path(path.parent, parent_fd, parent_identity, "图片目录")
    except FileNotFoundError:
        return
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"规范图片已提交，但无法删除已消费下载：{exc}") from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _confirmed_image_candidate(image_dir: Path, source) -> tuple[Path, dict]:
    try:
        source_path = Path(source)
    except TypeError as exc:
        raise WorkflowError("确认图片路径非法") from exc
    if not source_path.is_absolute():
        raise WorkflowError("确认图片必须使用绝对路径")
    if (
        source_path.name.casefold()
        in {REFERENCE_FILE.casefold(), *(name.casefold() for name in FRAME_FILES.values())}
        or source_path.suffix.casefold() not in IMAGE_DOWNLOAD_EXTENSIONS
    ):
        raise WorkflowError("确认图片必须是非规范的 JPEG、PNG 或 WEBP 下载文件")
    expected = _real_regular_file(source_path, "用户确认图片")
    try:
        resolved_source = source_path.resolve(strict=True)
        resolved_image_dir = image_dir.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"无法解析确认图片路径：{exc}") from exc
    allowed_parents = {resolved_image_dir}
    retry_archive = image_dir / RETRY_ARCHIVE_DIR
    if retry_archive.exists() or retry_archive.is_symlink():
        _assert_real_directory(retry_archive, "图片退回归档")
        allowed_parents.add(retry_archive.resolve(strict=True))
    if resolved_source.parent not in allowed_parents:
        raise WorkflowError("确认图片必须位于当前任务的图片制作目录或退回归档")
    current = source_path.lstat()
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or not _same_identity(current, expected)
        or current.st_size != expected.st_size
        or current.st_mtime_ns != expected.st_mtime_ns
    ):
        raise WorkflowError("用户确认图片在登记前发生变化")
    return source_path, {
        "device": current.st_dev,
        "inode": current.st_ino,
        "size": current.st_size,
        "mtime_ns": current.st_mtime_ns,
    }


def ingest_image(config: Config, index, confirmed_source=None) -> dict:
    valid_index = _validate_image_index(index)
    _validate_root_relationship(config)
    _, awaiting, success = IMAGE_STAGE_FLOW[valid_index]
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != awaiting:
            raise WorkflowError(f"当前阶段不能接收第 {valid_index} 张图片")
        image_dir = Path(task["image_dir"])
        if confirmed_source is None:
            candidates = _changed_image_candidates(image_dir, task["image_baseline"])
            if len(candidates) != 1:
                raise WorkflowError(
                    f"相对提示词 baseline 必须恰好一个新建或变化的图片下载候选，当前为 {len(candidates)} 个"
                )
            candidate, snapshot = candidates[0]
        else:
            candidate, snapshot = _confirmed_image_candidate(
                image_dir,
                confirmed_source,
            )
        destination = image_dir / FRAME_FILES[valid_index]
        if destination.exists() or destination.is_symlink():
            raise WorkflowError(f"规范图片已存在，拒绝覆盖：{destination.name}")
        clean, consumed = _load_download_image(candidate, snapshot)
        temporary = _write_png_temp(image_dir, destination.name, clean)
        task["frames"][str(valid_index)] = str(destination.resolve(strict=False))
        task["image_baseline"] = {}
        task["stage"] = success
        task["updated_at"] = _timestamp_now()
        _publish_png_with_state(
            config,
            state_value,
            temporary,
            destination,
            allow_replace=False,
        )
        _delete_consumed_candidate(candidate, consumed)
        return task


def _retry_image_archive_name(index: int) -> str:
    stem = Path(FRAME_FILES[index]).stem
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stem}-rejected-{timestamp}-{uuid.uuid4().hex}.png"


def _after_retry_image_moved(source: Path, archive: Path) -> None:
    return None


def _retry_image_matches(
    current: os.stat_result,
    expected: os.stat_result,
) -> bool:
    return (
        not stat.S_ISLNK(current.st_mode)
        and stat.S_ISREG(current.st_mode)
        and _same_identity(current, expected)
        and current.st_size == expected.st_size
        and current.st_mtime_ns == expected.st_mtime_ns
    )


def _rollback_retry_image(
    image_dir: Path,
    image_fd: int,
    image_identity: os.stat_result,
    archive_fd: int,
    archive_identity: os.stat_result,
    archive_name: str,
    frame_name: str,
    frame_fd: int,
    source_identity: os.stat_result,
) -> None:
    try:
        _verify_pinned_path(image_dir, image_fd, image_identity, "图片目录")
        _verify_pinned_entry(
            image_fd,
            RETRY_ARCHIVE_DIR,
            archive_fd,
            archive_identity,
            "退回归档",
        )
        archived = os.stat(
            archive_name,
            dir_fd=archive_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(frame_fd)
        if not _retry_image_matches(archived, source_identity) or not (
            _retry_image_matches(opened, source_identity)
        ):
            raise WorkflowError("退回归档在规范图片回滚前被替换或修改")
        if not _rename_noreplace(
            archive_fd,
            archive_name,
            image_fd,
            frame_name,
        ):
            raise WorkflowError("规范图片原路径已被占用，拒绝覆盖")
        restored = os.stat(
            frame_name,
            dir_fd=image_fd,
            follow_symlinks=False,
        )
        if not _retry_image_matches(restored, source_identity):
            raise WorkflowError("规范图片回滚后身份不匹配")
        _fsync_fd_best_effort(archive_fd)
        _fsync_fd_best_effort(image_fd)
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"无法回滚退回的规范图片：{exc}") from exc


def retry_image(config: Config, index) -> dict:
    valid_index = _validate_image_index(index)
    _, done_root = _validate_root_relationship(config)
    expected_stage, predecessor = IMAGE_RETRY_FLOW[valid_index]
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None or task["stage"] != expected_stage:
            raise WorkflowError(f"当前阶段不能退回第 {valid_index} 张图片")
        expected_frame_keys = {str(i) for i in range(1, valid_index + 1)}
        if set(task["frames"]) != expected_frame_keys:
            raise WorkflowError("当前图片帧状态与退回阶段不一致")
        if task["image_baseline"]:
            raise WorkflowError("当前图片基线状态与退回阶段不一致")
        if task["video_baseline"] or task["videos"] or task["merge"] is not None:
            raise WorkflowError("当前任务已有视频状态，不能退回图片")

        task_dir = Path(task["task_dir"])
        image_dir = Path(task["image_dir"])
        frame = image_dir / FRAME_FILES[valid_index]
        canonical = frame.resolve(strict=False)
        if task["frames"].get(str(valid_index)) != str(canonical):
            raise WorkflowError(f"第 {valid_index} 张规范帧状态路径不是规范路径")

        done_fd = date_fd = task_fd = image_fd = archive_fd = frame_fd = None
        moved = False
        archive_name = None
        source_identity = None
        expected_committed_state = None
        try:
            (
                done_fd,
                done_identity,
                date_fd,
                date_identity,
                task_fd,
                task_identity,
            ) = _open_task_directory_chain(
                done_root,
                task_dir,
            )
            image_fd, image_identity = _open_pinned_directory_at(
                task_fd,
                image_dir.name,
                "图片目录",
            )

            def verify_directory_chain() -> None:
                _verify_task_directory_chain(
                    done_root,
                    task_dir,
                    done_fd,
                    done_identity,
                    date_fd,
                    date_identity,
                    task_fd,
                    task_identity,
                )
                _verify_pinned_entry(
                    task_fd,
                    image_dir.name,
                    image_fd,
                    image_identity,
                    "图片目录",
                )

            verify_directory_chain()
            _validate_canonical_image(task, "reference", None)
            for previous in range(1, valid_index):
                _validate_canonical_image(task, "frames", previous)
            verify_directory_chain()
            try:
                source_identity = os.stat(
                    frame.name,
                    dir_fd=image_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                source_identity = None
            if source_identity is not None:
                if stat.S_ISLNK(source_identity.st_mode) or not stat.S_ISREG(
                    source_identity.st_mode
                ):
                    raise WorkflowError("待退回规范图片必须是普通文件且不能是符号链接")
                frame_fd = os.open(
                    frame.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=image_fd,
                )
                opened = os.fstat(frame_fd)
                if not stat.S_ISREG(opened.st_mode) or not _same_identity(
                    opened, source_identity
                ):
                    raise WorkflowError("待退回规范图片在打开时被替换")

                try:
                    os.mkdir(RETRY_ARCHIVE_DIR, mode=0o755, dir_fd=image_fd)
                    _fsync_fd_best_effort(image_fd)
                except FileExistsError:
                    pass
                archive_fd, archive_identity = _open_pinned_directory_at(
                    image_fd,
                    RETRY_ARCHIVE_DIR,
                    "退回归档",
                )
                archive_name = _retry_image_archive_name(valid_index)
                if not _rename_noreplace(
                    image_fd,
                    frame.name,
                    archive_fd,
                    archive_name,
                ):
                    raise WorkflowError("退回归档文件名冲突，拒绝覆盖")
                moved = True
                _after_retry_image_moved(
                    frame,
                    image_dir / RETRY_ARCHIVE_DIR / archive_name,
                )
                archived = os.stat(
                    archive_name,
                    dir_fd=archive_fd,
                    follow_symlinks=False,
                )
                opened = os.fstat(frame_fd)
                if not _retry_image_matches(
                    archived, source_identity
                ) or not _retry_image_matches(opened, source_identity):
                    raise WorkflowError("规范图片退回归档后身份不匹配")
                _fsync_fd_best_effort(archive_fd)
                _fsync_fd_best_effort(image_fd)
                verify_directory_chain()
                _verify_pinned_entry(
                    image_fd,
                    RETRY_ARCHIVE_DIR,
                    archive_fd,
                    archive_identity,
                    "退回归档",
                )

            task["frames"].pop(str(valid_index), None)
            task["image_baseline"] = {}
            task["stage"] = predecessor
            task["updated_at"] = _timestamp_now()
            expected_committed_state = state_value
            verify_directory_chain()
            save_state_atomic(config, state_value)
            return task
        except BaseException as exc:
            state_committed = _state_was_committed(
                config,
                expected_committed_state,
            )
            if moved and not state_committed:
                try:
                    verify_directory_chain()
                    _rollback_retry_image(
                        image_dir,
                        image_fd,
                        image_identity,
                        archive_fd,
                        archive_identity,
                        archive_name,
                        frame.name,
                        frame_fd,
                        source_identity,
                    )
                    moved = False
                except WorkflowError as rollback_exc:
                    raise WorkflowError(
                        f"图片退回失败且规范图片回滚失败：{rollback_exc}"
                    ) from exc
            if isinstance(exc, WorkflowError):
                raise
            if isinstance(exc, OSError):
                raise WorkflowError(f"无法退回规范图片：{exc}") from exc
            raise
        finally:
            if frame_fd is not None:
                os.close(frame_fd)
            if archive_fd is not None:
                os.close(archive_fd)
            if image_fd is not None:
                os.close(image_fd)
            if task_fd is not None:
                os.close(task_fd)
            if date_fd is not None:
                os.close(date_fd)
            if done_fd is not None:
                os.close(done_fd)


def save_state_atomic(config: Config, state_value: dict) -> None:
    _validate_root_relationship(config)
    _validate_state(config, state_value)
    parent = config.state_file.parent
    temporary_name = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        parent_info = parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise WorkflowError("状态目录必须是真实目录")
        if config.state_file.exists() or config.state_file.is_symlink():
            current = config.state_file.lstat()
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise WorkflowError("状态文件必须是普通文件且不能是符号链接")
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{config.state_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(
                state_value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, config.state_file)
        temporary_name = None
        _fsync_directory(parent)
    except WorkflowError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WorkflowError(f"无法原子保存状态：{exc}") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


@contextmanager
def _workflow_lock(config: Config):
    parent = config.state_file.parent
    parent_fd = lock_fd = None
    locked = False
    try:
        try:
            parent.mkdir(parents=True, exist_ok=True)
            parent_fd, parent_identity = _open_pinned_directory(parent, "状态目录")
            lock_name = f".{config.state_file.name}.lock"
            lock_fd = os.open(
                lock_name,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode):
                raise WorkflowError("工作流锁必须是普通文件")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            locked = True
            _verify_pinned_path(parent, parent_fd, parent_identity, "状态目录")
        except WorkflowError:
            raise
        except OSError as exc:
            raise WorkflowError(f"无法获取工作流锁：{exc}") from exc
        yield
    finally:
        if locked and lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)


@contextmanager
def _archive_lock(config: Config):
    _, done_root = _validate_root_relationship(config)
    lock_root = done_root.parent / ".swimwear-tiktok-video-workflow"
    parent_fd = lock_fd = None
    locked = False
    try:
        try:
            lock_root.mkdir(mode=0o700, exist_ok=True)
            parent_fd, parent_identity = _open_pinned_directory(
                lock_root, "归档锁目录"
            )
            lock_flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                lock_fd = os.open(
                    "archive.lock",
                    lock_flags,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                # Two brand-new workflows may race while the shared lock directory
                # is first becoming visible. Re-pin it once before failing closed.
                os.close(parent_fd)
                parent_fd, parent_identity = _open_pinned_directory(
                    lock_root, "归档锁目录"
                )
                lock_fd = os.open(
                    "archive.lock",
                    lock_flags,
                    0o600,
                    dir_fd=parent_fd,
                )
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode):
                raise WorkflowError("归档锁必须是普通文件")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            locked = True
            _verify_pinned_path(lock_root, parent_fd, parent_identity, "归档锁目录")
        except WorkflowError:
            raise
        except OSError as exc:
            raise WorkflowError(f"无法获取归档锁：{exc}") from exc
        yield
    finally:
        if locked and lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _rmtree_at(
    parent_fd: int, name: str, expected_identity: tuple[int, int] | None = None
) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    identity = (info.st_dev, info.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise WorkflowError(f"拒绝删除身份不匹配的目录项：{name}")
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        opened = os.fstat(child_fd)
        if (opened.st_dev, opened.st_ino) != identity:
            raise WorkflowError(f"目录项在删除前被替换：{name}")
        with os.scandir(child_fd) as entries:
            child_names = [entry.name for entry in entries]
        for child_name in child_names:
            child_info = os.stat(
                child_name, dir_fd=child_fd, follow_symlinks=False
            )
            _rmtree_at(
                child_fd,
                child_name,
                (child_info.st_dev, child_info.st_ino),
            )
    finally:
        os.close(child_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != identity:
        raise WorkflowError(f"目录项在最终删除前被替换：{name}")
    os.rmdir(name, dir_fd=parent_fd)


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "写入归档文件失败")
        offset += written


def _copy_regular_file_anchored(
    source_parent_fd: int,
    destination_parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    source_fd = destination_fd = None
    try:
        source_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_parent_fd,
        )
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise WorkflowError(f"商品文件在复制前被替换：{name}")
        destination_fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(opened.st_mode),
            dir_fd=destination_parent_fd,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
        after = os.fstat(source_fd)
        if (
            not _same_identity(after, opened)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise WorkflowError(f"商品文件在复制期间发生变化：{name}")
        os.fsync(destination_fd)
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)


def _copy_tree_anchored(
    source_fd: int, destination_parent_fd: int, destination_name: str
) -> None:
    destination_fd = None
    created = False
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISDIR(source_info.st_mode):
            raise WorkflowError("归档源必须是固定的真实目录")
        os.mkdir(
            destination_name,
            mode=stat.S_IMODE(source_info.st_mode),
            dir_fd=destination_parent_fd,
        )
        created = True
        destination_fd = os.open(
            destination_name, _directory_flags(), dir_fd=destination_parent_fd
        )
        with os.scandir(source_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            entry_info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry_info.st_mode):
                raise WorkflowError(f"商品树内不能包含符号链接：{name}")
            if stat.S_ISDIR(entry_info.st_mode):
                child_fd = None
                try:
                    child_fd = os.open(name, _directory_flags(), dir_fd=source_fd)
                    opened = os.fstat(child_fd)
                    if not _same_identity(opened, entry_info):
                        raise WorkflowError(f"商品目录在复制前被替换：{name}")
                    _copy_tree_anchored(child_fd, destination_fd, name)
                    current = os.stat(
                        name, dir_fd=source_fd, follow_symlinks=False
                    )
                    if not _same_identity(current, opened):
                        raise WorkflowError(f"商品目录在复制期间被替换：{name}")
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
            elif stat.S_ISREG(entry_info.st_mode):
                _copy_regular_file_anchored(
                    source_fd, destination_fd, name, entry_info
                )
            else:
                raise WorkflowError(f"商品树包含不支持的特殊文件：{name}")
    except BaseException:
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None
        if created:
            _rmtree_at(destination_parent_fd, destination_name)
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)


def _rename_noreplace(
    source_fd: int, source_name: str, target_fd: int, target_name: str
) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source_name)
    encoded_target = os.fsencode(target_name)
    if sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "renameatx_np 不可用")
        result = rename(
            source_fd,
            ctypes.c_char_p(encoded_source),
            target_fd,
            ctypes.c_char_p(encoded_target),
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "renameat2 不可用")
        result = rename(
            source_fd,
            ctypes.c_char_p(encoded_source),
            target_fd,
            ctypes.c_char_p(encoded_target),
            0x00000001,
        )
    else:
        raise OSError(errno.ENOTSUP, "当前平台不支持目录无覆盖发布")
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        return False
    raise OSError(error_number, os.strerror(error_number))


def _fsync_fd_best_effort(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        pass


def _unlink_at(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _write_pending_at(
    done_fd: int,
    workflow_id: str,
    target_name: str,
    task: dict,
    base_state_digest: str,
) -> str:
    pending_name = _pending_name(workflow_id, target_name)
    temporary_name = f".{pending_name}.tmp-{uuid.uuid4().hex}"
    descriptor = None
    try:
        try:
            os.stat(pending_name, dir_fd=done_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise WorkflowError(f"目标已有待恢复记录：{target_name}")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=done_fd,
        )
        payload = json.dumps(
            {
                "version": 1,
                "phase": "prepared",
                "base_state_digest": base_state_digest,
                "workflow_id": workflow_id,
                "target_name": target_name,
                "target_identity": None,
                "task": task,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_all(descriptor, payload + b"\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if not _rename_noreplace(done_fd, temporary_name, done_fd, pending_name):
            raise WorkflowError(f"目标已有待恢复记录：{target_name}")
        _fsync_fd_best_effort(done_fd)
        return pending_name
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _unlink_at(done_fd, temporary_name)


def _mark_pending_published_at(
    done_fd: int,
    pending_name: str,
    workflow_id: str,
    target_name: str,
    task: dict,
    target_identity: tuple[int, int],
) -> None:
    temporary_name = f".{pending_name}.tmp-{uuid.uuid4().hex}"
    descriptor = None
    try:
        expected = os.stat(pending_name, dir_fd=done_fd, follow_symlinks=False)
        record = _read_json_at(done_fd, pending_name, "pending 记录")
        (
            recorded_target,
            recorded_task,
            phase,
            identity,
            base_state_digest,
        ) = _validate_pending_record(record, pending_name, workflow_id)
        current = os.stat(pending_name, dir_fd=done_fd, follow_symlinks=False)
        if not _same_identity(current, expected):
            raise WorkflowError("pending 记录在发布阶段更新前被替换")
        if (
            recorded_target != target_name
            or recorded_task["task_dir"] != task["task_dir"]
            or phase != "prepared"
            or identity is not None
        ):
            raise WorkflowError("pending 记录不是当前 prepared 事务")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=done_fd,
        )
        payload = json.dumps(
            {
                "version": 1,
                "phase": "published",
                "base_state_digest": base_state_digest,
                "workflow_id": workflow_id,
                "target_name": target_name,
                "target_identity": {
                    "dev": target_identity[0],
                    "ino": target_identity[1],
                },
                "task": task,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_all(descriptor, payload + b"\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current = os.stat(pending_name, dir_fd=done_fd, follow_symlinks=False)
        if not _same_identity(current, expected):
            raise WorkflowError("pending 记录在原子更新前被替换")
        os.replace(
            temporary_name,
            pending_name,
            src_dir_fd=done_fd,
            dst_dir_fd=done_fd,
        )
        _fsync_fd_best_effort(done_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _unlink_at(done_fd, temporary_name)


def _real_directory_at(parent_fd: int, name: str, label: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkflowError(f"无法检查{label}：{exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkflowError(f"{label}已存在且不是真实目录，拒绝覆盖")


def _ensure_date_directory_at(
    done_fd: int, date_name: str
) -> tuple[int, os.stat_result, bool]:
    if DATE_DIR_PATTERN.fullmatch(date_name) is None or not _valid_iso_date(date_name):
        raise WorkflowError("归档日期目录名非法")
    created = False
    try:
        os.mkdir(date_name, mode=0o700, dir_fd=done_fd)
        created = True
        _fsync_fd_best_effort(done_fd)
    except FileExistsError:
        pass
    date_fd, date_identity = _open_pinned_directory_at(
        done_fd, date_name, "日期目录"
    )
    return date_fd, date_identity, created


def _next_task_name_at(date_fd: int, product_name: str) -> str:
    if not _valid_product_name(product_name):
        raise WorkflowError("商品货号非法")
    highest_index = 0
    with os.scandir(date_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        match = TASK_DIR_PATTERN.fullmatch(name)
        if (
            match is None
            or int(match.group(1)) < 1
            or not _valid_product_name(match.group(2))
        ):
            continue
        info = os.stat(name, dir_fd=date_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise WorkflowError(f"产品归档目录不能是符号链接：{name}")
        if not stat.S_ISDIR(info.st_mode):
            if match.group(2) == product_name:
                return name
            continue
        highest_index = max(highest_index, int(match.group(1)))
        if match.group(2) == product_name:
            return name
    next_index = highest_index + 1
    if next_index > 999:
        raise WorkflowError("当天产品归档编号已超过 999")
    return f"{next_index:03d}-{product_name}"


def _open_archive_target_parent_at(
    done_fd: int, target_name: str
) -> tuple[int, os.stat_result, str, str]:
    date_name, task_name, _ = _parse_archive_target(target_name)
    date_fd, date_identity = _open_pinned_directory_at(
        done_fd, date_name, "日期目录"
    )
    return date_fd, date_identity, date_name, task_name


def _validate_reused_target(date_fd: int, task_name: str) -> None:
    _real_directory_at(date_fd, task_name, "并发任务目标")
    target_fd = os.open(task_name, _directory_flags(), dir_fd=date_fd)
    try:
        _real_directory_at(target_fd, "图片制作", "并发任务图片目录")
        _real_directory_at(target_fd, "视频制作", "并发任务视频目录")
    finally:
        os.close(target_fd)


def _new_task(product_name: str, source: Path, target: Path) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    resolved_target = target.resolve(strict=False)
    return {
        "product_name": product_name,
        "source_dir": str(source.resolve(strict=True)),
        "task_dir": str(resolved_target),
        "image_dir": str(resolved_target / "图片制作"),
        "video_dir": str(resolved_target / "视频制作"),
        "stage": "selected",
        "reference": None,
        "image_baseline": {},
        "image_attempts": {},
        "video_baseline": {},
        "video_attempts": {},
        "frames": {},
        "videos": [],
        "merge": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _create_task(
    config: Config,
    product: Path,
    today: date,
    base_state_digest: str | None = None,
    *,
    task_local_state: bool = False,
    direct_product: bool = False,
    image_mode: str | None = None,
) -> _ArchiveCreation:
    if type(today) is not date:
        raise WorkflowError("任务日期非法")
    if type(task_local_state) is not bool:
        raise WorkflowError("任务独立状态参数非法")
    if type(direct_product) is not bool:
        raise WorkflowError("指定产品参数非法")
    catalog, done_root = _validate_root_relationship(config)
    product_path = Path(product).expanduser()
    if not product_path.is_absolute():
        product_path = Path.cwd() / product_path
    source_date_name = None
    try:
        product_parent = product_path.parent.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"无法解析商品目录项：{exc}") from exc
    if direct_product:
        if product_parent != catalog:
            raise WorkflowError("指定产品目录必须直接位于商品目录中")
        product_name = _direct_product_name(product_path)
    elif product_parent == catalog and PRODUCT_PATTERN.match(product_path.name):
        product_name = product_path.name
    else:
        try:
            nested_catalog = product_path.parent.parent.resolve(strict=True)
        except OSError as exc:
            raise WorkflowError(f"无法解析链接商品目录项：{exc}") from exc
        source_match = TASK_DIR_PATTERN.fullmatch(product_path.name)
        source_date_name = product_path.parent.name
        if (
            nested_catalog != catalog
            or DATE_DIR_PATTERN.fullmatch(source_date_name) is None
            or not _valid_iso_date(source_date_name)
            or source_match is None
            or int(source_match.group(1)) < 1
            or not _valid_product_name(source_match.group(2))
        ):
            raise WorkflowError("只能归档商品目录中的顶层旧产品或日期目录下的链接产品")
        _assert_real_directory(product_path.parent, "链接日期目录")
        product_name = source_match.group(2)
    product_info = _lstat(product_path, "商品")
    if stat.S_ISLNK(product_info.st_mode):
        raise WorkflowError(f"归档入口拒绝商品符号链接：{product_path}")
    if not stat.S_ISDIR(product_info.st_mode):
        raise WorkflowError(f"商品必须是真实目录：{product_path}")
    if (
        not isinstance(base_state_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", base_state_digest) is None
    ):
        raise WorkflowError("归档事务缺少合法 base state digest")

    catalog_fd = source_parent_fd = done_fd = date_fd = product_fd = temporary_fd = None
    date_name = today.isoformat()
    task_name = temporary_name = pending_name = None
    owned_date = False
    archive_ready = False
    try:
        catalog_fd, catalog_identity = _open_pinned_directory(catalog, "商品目录")
        done_fd, done_identity = _open_pinned_directory(done_root, "已做目录")
        product_parent_fd = catalog_fd
        if source_date_name is not None:
            source_parent_fd, source_parent_identity = _open_pinned_directory_at(
                catalog_fd,
                source_date_name,
                "链接日期目录",
            )
            product_parent_fd = source_parent_fd
        try:
            product_fd = os.open(
                product_path.name,
                _directory_flags(),
                dir_fd=product_parent_fd,
            )
        except OSError as exc:
            raise WorkflowError(f"无法固定商品目录：{product_path}：{exc}") from exc
        product_identity = os.fstat(product_fd)

        def verify_source() -> None:
            _verify_pinned_path(catalog, catalog_fd, catalog_identity, "商品目录")
            if source_parent_fd is not None:
                _verify_pinned_entry(
                    catalog_fd,
                    source_date_name,
                    source_parent_fd,
                    source_parent_identity,
                    "链接日期目录",
                )
            _verify_pinned_entry(
                product_parent_fd,
                product_path.name,
                product_fd,
                product_identity,
                "商品目录",
            )

        verify_source()
        _verify_pinned_path(done_root, done_fd, done_identity, "已做目录")

        date_fd, date_identity, owned_date = _ensure_date_directory_at(
            done_fd, date_name
        )
        _verify_pinned_entry(
            done_fd, date_name, date_fd, date_identity, "日期目录"
        )
        task_name = _next_task_name_at(date_fd, product_name)
        target_name = f"{date_name}/{task_name}"
        target = done_root / date_name / task_name
        task = _new_task(product_name, product_path, target)
        if image_mode is not None:
            task["image_mode"] = image_mode
        state_config = (
            _config_for_task(config, target) if task_local_state else config
        )
        workflow_id = _workflow_id(state_config)
        try:
            existing = os.stat(task_name, dir_fd=date_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                raise WorkflowError(f"目标已存在且不能覆盖：{target}")
            _validate_reused_target(date_fd, task_name)
            pending_name = _write_pending_at(
                done_fd,
                workflow_id,
                target_name,
                task,
                base_state_digest,
            )
            archive_ready = True
            target_identity = (existing.st_dev, existing.st_ino)
            current = os.stat(
                task_name, dir_fd=date_fd, follow_symlinks=False
            )
            if (current.st_dev, current.st_ino) != target_identity:
                raise WorkflowError("复用目标在 pending 发布前被替换")
            _after_target_published_before_pending_update(task)
            current = os.stat(
                task_name, dir_fd=date_fd, follow_symlinks=False
            )
            if (current.st_dev, current.st_ino) != target_identity:
                raise WorkflowError("复用目标在 pending 更新前被替换")
            _mark_pending_published_at(
                done_fd,
                pending_name,
                workflow_id,
                target_name,
                task,
                target_identity,
            )
            return _ArchiveCreation(
                task=task,
                pending_name=pending_name,
                target_name=target_name,
                target_identity=target_identity,
                owned_target=False,
                owned_date=owned_date,
            )

        pending_name = _write_pending_at(
            done_fd,
            workflow_id,
            target_name,
            task,
            base_state_digest,
        )
        temporary_name = f".{task_name}.tmp-{uuid.uuid4().hex}"
        os.mkdir(temporary_name, mode=0o700, dir_fd=date_fd)
        temporary_fd = os.open(temporary_name, _directory_flags(), dir_fd=date_fd)

        verify_source()
        _verify_pinned_path(done_root, done_fd, done_identity, "已做目录")
        _verify_pinned_entry(
            done_fd, date_name, date_fd, date_identity, "日期目录"
        )
        _copy_tree_anchored(product_fd, temporary_fd, "payload")
        payload_fd = os.open("payload", _directory_flags(), dir_fd=temporary_fd)
        try:
            os.mkdir("图片制作", dir_fd=payload_fd)
            os.mkdir("视频制作", dir_fd=payload_fd)
        finally:
            os.close(payload_fd)

        verify_source()
        _verify_pinned_path(done_root, done_fd, done_identity, "已做目录")
        _verify_pinned_entry(
            done_fd, date_name, date_fd, date_identity, "日期目录"
        )
        published = _rename_noreplace(temporary_fd, "payload", date_fd, task_name)
        if published:
            archive_ready = True
        else:
            _validate_reused_target(date_fd, task_name)
            archive_ready = True
        _real_directory_at(date_fd, task_name, "任务目标")
        target_info = os.stat(task_name, dir_fd=date_fd, follow_symlinks=False)
        target_identity = (target_info.st_dev, target_info.st_ino)
        _after_target_published_before_pending_update(task)
        current = os.stat(task_name, dir_fd=date_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != target_identity
        ):
            raise WorkflowError("发布目标在 pending 更新前被替换")
        _mark_pending_published_at(
            done_fd,
            pending_name,
            workflow_id,
            target_name,
            task,
            target_identity,
        )
        return _ArchiveCreation(
            task=task,
            pending_name=pending_name,
            target_name=target_name,
            target_identity=target_identity,
            owned_target=published,
            owned_date=owned_date,
        )
    except WorkflowError:
        raise
    except (OSError, shutil.Error) as exc:
        raise WorkflowError(f"归档产品失败：{exc}") from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None and date_fd is not None:
            try:
                _rmtree_at(date_fd, temporary_name)
            except (OSError, WorkflowError):
                pass
        if pending_name is not None and not archive_ready and done_fd is not None:
            _unlink_at(done_fd, pending_name)
            _fsync_fd_best_effort(done_fd)
        if date_fd is not None:
            os.close(date_fd)
        if owned_date and not archive_ready and done_fd is not None:
            try:
                os.rmdir(date_name, dir_fd=done_fd)
                _fsync_fd_best_effort(done_fd)
            except OSError:
                pass
        if product_fd is not None:
            os.close(product_fd)
        if source_parent_fd is not None:
            os.close(source_parent_fd)
        if done_fd is not None:
            os.close(done_fd)
        if catalog_fd is not None:
            os.close(catalog_fd)


def _pending_names_at(done_fd: int, workflow_id: str) -> list[str]:
    with os.scandir(done_fd) as entries:
        return sorted(
            entry.name
            for entry in entries
            if (match := PENDING_PATTERN.fullmatch(entry.name))
            and match.group(1) == workflow_id
        )


def _clear_pending_creation(config: Config, creation: _ArchiveCreation) -> None:
    done_fd = date_fd = None
    try:
        workflow_id = _workflow_id(config)
        done_fd, _ = _open_pinned_directory(config.done_root, "已做目录")
        record = _read_json_at(done_fd, creation.pending_name, "pending 记录")
        target_name, task, phase, identity, _ = _validate_pending_record(
            record, creation.pending_name, workflow_id
        )
        if (
            target_name != creation.target_name
            or task["task_dir"] != creation.task["task_dir"]
            or phase != "published"
            or identity != creation.target_identity
        ):
            raise WorkflowError("pending 记录与当前归档事务不一致")
        date_fd, _, _, task_name = _open_archive_target_parent_at(
            done_fd, creation.target_name
        )
        current = os.stat(
            task_name, dir_fd=date_fd, follow_symlinks=False
        )
        if (current.st_dev, current.st_ino) != creation.target_identity:
            raise WorkflowError("当前归档目标身份已变化，保留 pending 记录")
        _unlink_at(done_fd, creation.pending_name)
        _fsync_fd_best_effort(done_fd)
    finally:
        if date_fd is not None:
            os.close(date_fd)
        if done_fd is not None:
            os.close(done_fd)


def _rollback_creation(config: Config, creation: _ArchiveCreation) -> None:
    done_fd = date_fd = None
    date_name = None
    try:
        workflow_id = _workflow_id(config)
        done_fd, _ = _open_pinned_directory(config.done_root, "已做目录")
        record = _read_json_at(done_fd, creation.pending_name, "pending 记录")
        target_name, task, phase, identity, _ = _validate_pending_record(
            record, creation.pending_name, workflow_id
        )
        if (
            target_name != creation.target_name
            or task["task_dir"] != creation.task["task_dir"]
            or phase != "published"
            or identity != creation.target_identity
        ):
            raise WorkflowError("pending 记录与待回滚归档不一致")
        date_fd, _, date_name, task_name = _open_archive_target_parent_at(
            done_fd, creation.target_name
        )
        current = os.stat(
            task_name, dir_fd=date_fd, follow_symlinks=False
        )
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise WorkflowError("待回滚归档已被替换，保留 pending 记录")
        if (current.st_dev, current.st_ino) != creation.target_identity:
            raise WorkflowError("待回滚归档身份已变化，保留 pending 记录")
        if not creation.owned_target:
            _unlink_at(done_fd, creation.pending_name)
            _fsync_fd_best_effort(done_fd)
            return

        _before_rollback_quarantine(creation)
        quarantine_name = f".swimwear-quarantine-{uuid.uuid4().hex}"
        try:
            moved = _rename_noreplace(
                date_fd,
                task_name,
                date_fd,
                quarantine_name,
            )
        except OSError as exc:
            raise WorkflowError(f"无法隔离待回滚归档：{exc}") from exc
        if not moved:
            raise WorkflowError("回滚 quarantine 名称冲突，保留 pending 记录")
        quarantined = os.stat(
            quarantine_name, dir_fd=date_fd, follow_symlinks=False
        )
        quarantined_identity = (quarantined.st_dev, quarantined.st_ino)
        if quarantined_identity != creation.target_identity:
            try:
                restored = _rename_noreplace(
                    date_fd,
                    quarantine_name,
                    date_fd,
                    task_name,
                )
            except OSError as exc:
                raise WorkflowError(
                    f"quarantine 中是外国目录且无法恢复原名：{exc}"
                ) from exc
            if not restored:
                raise WorkflowError("quarantine 中是外国目录且原名已被占用")
            raise WorkflowError("待回滚名称已被外国目录替换，已安全恢复并保留 pending")
        _rmtree_at(date_fd, quarantine_name, creation.target_identity)
        _unlink_at(done_fd, creation.pending_name)
        _fsync_fd_best_effort(done_fd)
    finally:
        if date_fd is not None:
            os.close(date_fd)
        if creation.owned_date and date_name is not None and done_fd is not None:
            try:
                os.rmdir(date_name, dir_fd=done_fd)
                _fsync_fd_best_effort(done_fd)
            except OSError:
                pass
        if done_fd is not None:
            os.close(done_fd)


def _recover_pending_locked(config: Config, state_value: dict) -> dict:
    done_fd = None
    try:
        workflow_id = _workflow_id(config)
        done_fd, _ = _open_pinned_directory(config.done_root, "已做目录")
        for pending_name in _pending_names_at(done_fd, workflow_id):
            record = _read_json_at(done_fd, pending_name, "pending 记录")
            (
                target_name,
                task,
                phase,
                identity,
                base_state_digest,
            ) = _validate_pending_record(record, pending_name, workflow_id)
            if phase != "published" or identity is None:
                raise WorkflowError(
                    "pending 发布阶段为 prepared 或缺少目标身份，拒绝自动恢复"
                )
            date_name, task_name, _ = _parse_archive_target(target_name)
            try:
                date_info = os.stat(
                    date_name, dir_fd=done_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                _unlink_at(done_fd, pending_name)
                _fsync_fd_best_effort(done_fd)
                continue
            if stat.S_ISLNK(date_info.st_mode) or not stat.S_ISDIR(date_info.st_mode):
                raise WorkflowError("pending 日期目录已被替换，拒绝自动恢复")
            date_fd, _ = _open_pinned_directory_at(
                done_fd, date_name, "pending 日期目录"
            )
            try:
                try:
                    target_info = os.stat(
                        task_name, dir_fd=date_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    _unlink_at(done_fd, pending_name)
                    _fsync_fd_best_effort(done_fd)
                    continue
                if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(
                    target_info.st_mode
                ):
                    raise WorkflowError("pending 目标已被替换，拒绝自动恢复")
                if (target_info.st_dev, target_info.st_ino) != identity:
                    raise WorkflowError("pending 目标身份不一致，拒绝自动恢复")
                _validate_active_task(config, task)
            finally:
                os.close(date_fd)
            active = state_value["active_task"]
            active_is_new_task = active is not None and (
                active["product_name"] == task["product_name"]
                and active["task_dir"] == task["task_dir"]
            )
            if active_is_new_task:
                pass
            elif _state_digest(state_value) == base_state_digest:
                recovered = {"version": 1, "active_task": task}
                save_state_atomic(config, recovered)
                state_value = recovered
            else:
                raise WorkflowError("pending 任务与当前活动任务冲突")
            _unlink_at(done_fd, pending_name)
            _fsync_fd_best_effort(done_fd)
        return state_value
    finally:
        if done_fd is not None:
            os.close(done_fd)


def _recover_pending(config: Config, state_value: dict) -> dict:
    with _archive_lock(config):
        return _recover_pending_locked(config, state_value)


def _after_archive_published(task: dict) -> None:
    return None


def _after_target_published_before_pending_update(task: dict) -> None:
    return None


def _before_rollback_quarantine(creation: _ArchiveCreation) -> None:
    return None


def _commit_creation(
    config: Config, state_value: dict, creation: _ArchiveCreation
) -> dict:
    committed = False
    try:
        _after_archive_published(creation.task)
        state_value["active_task"] = creation.task
        save_state_atomic(config, state_value)
        committed = True
        _clear_pending_creation(config, creation)
        return creation.task
    except WorkflowError:
        if not committed:
            _rollback_creation(config, creation)
        raise


def _candidate_product_name(catalog: Path, product_path: Path) -> str:
    if product_path.parent.resolve(strict=True) == catalog.resolve(strict=True):
        if not PRODUCT_PATTERN.match(product_path.name):
            raise WorkflowError(f"旧商品目录名非法：{product_path.name}")
        return product_path.name
    match = TASK_DIR_PATTERN.fullmatch(product_path.name)
    if match is None or not _valid_product_name(match.group(2)):
        raise WorkflowError(f"链接商品目录名非法：{product_path.name}")
    return match.group(2)


def _direct_product_name(product_path: Path) -> str:
    """Return a stable product name for a user-selected source directory."""
    name = product_path.name
    archived = TASK_DIR_PATTERN.fullmatch(name)
    if archived is not None and int(archived.group(1)) >= 1:
        name = archived.group(2)
    if not _valid_product_name(name):
        raise WorkflowError(f"指定产品目录名非法：{product_path.name}")
    return name


def _selection_candidates(config: Config) -> list[Path]:
    completed = completed_product_names(config.done_root)
    completed_keys = {_canonical_product_key(name) for name in completed}
    if config.link_history is not None:
        products = discover_link_products(config.catalog, config.link_history)
    else:
        products = discover_products(config.catalog)
    return [
        path
        for path in products
        if _canonical_product_key(_candidate_product_name(config.catalog, path))
        not in completed_keys
    ]


def select_product(config: Config, rng, today: date) -> dict:
    if type(today) is not date:
        raise WorkflowError("任务日期非法")
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        active = state_value["active_task"]
        if active is not None and active["stage"] != "merged":
            return active
        with _archive_lock(config):
            candidates = _selection_candidates(config)
            if not candidates:
                raise WorkflowError("没有未做产品")
            creation = _create_task(
                config,
                rng.choice(candidates),
                today,
                _state_digest(state_value),
            )
            return _commit_creation(config, state_value, creation)


def start_independent_product(config: Config, rng, today: date) -> dict:
    """Create a new workflow whose state lives inside its task directory."""
    if type(today) is not date:
        raise WorkflowError("任务日期非法")
    _validate_root_relationship(config)
    with _archive_lock(config):
        candidates = _selection_candidates(config)
        if not candidates:
            raise WorkflowError("没有未做产品")
        creation = _create_task(
            config,
            rng.choice(candidates),
            today,
            _state_digest(default_state()),
            task_local_state=True,
        )
        task_config = _config_for_task(config, Path(creation.task["task_dir"]))
        with _workflow_lock(task_config):
            return _commit_creation(task_config, default_state(), creation)


def start_exact_product(config: Config, product: Path, today: date, image_mode="website") -> dict:
    """Start a task for one exact user-selected product directory."""
    if type(today) is not date:
        raise WorkflowError("任务日期非法")
    if image_mode not in {"website", "api-first"}:
        raise WorkflowError("生图模式非法")
    product_path = Path(product).expanduser()
    if not product_path.is_absolute():
        product_path = Path.cwd() / product_path
    _assert_real_directory(product_path, "指定产品目录")
    try:
        product_path = product_path.resolve(strict=True)
        parent = product_path.parent.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"无法解析指定产品目录：{exc}") from exc
    _assert_real_directory(product_path, "指定产品目录")
    config = replace(config, source_product=product_path)
    _validate_root_relationship(config)
    if parent != config.catalog.expanduser().resolve(strict=True):
        raise WorkflowError("指定产品目录必须直接位于商品目录中")
    product_name = _direct_product_name(product_path)
    with _archive_lock(config):
        completed_keys = {
            _canonical_product_key(name)
            for name in completed_product_names(config.done_root)
        }
        if _canonical_product_key(product_name) in completed_keys:
            raise WorkflowError(f"指定产品已有任务，请恢复已有任务：{product_name}")
        creation = _create_task(
            config,
            product_path,
            today,
            _state_digest(default_state()),
            task_local_state=True,
            direct_product=True,
            image_mode=image_mode,
        )
        task_config = _config_for_task(config, Path(creation.task["task_dir"]))
        with _workflow_lock(task_config):
            return _commit_creation(task_config, default_state(), creation)


def replace_product(config: Config, rng, today: date, force: bool = False) -> dict:
    if type(today) is not date:
        raise WorkflowError("任务日期非法")
    if type(force) is not bool:
        raise WorkflowError("强制换品参数非法")
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        active = state_value["active_task"]
        if (
            active is None
            or active["stage"] == "merged"
            or (
                not force
                and active["stage"] not in {"selected", "reference-ready"}
            )
        ):
            raise WorkflowError("当前阶段不能换产品")
        with _archive_lock(config):
            candidates = [
                path
                for path in _selection_candidates(config)
                if _canonical_product_key(
                    _candidate_product_name(config.catalog, path)
                )
                != _canonical_product_key(active["product_name"])
            ]
            if not candidates:
                raise WorkflowError("没有可替换的未做产品")
            creation = _create_task(
                config,
                rng.choice(candidates),
                today,
                _state_digest(state_value),
            )
            return _commit_creation(config, state_value, creation)


def replace_with_independent_product(
    config: Config,
    rng,
    today: date,
    force: bool = False,
) -> dict:
    """Start a replacement in a new task-local state without changing the old task."""
    if type(today) is not date:
        raise WorkflowError("任务日期非法")
    if type(force) is not bool:
        raise WorkflowError("强制换品参数非法")
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        active = state_value["active_task"]
        if (
            active is None
            or active["stage"] == "merged"
            or (
                not force
                and active["stage"] not in {"selected", "reference-ready"}
            )
        ):
            raise WorkflowError("当前阶段不能换产品")
        with _archive_lock(config):
            candidates = [
                path
                for path in _selection_candidates(config)
                if _canonical_product_key(
                    _candidate_product_name(config.catalog, path)
                )
                != _canonical_product_key(active["product_name"])
            ]
            if not candidates:
                raise WorkflowError("没有可替换的未做产品")
            creation = _create_task(
                config,
                rng.choice(candidates),
                today,
                _state_digest(default_state()),
                task_local_state=True,
            )
            task_config = _config_for_task(config, Path(creation.task["task_dir"]))
            with _workflow_lock(task_config):
                return _commit_creation(task_config, default_state(), creation)


def migrate_state_to_task(config: Config, remove_source: bool = False) -> dict:
    if type(remove_source) is not bool:
        raise WorkflowError("删除旧状态参数非法")
    _validate_root_relationship(config)
    with _workflow_lock(config):
        state_value = _recover_pending(config, load_state(config))
        task = state_value["active_task"]
        if task is None:
            raise WorkflowError("旧状态中没有可迁移的活动任务")
        target_config = _config_for_task(config, Path(task["task_dir"]))
        source = config.state_file.expanduser().resolve(strict=False)
        target = target_config.state_file
        if source == target:
            return {
                "active_task": task,
                "state_file": str(target),
                "migrated": False,
                "source_removed": False,
            }
        with _workflow_lock(target_config):
            if target.exists() or target.is_symlink():
                existing = load_state(target_config)
                if existing != state_value:
                    raise WorkflowError("任务目录中已有不同的工作流进度")
            else:
                save_state_atomic(target_config, state_value)
            if load_state(target_config) != state_value:
                raise WorkflowError("任务目录中的工作流进度校验失败")

        source_removed = False
        if remove_source:
            expected = _real_regular_file(config.state_file, "旧状态文件")
            current = config.state_file.lstat()
            if not _same_identity(current, expected):
                raise WorkflowError("旧状态文件在迁移期间被替换")
            config.state_file.unlink()
            _fsync_directory(config.state_file.parent)
            source_removed = True
        return {
            "active_task": task,
            "state_file": str(target),
            "migrated": True,
            "source_removed": source_removed,
        }


def _status_state(config: Config) -> dict:
    _validate_root_relationship(config)
    with _workflow_lock(config):
        return _recover_pending(config, load_state(config))


class ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"错误：命令参数无效：{message}\n")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD 格式") from exc


def _print_json(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(description="Swimwear TikTok video workflow")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--source-product", type=Path, default=None)
    parser.add_argument("--link-history", type=Path, default=None)
    parser.add_argument("--done-root", type=Path, default=DEFAULT_DONE_ROOT)
    parser.add_argument("--task-dir", type=Path, default=None)
    parser.add_argument(
        "--state-file",
        type=Path,
        metavar="LEGACY_STATE_FILE",
        default=None,
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    exact = commands.add_parser("start-product")
    exact.add_argument("source", type=Path)
    exact.add_argument("--date", type=_parse_date, default=None)
    exact.add_argument("--image-mode", choices=("website", "api-first"), default="website")
    for name in ("select", "replace"):
        command = commands.add_parser(name)
        command.add_argument("--date", type=_parse_date, default=None)
        command.add_argument("--seed", type=int, default=None)
        if name == "replace":
            command.add_argument("--force", action="store_true")
    reference = commands.add_parser("register-reference")
    reference.add_argument("source", type=Path)
    reference.add_argument(
        "--crop",
        type=int,
        nargs=4,
        metavar=("X", "Y", "WIDTH", "HEIGHT"),
        default=None,
    )
    for name in ("mark-image-prompt", "retry-image"):
        command = commands.add_parser(name)
        command.add_argument("index", type=int)
    image_attempt = commands.add_parser("claim-image-attempt")
    image_attempt.add_argument("index", type=int)
    image_attempt.add_argument("surface", choices=tuple(IMAGE_ATTEMPT_LIMITS))
    ingest = commands.add_parser("ingest-image")
    ingest.add_argument("index", type=int)
    ingest.add_argument("--confirmed-source", type=Path, default=None)
    video_attempt = commands.add_parser("claim-video-attempt")
    video_attempt.add_argument("index", type=int)
    for name in ("redo-videos", "mark-video-prompts", "ingest-videos", "merge"):
        commands.add_parser(name)
    manual_video = commands.add_parser("register-manual-video")
    manual_video.add_argument("source", type=Path)
    commands.add_parser("validate-product-copy")
    commands.add_parser("validate-publish-copy")
    migrate = commands.add_parser("migrate-state")
    migrate.add_argument("--remove-source", action="store_true")
    return parser


def _base_config(args, state_file: Path) -> Config:
    link_history = args.link_history
    if link_history is None and args.catalog == DEFAULT_CATALOG and args.command != "start-product":
        link_history = DEFAULT_LINK_HISTORY
    return Config(
        catalog=args.catalog,
        done_root=args.done_root,
        state_file=state_file,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        link_history=link_history,
        source_product=args.source_product,
    )


def _task_result_with_state_file(result: dict, state_file: Path) -> dict:
    output = dict(result)
    output["state_file"] = str(state_file.expanduser().resolve(strict=False))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.task_dir is not None and args.state_file is not None:
        parser.error("--task-dir 与 --state-file 不能同时使用")

    task_local_cli = args.task_dir is not None
    if args.command == "start-product":
        if args.task_dir is not None or args.state_file is not None:
            parser.error("start-product 不能传 --task-dir 或 --state-file")
        source = args.source.expanduser().absolute()
        if source.is_symlink() or not source.is_dir():
            parser.error("指定产品必须是真实目录")
        args.source = source.resolve()
        args.source_product = args.source
        args.catalog = args.source.parent
        args.link_history = None
        selected_state_file = DEFAULT_STATE_FILE
        args.done_root = args.done_root.expanduser().absolute()
        resolved_output = args.done_root.resolve(strict=False)
        if (resolved_output == args.source or resolved_output.is_relative_to(args.source)
                or args.source.is_relative_to(resolved_output)):
            parser.error("产品目录与已做目录不能重叠或互相包含")
        try:
            args.done_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            parser.error(f"无法创建已做目录：{exc}")
    elif args.command == "select":
        if args.task_dir is not None:
            parser.error("select 会创建新独立任务，不能传 --task-dir")
        selected_state_file = args.state_file or DEFAULT_STATE_FILE
    elif args.command == "migrate-state":
        if args.task_dir is not None or args.state_file is None:
            parser.error("migrate-state 必须只传 --state-file 指定旧状态")
        selected_state_file = args.state_file
    elif args.task_dir is not None:
        selected_state_file = task_state_file(args.task_dir)
    elif args.state_file is not None:
        selected_state_file = args.state_file
    else:
        cwd_state = task_state_file(Path.cwd())
        if not cwd_state.is_file():
            parser.error(
                f"请使用 --task-dir 指定任务目录，或在包含 "
                f"{TASK_STATE_FILE_NAME} 的任务目录中运行"
            )
        selected_state_file = cwd_state

    config = _base_config(args, selected_state_file)
    try:
        if args.command == "status":
            result = _status_state(config)
        elif args.command == "start-product":
            result = start_exact_product(config, args.source, args.date or date.today(), args.image_mode)
            selected_state_file = task_state_file(Path(result["task_dir"]))
            result = _task_result_with_state_file(result, selected_state_file)
        elif args.command in {"select", "replace"}:
            rng = random.Random(args.seed)
            today = args.date or date.today()
            if args.command == "select":
                if args.state_file is None:
                    result = start_independent_product(config, rng, today)
                    selected_state_file = task_state_file(Path(result["task_dir"]))
                    result = _task_result_with_state_file(
                        result,
                        selected_state_file,
                    )
                else:
                    result = select_product(config, rng, today)
            elif task_local_cli:
                result = replace_with_independent_product(
                    config,
                    rng,
                    today,
                    force=args.force,
                )
                selected_state_file = task_state_file(Path(result["task_dir"]))
                result = _task_result_with_state_file(result, selected_state_file)
            else:
                result = replace_product(config, rng, today, force=args.force)
        elif args.command == "register-reference":
            crop = tuple(args.crop) if args.crop is not None else None
            result = register_reference(config, args.source, crop)
        elif args.command == "mark-image-prompt":
            result = mark_image_prompt(config, args.index)
        elif args.command == "claim-image-attempt":
            result = claim_image_attempt(config, args.index, args.surface)
        elif args.command == "ingest-image":
            result = ingest_image(config, args.index, args.confirmed_source)
        elif args.command == "retry-image":
            result = retry_image(config, args.index)
        elif args.command == "claim-video-attempt":
            result = claim_video_attempt(config, args.index)
        elif args.command == "redo-videos":
            result = redo_videos(config)
        elif args.command == "mark-video-prompts":
            result = mark_video_prompts(config)
        elif args.command == "ingest-videos":
            result = ingest_videos(config)
        elif args.command == "merge":
            result = merge_videos(config)
        elif args.command == "register-manual-video":
            result = register_manual_video(config, args.source)
        elif args.command == "validate-product-copy":
            result = validate_product_copy(config)
        elif args.command == "validate-publish-copy":
            result = validate_publish_copy(config)
        elif args.command == "migrate-state":
            result = migrate_state_to_task(config, args.remove_source)
        else:
            raise WorkflowError("未知工作流命令")
        if task_local_cli and args.command not in {"select", "replace"}:
            result = _task_result_with_state_file(result, selected_state_file)
        _print_json(result)
        return 0
    except WorkflowError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误：工作流操作失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
