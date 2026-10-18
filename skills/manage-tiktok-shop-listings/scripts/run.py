#!/usr/bin/env python3
"""Unified listing-to-video entry point. Credentials remain in browser sessions."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import listing_workflow

if os.name == "posix":
    import workflow
else:
    workflow = None


DEFAULT_WORKSPACE = Path(
    os.environ.get("TIKTOK_SHOP_WORKSPACE", Path.home() / "TikTokShopWorkspace")
).expanduser()


def doctor() -> int:
    report = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "supported_runtime": os.name == "posix" and sys.version_info >= (3, 10),
        "Pillow": importlib.util.find_spec("PIL") is not None,
        "openai_optional": importlib.util.find_spec("openai") is not None,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "image_api_configured": bool(
            os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL")
        ),
        "browser_login": (
            "Codex 按步骤检查妙手、TikTok Shop、供货文档和 toapi.top；未登录时打开浏览器让用户登录"
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(report[key] for key in ("supported_runtime", "Pillow", "ffmpeg", "ffprobe")) else 2


def ensure_posix(parser: argparse.ArgumentParser) -> None:
    if os.name != "posix" or sys.version_info < (3, 10):
        parser.error(
            "需要 macOS/Linux 的 Python 3.10+；Windows 请运行 scripts/windows.py，通过 WSL2 后端执行"
        )


def config_path(workspace: Path) -> Path:
    return workspace / ".tiktok-shop-listing" / "config.json"


def load_listing_context(workspace: Path) -> dict:
    workspace = workspace.expanduser().resolve()
    path = config_path(workspace)
    config = listing_workflow.load_config(path)
    return listing_workflow.build_context(workspace, path, config)


def bootstrap_product(workspace: Path, product: Path) -> dict:
    workspace = workspace.expanduser().resolve()
    product = product.expanduser().resolve(strict=True)
    workspace.mkdir(parents=True, exist_ok=True)
    path = config_path(workspace)
    created = not path.is_file()
    if created:
        config = listing_workflow.read_json(listing_workflow.example_config_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        listing_workflow.atomic_write_json(path, config)
    raw = json.loads(path.read_text(encoding="utf-8"))
    parent = str(product.parent.resolve())
    catalog_roots = raw.get("catalog_roots")
    if not isinstance(catalog_roots, list):
        raise ValueError("catalog_roots 配置损坏")
    placeholder_only = catalog_roots == ["选品"] and not (workspace / "选品").is_dir()
    if created or placeholder_only:
        raw["catalog_roots"] = [parent]
    elif parent not in catalog_roots:
        raw["catalog_roots"] = [*catalog_roots, parent]
    else:
        return load_listing_context(workspace)
    if raw["catalog_roots"] != catalog_roots:
        availability = raw.setdefault("availability", {})
        availability["document_url"] = ""
        availability["bound_catalog_roots"] = []
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_listing_context(workspace)


def start_listing(
    workspace: Path,
    product: Path,
    product_code: str,
    category: str | None,
    selected_date: str | None,
) -> int:
    product = product.expanduser().resolve(strict=True)
    if not product.is_dir():
        raise ValueError("产品目录不存在")
    context = bootstrap_product(workspace, product)
    return listing_workflow.command_reserve(
        argparse.Namespace(
            source=str(product),
            product_code=product_code,
            aliases=[],
            category=category,
            tags=[],
            date=selected_date,
        ),
        context,
    )


def start_video(workspace: Path, output_dir: Path, image_mode: str) -> int:
    context = load_listing_context(workspace)
    output_dir = output_dir.expanduser().resolve(strict=True)
    with listing_workflow.locked(context):
        history = listing_workflow.load_history(context["history"])
        entry = listing_workflow.find_entry_by_output(history, output_dir, context)
        if entry.get("status") != "tiktok_published":
            raise ValueError("只有确认 tiktok_published 的同一商品才能开始视频制作")
        publication = listing_workflow.validated_publication_record(
            entry, context["config"]
        )
        source = listing_workflow.source_path_from_entry(entry, context)
        task_name = output_dir.name.split("-", 1)[1]
    task = workflow.attach_published_listing(source, output_dir, task_name, image_mode)
    print(
        json.dumps(
            {
                **task,
                "state_file": str(workflow.task_state_file(output_dir)),
                "publication": publication,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def resume_task(directory: Path, operation: list[str]) -> int:
    directory = directory.expanduser().resolve(strict=True)
    state_path = directory / workflow.TASK_STATE_FILE_NAME
    if state_path.is_symlink() or not state_path.is_file():
        raise ValueError("任务进度必须是普通文件")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    active = state["active_task"]
    if Path(active["task_dir"]).resolve() != directory:
        raise ValueError("任务目录与进度文件中的路径不一致")
    source = Path(active["source_dir"]).resolve(strict=True)
    return workflow.main(
        [
            "--catalog", str(source.parent),
            "--done-root", str(directory.parent.parent),
            "--source-product", str(source),
            "--task-dir", str(directory),
            *(operation or ["status"]),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TikTok Shop 商品上架、发布确认与同款视频统一流程")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="本机业务工作区；保存配置、永久历史和上架输出",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    start = commands.add_parser("start", help="从一个单产品目录建立上架任务")
    start.add_argument("product", type=Path)
    start.add_argument("--product-code", required=True)
    start.add_argument("--category")
    start.add_argument("--date", type=listing_workflow.validate_date)
    listing = commands.add_parser("listing", help="透传上架状态机命令")
    listing.add_argument("operation", nargs=argparse.REMAINDER)
    video = commands.add_parser("start-video", help="为已确认发布的同一商品附着视频任务")
    video.add_argument("output_dir", type=Path)
    video.add_argument("--image-mode", choices=("website", "api-first"), default="website")
    task = commands.add_parser("task", help="恢复已发布商品目录中的视频任务")
    task.add_argument("directory", type=Path)
    task.add_argument("operation", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    ensure_posix(parser)
    workspace = args.workspace.expanduser().resolve()
    try:
        if args.command == "start":
            return start_listing(
                workspace, args.product, args.product_code, args.category, args.date
            )
        if args.command == "listing":
            return listing_workflow.main(
                ["--root", str(workspace), *(args.operation or ["history"])]
            )
        if args.command == "start-video":
            return start_video(workspace, args.output_dir, args.image_mode)
        return resume_task(args.directory, args.operation)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        workflow.WorkflowError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
