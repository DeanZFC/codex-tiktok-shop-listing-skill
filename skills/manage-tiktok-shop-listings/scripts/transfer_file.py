#!/usr/bin/env python3
"""Copy one browser upload/download between WSL and Windows without overwriting."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat


def transfer(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_absolute() or not destination.is_absolute():
        raise ValueError("请提供源和目标文件的绝对路径")
    for path, label in ((source.parent, "源"), (destination.parent, "目标")):
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{label}路径父目录不存在或含有符号链接：{current}")
    if source == destination:
        raise ValueError("源文件和目标文件不能相同")
    # Refuse special files and symlinks; the destination must be a fresh candidate.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise ValueError("只允许复制普通文件")
        with destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
    return {"source": str(source), "destination": str(destination), "bytes": destination.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(transfer(args.source, args.destination), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(2, f"文件传递失败：{exc}；目标已存在时请使用新的候选文件名。\n")


if __name__ == "__main__":
    raise SystemExit(main())
