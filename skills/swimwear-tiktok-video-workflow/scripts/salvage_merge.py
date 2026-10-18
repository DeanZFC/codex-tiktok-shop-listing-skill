#!/usr/bin/env python3

import argparse
import json
import math
import os
import stat
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path


class SalvageMergeError(RuntimeError):
    pass


def _finite_time(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("time must be finite and non-negative")
    return parsed


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise SalvageMergeError(f"cannot run {label}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise SalvageMergeError(f"{label} failed: {detail}")
    return completed


def _regular_video(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SalvageMergeError(f"cannot read {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SalvageMergeError(f"{label} must be a regular file, not a symlink")
    if path.suffix.casefold() not in {".mp4", ".mov", ".m4v", ".webm"}:
        raise SalvageMergeError(f"{label} must be MP4, MOV, M4V, or WEBM")
    return path.resolve(strict=True)


def _probe(ffprobe: str, path: Path) -> dict:
    completed = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration:"
                "stream=index,codec_type,codec_name,width,height,pix_fmt,"
                "r_frame_rate,avg_frame_rate,sample_rate,channels"
            ),
            "-of",
            "json",
            str(path),
        ],
        "ffprobe",
    )
    try:
        payload = json.loads(completed.stdout)
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SalvageMergeError(f"invalid ffprobe output for {path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise SalvageMergeError(f"invalid duration for {path}")
    video = next(
        (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )
    if video is None:
        raise SalvageMergeError(f"video stream missing in {path}")
    if audio is None:
        raise SalvageMergeError(f"audio stream missing in {path}")
    return {"duration": duration, "video": video, "audio": audio}


def _frame_rate(video: dict) -> str:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = video.get(key)
        if not isinstance(value, str):
            continue
        try:
            rate = Fraction(value)
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return value
    raise SalvageMergeError("first video has no valid frame rate")


def _trim_range(start: float, end: float | None, duration: float, label: str) -> tuple[float, float]:
    actual_end = duration if end is None else end
    if start >= actual_end:
        raise SalvageMergeError(f"{label} start must be before end")
    if actual_end > duration + 0.02:
        raise SalvageMergeError(f"{label} end exceeds source duration")
    return start, min(actual_end, duration)


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def merge(args: argparse.Namespace) -> dict:
    first = _regular_video(args.first, "first input")
    second = _regular_video(args.second, "second input")
    if first == second:
        raise SalvageMergeError("first and second inputs must be different files")

    output = args.output.expanduser().absolute()
    if output.suffix.casefold() != ".mp4":
        raise SalvageMergeError("output must use the .mp4 extension")
    if output.exists() or output.is_symlink():
        raise SalvageMergeError("output already exists; refusing to overwrite")
    if not output.parent.is_dir():
        raise SalvageMergeError("output parent directory does not exist")

    first_media = _probe(args.ffprobe, first)
    second_media = _probe(args.ffprobe, second)
    first_start, first_end = _trim_range(
        args.first_start, args.first_end, first_media["duration"], "first trim"
    )
    second_start, second_end = _trim_range(
        args.second_start, args.second_end, second_media["duration"], "second trim"
    )
    first_duration = first_end - first_start
    second_duration = second_end - second_start
    transition = args.transition
    transition_style = args.transition_style
    if transition < 0.1 or transition > 1.0:
        raise SalvageMergeError("transition must be between 0.1 and 1.0 seconds")
    if transition >= min(first_duration, second_duration):
        raise SalvageMergeError("transition must be shorter than both trimmed clips")

    first_video = first_media["video"]
    try:
        width = int(first_video["width"])
        height = int(first_video["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SalvageMergeError("first video has invalid dimensions") from exc
    width -= width % 2
    height -= height % 2
    if width <= 0 or height <= 0:
        raise SalvageMergeError("first video has invalid dimensions")
    frame_rate = _frame_rate(first_video)
    offset = first_duration - transition
    expected_duration = first_duration + second_duration - transition
    audio_delay_ms = int(round(offset * 1000))

    video_common = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={frame_rate},format=yuv420p,settb=AVTB"
    )
    filter_complex = (
        f"[0:v:0]trim=start={_fmt(first_start)}:end={_fmt(first_end)},"
        f"setpts=PTS-STARTPTS,{video_common}[v0];"
        f"[1:v:0]trim=start={_fmt(second_start)}:end={_fmt(second_end)},"
        f"setpts=PTS-STARTPTS,{video_common}[v1];"
        f"[v0][v1]xfade=transition={transition_style}:duration={_fmt(transition)}:"
        f"offset={_fmt(offset)}[v];"
        f"[0:a:0]atrim=start={_fmt(first_start)}:end={_fmt(first_end)},"
        "asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
        f"afade=t=out:st={_fmt(offset)}:d={_fmt(transition)}[a0];"
        f"[1:a:0]atrim=start={_fmt(second_start)}:end={_fmt(second_end)},"
        "asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
        f"adelay=delays={audio_delay_ms}:all=1[a1];"
        "[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95[a]"
    )

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".salvage-merge-") as temp_name:
        temporary = Path(temp_name) / "merged.mp4"
        command = [
            args.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(first),
            "-i",
            str(second),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            _fmt(expected_duration),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        _run(command, "ffmpeg salvage merge")
        output_media = _probe(args.ffprobe, temporary)
        if abs(output_media["duration"] - expected_duration) > 0.15:
            raise SalvageMergeError("merged duration does not match the edited timeline")
        output_video = output_media["video"]
        output_audio = output_media["audio"]
        if (
            output_video.get("codec_name") != "h264"
            or output_video.get("pix_fmt") != "yuv420p"
            or int(output_video.get("width", 0)) != width
            or int(output_video.get("height", 0)) != height
            or output_audio.get("codec_name") != "aac"
            or int(output_audio.get("sample_rate", 0)) != 48000
        ):
            raise SalvageMergeError("merged output does not meet H.264/AAC media requirements")
        try:
            os.link(temporary, output)
        except OSError as exc:
            raise SalvageMergeError(f"cannot publish output without overwrite: {exc}") from exc

    return {
        "first": str(first),
        "first_trim": [first_start, first_end],
        "second": str(second),
        "second_trim": [second_start, second_end],
        "transition": transition,
        "transition_style": transition_style,
        "duration": output_media["duration"],
        "output": str(output.resolve(strict=True)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trim two approved clips and merge them with a short video transition."
    )
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first-start", type=_finite_time, default=0.0)
    parser.add_argument("--first-end", type=_finite_time)
    parser.add_argument("--second-start", type=_finite_time, default=0.0)
    parser.add_argument("--second-end", type=_finite_time)
    parser.add_argument("--transition", type=_finite_time, default=0.35)
    parser.add_argument(
        "--transition-style",
        choices=(
            "hblur",
            "wipeleft",
            "wiperight",
            "slideleft",
            "slideright",
            "smoothleft",
            "smoothright",
            "smoothup",
            "smoothdown",
            "fade",
        ),
        default="hblur",
        help=(
            "video transition; hblur, wipes, and slides avoid full-frame "
            "double-person ghosting, while fade is only suitable for closely "
            "matched framing"
        ),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = merge(args)
    except SalvageMergeError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
