#!/usr/bin/env python3
"""Render verified supplier specifications as a reusable PNG table."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError:
    Image = ImageDraw = ImageFont = None


WIDTH = 1800
HEIGHT = 2200
ACCENT = "#d64c42"
INK = "#171717"
MUTED = "#666666"
GRID = "#d7d7d3"


def font_candidates(bold: bool) -> list[Path | str]:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if bold:
        return [
            windows / "msyhbd.ttc",
            windows / "segoeuib.ttf",
            Path("/System/Library/Fonts/STHeiti Medium.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            "DejaVuSans-Bold.ttf",
        ]
    return [
        windows / "msyh.ttc",
        windows / "segoeui.ttf",
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        "DejaVuSans.ttf",
    ]


def resolve_font(bold: bool) -> Path | str:
    for candidate in font_candidates(bold):
        try:
            ImageFont.truetype(str(candidate), size=12)
            return candidate
        except OSError:
            continue
    raise OSError("No usable TrueType font was found")


REGULAR_FONT = resolve_font(False) if ImageFont is not None else None
BOLD_FONT = resolve_font(True) if ImageFont is not None else None


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(BOLD_FONT if bold else REGULAR_FONT), size=size)


def text_width(draw: ImageDraw.ImageDraw, value: str, text_font: ImageFont.FreeTypeFont) -> float:
    box = draw.textbbox((0, 0), value, font=text_font)
    return box[2] - box[0]


def fit_font(
    draw: ImageDraw.ImageDraw,
    value: str,
    max_width: int,
    initial: int,
    minimum: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont:
    for size in range(initial, minimum - 1, -1):
        candidate = font(size, bold=bold)
        if text_width(draw, value, candidate) <= max_width:
            return candidate
    return font(minimum, bold=bold)


def centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    value: str,
    text_font: ImageFont.FreeTypeFont,
    fill: str,
) -> None:
    left, top, right, bottom = box
    bbox = draw.textbbox((0, 0), value, font=text_font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text(
        ((left + right - width) / 2, (top + bottom - height) / 2 - bbox[1]),
        value,
        font=text_font,
        fill=fill,
    )


def validate_spec(spec: dict) -> None:
    if not isinstance(spec.get("product_code"), str) or not spec["product_code"].strip():
        raise ValueError("product_code is required")
    if spec.get("unavailable"):
        return
    columns = spec.get("columns")
    rows = spec.get("rows")
    if not isinstance(columns, list) or len(columns) < 2 or not all(
        isinstance(value, str) for value in columns
    ):
        raise ValueError("columns must contain at least two strings")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a non-empty list")
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f"row {index} must contain exactly {len(columns)} values")


def draw_header(draw: ImageDraw.ImageDraw, spec: dict) -> None:
    draw.rectangle((0, 0, WIDTH, 22), fill=ACCENT)
    title = str(spec.get("title") or "PRODUCT SPECIFICATIONS")
    title_font = fit_font(draw, title, 1500, 86, 52, bold=True)
    draw.text((90, 90), title, font=title_font, fill=INK)
    draw.text((90, 220), spec["product_code"], font=font(38, bold=True), fill=ACCENT)
    label = str(spec.get("label") or "VERIFIED SUPPLIER DATA")
    label_font = fit_font(draw, label, 650, 30, 22, bold=True)
    draw.text((1710 - text_width(draw, label, label_font), 145), label, font=label_font, fill="#555555")


def render_unavailable(spec: dict, output: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw_header(draw, spec)
    draw.line((120, 375, 1680, 375), fill="#dedede", width=3)
    draw.rounded_rectangle((120, 620, 1680, 1340), radius=18, fill="#f6f6f4", outline="#dadad7", width=3)
    draw.ellipse((824, 760, 976, 912), fill=ACCENT)
    centered_text(draw, (824, 760, 976, 912), "!", font(82, bold=True), "white")
    message = str(spec.get("unavailable_message") or "SPECIFICATIONS NOT PROVIDED")
    centered_text(draw, (180, 970, 1620, 1060), message, fit_font(draw, message, 1300, 54, 34, True), INK)
    centered_text(
        draw,
        (180, 1075, 1620, 1140),
        "No verified supplier specification table is available for this item.",
        font(34),
        "#555555",
    )
    centered_text(draw, (180, 1140, 1620, 1210), "Do not estimate values.", font(36), "#555555")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)


def render_table(spec: dict, output: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw_header(draw, spec)
    unit = str(spec.get("unit") or "Not specified by supplier")
    draw.rounded_rectangle((90, 340, 1710, 412), radius=8, fill="#fff1ef")
    draw.text((125, 359), f"Unit: {unit}", font=font(31, bold=True), fill="#8d302b")

    columns = [str(value) for value in spec["columns"]]
    rows = [[str(value) for value in row] for row in spec["rows"]]
    x = 90
    y = 470
    table_width = 1620
    row_height = max(72, min(100, 1280 // (len(rows) + 1)))
    first_width = 470 if len(columns) <= 6 else 390
    remaining_width = (table_width - first_width) / (len(columns) - 1)
    widths = [first_width] + [remaining_width] * (len(columns) - 1)

    cursor_x = float(x)
    for index, label in enumerate(columns):
        width = widths[index]
        box = (round(cursor_x), y, round(cursor_x + width), y + row_height)
        draw.rectangle(box, fill=INK)
        centered_text(
            draw,
            box,
            label,
            fit_font(draw, label, round(width - 20), 32 if index else 29, 20, bold=True),
            "white",
        )
        cursor_x += width

    for row_index, row in enumerate(rows):
        row_y = y + row_height * (row_index + 1)
        cursor_x = float(x)
        fill = "#f5f5f2" if row_index % 2 == 0 else "white"
        for col_index, value in enumerate(row):
            width = widths[col_index]
            box = (round(cursor_x), row_y, round(cursor_x + width), row_y + row_height)
            draw.rectangle(box, fill=fill, outline=GRID, width=2)
            cell_font = fit_font(draw, value, round(width - 28), 33 if col_index else 29, 19, col_index == 0)
            if col_index == 0:
                bbox = draw.textbbox((0, 0), value, font=cell_font)
                text_height = bbox[3] - bbox[1]
                draw.text(
                    (box[0] + 22, (box[1] + box[3] - text_height) / 2 - bbox[1]),
                    value,
                    font=cell_font,
                    fill="#232323",
                )
            else:
                centered_text(draw, box, value, cell_font, "#232323")
            cursor_x += width

    foot_y = y + row_height * (len(rows) + 1) + 90
    notes = spec.get("notes") or [
        "Values are reproduced from verified supplier information.",
        "Do not publish units or measurements that the supplier did not provide.",
    ]
    draw.text((90, foot_y), "NOTES", font=font(30, bold=True), fill="#444444")
    for index, note in enumerate(notes):
        draw.text((90, foot_y + 55 + index * 47), str(note), font=font(29), fill=MUTED)

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render supplier specifications as a 1800x2200 PNG")
    parser.add_argument("--input", required=True, help="UTF-8 JSON specification")
    parser.add_argument("--output", required=True, help="PNG output path")
    args = parser.parse_args()
    try:
        if Image is None:
            raise OSError("Pillow is required. Install it with: python -m pip install Pillow")
        spec = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError("Specification root must be an object")
        validate_spec(spec)
        output = Path(args.output).expanduser().resolve()
        if spec.get("unavailable"):
            render_unavailable(spec, output)
        else:
            render_table(spec, output)
        print(json.dumps({"output": str(output), "width": WIDTH, "height": HEIGHT}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
