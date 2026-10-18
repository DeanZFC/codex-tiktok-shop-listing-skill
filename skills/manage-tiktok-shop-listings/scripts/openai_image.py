#!/usr/bin/env python3
"""Generate workflow images through an OpenAI-compatible relay API."""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Iterable

from openai import APIError, OpenAI
from PIL import Image, ImageOps, UnidentifiedImageError


API_KEY_ENV = "OPENAI_API_KEY"
BASE_URL_ENV = "OPENAI_BASE_URL"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_SIZE = "720x1280"
DEFAULT_QUALITY = "low"
RATIO_TOLERANCE = 0.015


class ImageApiError(RuntimeError):
    pass


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _settings() -> tuple[str, str]:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    base_url = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    missing = [
        name
        for name, value in ((API_KEY_ENV, api_key), (BASE_URL_ENV, base_url))
        if not value
    ]
    if missing:
        raise ImageApiError(f"缺少环境变量：{', '.join(missing)}")
    if not base_url.startswith(("https://", "http://")):
        raise ImageApiError(f"{BASE_URL_ENV} 必须是 http(s) URL")
    return api_key, base_url


def _parse_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ImageApiError(f"尺寸格式必须是 WIDTHxHEIGHT：{value}") from exc
    if width < 256 or height < 256 or width > 4096 or height > 4096:
        raise ImageApiError("输出宽高必须在 256 到 4096 像素之间")
    return width, height


def _api_size_for(width: int, height: int) -> str:
    if height > width:
        return "1024x1536"
    if width > height:
        return "1536x1024"
    return "1024x1024"


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _prepare_reference(source: Path, destination: Path, size: tuple[int, int]) -> None:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ImageApiError(f"参考图不存在：{source}")
    try:
        with Image.open(source) as opened:
            image = _to_rgb(ImageOps.exif_transpose(opened)).copy()
    except (OSError, UnidentifiedImageError) as exc:
        raise ImageApiError(f"参考图无法解码：{source}") from exc

    target_width, target_height = size
    image.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (target_width, target_height), "white")
    left = (target_width - image.width) // 2
    top = (target_height - image.height) // 2
    canvas.paste(image, (left, top))
    canvas.save(destination, format="PNG", optimize=True)


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        prompt = args.prompt_file.expanduser().read_text(encoding="utf-8")
    else:
        prompt = args.prompt or ""
    prompt = prompt.strip()
    if not prompt:
        raise ImageApiError("提示词不能为空；使用 --prompt 或 --prompt-file")
    return prompt


def _download_url(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"accept": "image/*", "user-agent": "Codex-Swimwear-Workflow/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except OSError as exc:
        raise ImageApiError(f"下载 API 返回图片失败：{exc}") from exc


def _response_image_bytes(item: object, timeout: float) -> bytes:
    encoded = getattr(item, "b64_json", None)
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageApiError("API 返回了无效的 base64 图片") from exc
    url = getattr(item, "url", None)
    if url:
        return _download_url(url, timeout)
    raise ImageApiError("API 响应既没有 b64_json，也没有图片 URL")


def _normalize_output(
    content: bytes, destination: Path, size: tuple[int, int]
) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(content)) as opened:
            image = _to_rgb(ImageOps.exif_transpose(opened)).copy()
    except (OSError, UnidentifiedImageError) as exc:
        raise ImageApiError("API 返回内容不是可解码图片") from exc

    source_size = image.size
    target_width, target_height = size
    source_ratio = image.width / image.height
    target_ratio = target_width / target_height
    if abs(source_ratio - target_ratio) / target_ratio > RATIO_TOLERANCE:
        raise ImageApiError(
            "API 返回图片比例不符合目标尺寸："
            f"{image.width}x{image.height}，目标 {target_width}x{target_height}"
        )
    if image.size != size:
        image = ImageOps.fit(
            image,
            size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    image.save(destination, format="PNG", optimize=True)
    return source_size


def _client(api_key: str, base_url: str, timeout: float) -> OpenAI:
    # One workflow claim must map to exactly one billable API submission.
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
    )


