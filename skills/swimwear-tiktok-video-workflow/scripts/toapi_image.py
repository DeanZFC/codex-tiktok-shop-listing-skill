#!/usr/bin/env python3
"""Download one image URL copied from the website; no login/API generation."""
import argparse
import io
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request
from PIL import Image


def download(url, output):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("图片链接必须使用 HTTPS")
    request = urllib.request.Request(url, headers={"accept": "image/*"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise ValueError("拒绝非 HTTPS 重定向")
        content = response.read(50 * 1024 * 1024 + 1)
    if len(content) > 50 * 1024 * 1024:
        raise ValueError("图片超过 50 MiB")
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
    output = output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        handle.write(content)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="下载网页已生成图片")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("download-url")
    command.add_argument("url")
    command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = download(args.url, args.output)
        print(json.dumps({"downloaded": str(result)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError):
        print("下载失败：检查登录/链接有效期、图片内容或目标是否已存在；不要重新提交生成。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
