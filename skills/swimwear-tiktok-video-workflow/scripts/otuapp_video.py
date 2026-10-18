#!/usr/bin/env python3

import argparse
import getpass
import hashlib
import http.client
import json
import mimetypes
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import certifi


API_ROOT = "https://zexapi.com/v1/videos"
DEFAULT_MODEL = "veo_3_1-fast-fl-hd"
DEFAULT_SIZE = "1080x1920"
DEFAULT_KEYCHAIN_SERVICE = "codex-otuapp-video-api"
DEFAULT_KEYCHAIN_ACCOUNT = os.environ.get("OTUAPP_KEYCHAIN_ACCOUNT", getpass.getuser())
TERMINAL_SUCCESS = {"completed", "succeeded", "success"}
TERMINAL_FAILURE = {"failed", "cancelled", "canceled", "rejected", "error"}
CHUNK_SIZE = 256 * 1024
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class VideoAPIError(Exception):
    pass


def _print_json(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _keychain_token(service: str, account: str) -> str:
    completed = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            service,
            "-a",
            account,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise VideoAPIError(
            f"macOS 钥匙串中没有可用令牌：service={service}, account={account}"
        )
    token = completed.stdout.strip()
    if not token or re.fullmatch(r"sk-[A-Za-z0-9_-]+", token) is None:
        raise VideoAPIError("钥匙串中的视频 API 令牌格式无效")
    return token


def _json_request(
    method: str,
    url: str,
    token: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 120,
) -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "codex-swimwear-video-workflow/1.0",
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=TLS_CONTEXT
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise VideoAPIError(f"视频 API HTTP {exc.code}：{detail[:1000]}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise VideoAPIError(f"视频 API 请求失败：{exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VideoAPIError("视频 API 返回的不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise VideoAPIError("视频 API 返回结构无效")
    return payload


def _multipart_body(
    prompt: str,
    start: Path,
    end: Path,
    model: str,
    size: str,
) -> tuple[bytes, str]:
    boundary = f"----CodexVideo{secrets.token_hex(16)}"
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    def file_field(name: str, path: Path) -> None:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{path.name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mime}\r\n\r\n".encode("ascii"),
                path.read_bytes(),
                b"\r\n",
            ]
        )

    field("model", model)
    field("size", size)
    field("prompt", prompt)
    file_field("input_reference", start)
    file_field("input_reference", end)
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _task_id(payload: dict) -> str:
    for key in ("id", "task_id", "taskId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    nested = payload.get("data")
    if isinstance(nested, dict):
        return _task_id(nested)
    raise VideoAPIError("视频 API 创建响应缺少任务 ID")


def _status_value(payload: dict) -> str:
    for key in ("status", "state"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.casefold()
    nested = payload.get("data")
    if isinstance(nested, dict):
        return _status_value(nested)
    return "unknown"


def _progress_value(payload: dict):
    for key in ("progress", "percent", "percentage"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    nested = payload.get("data")
    if isinstance(nested, dict):
        return _progress_value(nested)
    return None


def submit(
    token: str,
    prompt_file: Path,
    start: Path,
    end: Path,
    model: str,
    size: str,
) -> dict:
    for path, label in (
        (prompt_file, "视频提示词"),
        (start, "首帧"),
        (end, "尾帧"),
    ):
        if not path.is_file() or path.is_symlink():
            raise VideoAPIError(f"{label}必须是真实普通文件：{path}")
    try:
        prompt = prompt_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise VideoAPIError(f"无法读取视频提示词：{exc}") from exc
    if not prompt:
        raise VideoAPIError("视频提示词不能为空")
    body, content_type = _multipart_body(prompt, start, end, model, size)
    payload = _json_request(
        "POST",
        API_ROOT,
        token,
        body=body,
        content_type=content_type,
        timeout=180,
    )
    task_id = _task_id(payload)
    return {
        "id": task_id,
        "status": _status_value(payload),
        "progress": _progress_value(payload),
    }


def task_status(token: str, task_id: str) -> dict:
    return _json_request("GET", f"{API_ROOT}/{task_id}", token, timeout=60)


def wait_for_task(
    token: str,
    task_id: str,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_marker = None
    while True:
        payload = task_status(token, task_id)
        status = _status_value(payload)
        progress = _progress_value(payload)
        marker = (status, progress)
        if marker != last_marker:
            _print_json({"id": task_id, "status": status, "progress": progress})
            last_marker = marker
        if status in TERMINAL_SUCCESS:
            return payload
        if status in TERMINAL_FAILURE:
            detail = payload.get("error") or payload.get("message") or payload
            raise VideoAPIError(f"视频任务失败：{detail}")
        if time.monotonic() >= deadline:
            raise VideoAPIError(f"等待视频任务超时：{task_id}")
        time.sleep(poll_seconds)


def _find_video_url(value) -> str | None:
    if isinstance(value, dict):
        preferred = (
            "video_url",
            "videoUrl",
            "output_url",
            "outputUrl",
            "download_url",
            "downloadUrl",
            "url",
        )
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
        for nested in value.values():
            candidate = _find_video_url(nested)
            if candidate is not None:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_video_url(nested)
            if candidate is not None:
                return candidate
    return None


def _download_authenticated_content(token: str, task_id: str, destination: Path) -> None:
    request = urllib.request.Request(
        f"{API_ROOT}/{task_id}/content",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "video/mp4,application/octet-stream,application/json",
            "User-Agent": "codex-swimwear-video-workflow/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=180, context=TLS_CONTEXT) as response:
        content_type = (response.headers.get("Content-Type") or "").casefold()
        if "json" in content_type:
            payload = json.loads(response.read().decode("utf-8"))
            url = _find_video_url(payload)
            if url is None:
                raise VideoAPIError("视频内容接口未返回可下载文件")
            _download_public_url(url, destination)
            return
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)


def _fetch_range(url: str, start: int, end: int, output: Path) -> None:
    expected = end - start + 1
    last_error = None
    output.unlink(missing_ok=True)
    no_progress_failures = 0
    for _ in range(64):
        downloaded = output.stat().st_size if output.exists() else 0
        if downloaded == expected:
            return
        if downloaded > expected:
            raise VideoAPIError(f"视频分段超过预期长度：{start}-{end}")
        current = start + downloaded
        request = urllib.request.Request(
            url,
            headers={
                "Range": f"bytes={current}-{end}",
                "User-Agent": "codex-swimwear-video-workflow/1.0",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=120, context=TLS_CONTEXT
            ) as response:
                status = getattr(response, "status", None)
                content_range = response.headers.get("Content-Range")
                if status != 206 or not (content_range or "").startswith(
                    f"bytes {current}-"
                ):
                    raise VideoAPIError(
                        f"视频分段续传响应非法：{current}-{end}"
                    )
                try:
                    data = response.read()
                except http.client.IncompleteRead as exc:
                    data = exc.partial
                    if data:
                        with output.open("ab") as handle:
                            handle.write(data)
                    raise
                with output.open("ab") as handle:
                    handle.write(data)
            downloaded = output.stat().st_size
            if downloaded != expected:
                raise VideoAPIError(
                    f"分段长度不匹配：{start}-{end}，实际 {downloaded}"
                )
            return
        except (
            OSError,
            urllib.error.URLError,
            http.client.HTTPException,
            VideoAPIError,
        ) as exc:
            last_error = exc
            progressed = output.stat().st_size if output.exists() else 0
            if progressed > downloaded:
                no_progress_failures = 0
                time.sleep(0.1)
                continue
            no_progress_failures += 1
            if no_progress_failures >= 12:
                break
            time.sleep(min(2 ** no_progress_failures, 10))
    raise VideoAPIError(f"视频分段下载失败：{start}-{end}：{last_error}")


def _download_public_url(url: str, destination: Path) -> None:
    probe = urllib.request.Request(
        url,
        headers={
            "Range": "bytes=0-0",
            "User-Agent": "codex-swimwear-video-workflow/1.0",
        },
    )
    try:
        with urllib.request.urlopen(probe, timeout=60, context=TLS_CONTEXT) as response:
            first = response.read()
            status = getattr(response, "status", None)
            content_range = response.headers.get("Content-Range")
    except (OSError, urllib.error.URLError) as exc:
        raise VideoAPIError(f"无法探测视频下载地址：{exc}") from exc

    if status == 200 and len(first) > 1:
        destination.write_bytes(first)
        return
    match = re.fullmatch(r"bytes 0-0/(\d+)", content_range or "")
    if match is None:
        raise VideoAPIError("视频存储不支持可靠的字节范围下载")
    total = int(match.group(1))
    if total <= 0:
        raise VideoAPIError("视频存储返回的文件长度无效")

    with tempfile.TemporaryDirectory(prefix="otuapp-video-parts-") as temporary:
        parts_dir = Path(temporary)
        ranges = []
        for index, start in enumerate(range(0, total, CHUNK_SIZE)):
            end = min(start + CHUNK_SIZE - 1, total - 1)
            ranges.append((index, start, end, parts_dir / f"{index:05d}.part"))
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_fetch_range, url, start, end, part): index
                for index, start, end, part in ranges
            }
            for future in as_completed(futures):
                future.result()
        with destination.open("wb") as output:
            for _, start, end, part in ranges:
                if part.stat().st_size != end - start + 1:
                    raise VideoAPIError(f"视频分段不完整：{part.name}")
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    if destination.stat().st_size != total:
        raise VideoAPIError("视频分段合并后的长度不匹配")


def download_task(
    token: str,
    task_id: str,
    task_payload: dict,
    output: Path,
) -> dict:
    output = output.expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise VideoAPIError(f"输出文件已存在，拒绝覆盖：{output}")
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(8)}.part")
    try:
        try:
            _download_authenticated_content(token, task_id, temporary)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, VideoAPIError):
            url = _find_video_url(task_payload)
            if url is None:
                refreshed = task_status(token, task_id)
                url = _find_video_url(refreshed)
            if url is None:
                raise VideoAPIError("任务已完成，但没有可用的视频下载地址")
            _download_public_url(url, temporary)
        if temporary.stat().st_size <= 0:
            raise VideoAPIError("下载得到的文件为空")
        os.replace(temporary, output)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return {
            "id": task_id,
            "output": str(output),
            "bytes": output.stat().st_size,
            "sha256": digest,
        }
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Otuapp video API client")
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    parser.add_argument("--keychain-account", default=DEFAULT_KEYCHAIN_ACCOUNT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keychain-check")
    submit_parser = commands.add_parser("submit")
    generate_parser = commands.add_parser("generate")
    for command in (submit_parser, generate_parser):
        command.add_argument("--prompt-file", type=Path, required=True)
        command.add_argument("--start", type=Path, required=True)
        command.add_argument("--end", type=Path, required=True)
        command.add_argument("--model", default=DEFAULT_MODEL)
        command.add_argument("--size", default=DEFAULT_SIZE)
    generate_parser.add_argument("--output", type=Path, required=True)
    generate_parser.add_argument("--poll-seconds", type=float, default=20)
    generate_parser.add_argument("--timeout-seconds", type=float, default=2400)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("task_id")
    wait_parser = commands.add_parser("wait")
    wait_parser.add_argument("task_id")
    wait_parser.add_argument("--poll-seconds", type=float, default=20)
    wait_parser.add_argument("--timeout-seconds", type=float, default=2400)
    download_parser = commands.add_parser("download")
    download_parser.add_argument("task_id")
    download_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        token = _keychain_token(args.keychain_service, args.keychain_account)
        if args.command == "keychain-check":
            _print_json(
                {
                    "account": args.keychain_account,
                    "service": args.keychain_service,
                    "available": True,
                }
            )
            return 0
        if args.command in {"submit", "generate"}:
            created = submit(
                token,
                args.prompt_file,
                args.start,
                args.end,
                args.model,
                args.size,
            )
            _print_json(created)
            if args.command == "submit":
                return 0
            payload = wait_for_task(
                token,
                created["id"],
                args.poll_seconds,
                args.timeout_seconds,
            )
            _print_json(download_task(token, created["id"], payload, args.output))
            return 0
        if args.command == "status":
            payload = task_status(token, args.task_id)
            _print_json(
                {
                    "id": args.task_id,
                    "status": _status_value(payload),
                    "progress": _progress_value(payload),
                    "video_url_available": _find_video_url(payload) is not None,
                }
            )
            return 0
        if args.command == "wait":
            payload = wait_for_task(
                token,
                args.task_id,
                args.poll_seconds,
                args.timeout_seconds,
            )
            _print_json(
                {
                    "id": args.task_id,
                    "status": _status_value(payload),
                    "progress": _progress_value(payload),
                }
            )
            return 0
        if args.command == "download":
            payload = task_status(token, args.task_id)
            if _status_value(payload) not in TERMINAL_SUCCESS:
                raise VideoAPIError("视频任务尚未完成，不能下载")
            _print_json(download_task(token, args.task_id, payload, args.output))
            return 0
        raise VideoAPIError("未知命令")
    except VideoAPIError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