def _check(args: argparse.Namespace) -> int:
    api_key, base_url = _settings()
    client = _client(api_key, base_url, args.request_timeout)
    try:
        models = client.models.list()
    except APIError as exc:
        raise ImageApiError(f"中转站连接检查失败：{exc}") from exc
    model_ids = sorted(item.id for item in models.data)
    if args.model not in model_ids:
        raise ImageApiError(f"中转站未返回所需模型：{args.model}")
    _print_json(
        {
            "ok": True,
            "base_url": base_url,
            "model": args.model,
            "available_image_models": [
                model_id for model_id in model_ids if "image" in model_id.casefold()
            ],
        }
    )
    return 0


def _generate(args: argparse.Namespace) -> int:
    api_key, base_url = _settings()
    prompt = _read_prompt(args)
    output_size = _parse_size(args.size)
    api_size = _api_size_for(*output_size)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        _print_json(
            {
                "base_url": base_url,
                "model": args.model,
                "reference": str(args.reference.expanduser().resolve()),
                "size": args.size,
                "api_size": api_size,
                "quality": args.quality,
                "count": args.count,
                "output_dir": str(output_dir),
            }
        )
        return 0

    client = _client(api_key, base_url, args.request_timeout)
    with tempfile.TemporaryDirectory(prefix="swimwear-image-api-") as temporary:
        prepared = Path(temporary) / "reference-9x16.png"
        _prepare_reference(args.reference, prepared, output_size)
        try:
            with prepared.open("rb") as reference_handle:
                response = client.images.edit(
                    model=args.model,
                    image=reference_handle,
                    prompt=prompt,
                    size=api_size,
                    quality=args.quality,
                    n=args.count,
                )
        except APIError as exc:
            raise ImageApiError(f"中转站生图请求失败：{exc}") from exc

        if not response.data or len(response.data) < args.count:
            raise ImageApiError(
                f"API 返回图片数量不足：期望 {args.count}，实际 {len(response.data or [])}"
            )

        downloaded: list[str] = []
        source_sizes: list[str] = []
        timestamp = time.time_ns()
        for index, item in enumerate(response.data[: args.count], start=1):
            content = _response_image_bytes(item, args.request_timeout)
            target = output_dir / f"relay-{timestamp}-{index:02d}.png"
            temporary_target = output_dir / f".{target.name}.tmp"
            try:
                source_width, source_height = _normalize_output(
                    content, temporary_target, output_size
                )
                os.replace(temporary_target, target)
            finally:
                temporary_target.unlink(missing_ok=True)
            downloaded.append(str(target))
            source_sizes.append(f"{source_width}x{source_height}")

    _print_json(
        {
            "model": args.model,
            "base_url": base_url,
            "requested_size": args.size,
            "api_size": api_size,
            "source_sizes": source_sizes,
            "downloaded": downloaded,
        }
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="中转站 OpenAI 兼容生图工具")
    parser.add_argument("--request-timeout", type=float, default=600)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="检查环境变量、连接和模型")
    check.add_argument("--model", default=DEFAULT_MODEL)

    generate = subparsers.add_parser("generate", help="使用单张参考图生成并下载图片")
    prompt_group = generate.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    generate.add_argument("--reference", type=Path, required=True)
    generate.add_argument("--model", default=DEFAULT_MODEL)
    generate.add_argument("--size", default=DEFAULT_SIZE)
    generate.add_argument(
        "--quality",
        default=DEFAULT_QUALITY,
        choices=("standard", "low", "medium", "high", "auto"),
    )
    generate.add_argument("--count", type=int, default=1, choices=range(1, 5))
    generate.add_argument("--output-dir", type=Path, required=True)
    generate.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check":
            return _check(args)
        if args.command == "generate":
            return _generate(args)
    except (ImageApiError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
