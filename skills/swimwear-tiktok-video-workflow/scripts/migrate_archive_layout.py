#!/usr/bin/env python3

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


LEGACY_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{3}_.+)$")
TASK_PATTERN = re.compile(r"^(\d{3})-(\d{3}_.+)$")


class MigrationError(Exception):
    pass


def _real_directory(path: Path, label: str) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MigrationError(f"{label}必须是真实目录且不能是符号链接：{path}")


def _state_files(state_root: Path) -> list[Path]:
    paths = []
    default = state_root / "state.json"
    if default.exists() or default.is_symlink():
        paths.append(default)
    threads = state_root / "threads"
    if threads.is_dir() and not threads.is_symlink():
        paths.extend(sorted(threads.glob("*.json")))
    return paths


def _read_state(path: Path) -> tuple[dict, bytes]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MigrationError(f"状态文件必须是普通文件：{path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"状态文件无效：{path}：{exc}") from exc
    if not isinstance(value, dict) or set(value) != {"version", "active_task"}:
        raise MigrationError(f"状态文件结构非法：{path}")
    return value, raw


def _atomic_write(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _replace_prefix(value, old: Path, new: Path):
    if isinstance(value, str):
        path = Path(value)
        try:
            relative = path.relative_to(old)
        except ValueError:
            return value
        return str(new / relative)
    if isinstance(value, dict):
        return {key: _replace_prefix(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_prefix(item, old, new) for item in value]
    return value


def _planned_moves(done_root: Path) -> list[tuple[Path, Path]]:
    legacy = []
    for path in sorted(done_root.iterdir(), key=lambda item: item.name):
        match = LEGACY_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        _real_directory(path, "旧产品归档")
        legacy.append((path, match.group(1), match.group(2)))
    moves = []
    reserved: dict[str, set[int]] = {}
    for _, day, _ in legacy:
        date_dir = done_root / day
        indexes = reserved.setdefault(day, set())
        if date_dir.exists() or date_dir.is_symlink():
            _real_directory(date_dir, "日期目录")
            for child in date_dir.iterdir():
                match = TASK_PATTERN.fullmatch(child.name)
                if match is not None:
                    indexes.add(int(match.group(1)))
    for old, day, product in legacy:
        date_dir = done_root / day
        indexes = reserved.setdefault(day, set())
        index = 1
        while index in indexes:
            index += 1
        if index > 999:
            raise MigrationError(f"日期 {day} 的产品编号已超过 999")
        indexes.add(index)
        new = date_dir / f"{index:03d}-{product}"
        if new.exists() or new.is_symlink():
            raise MigrationError(f"迁移目标已存在：{new}")
        moves.append((old, new))
    return moves


def _resolve_state_target(task_dir: Path, moves: list[tuple[Path, Path]], done_root: Path):
    resolved_task_dir = task_dir.resolve(strict=False)
    for old, new in moves:
        if resolved_task_dir == old.resolve(strict=False):
            lexical_new = task_dir.parent / new.relative_to(done_root)
            return task_dir, lexical_new
    match = LEGACY_PATTERN.fullmatch(task_dir.name)
    if task_dir.parent.resolve(strict=False) == done_root and match is not None:
        date_dir = done_root / match.group(1)
        product = match.group(2)
        candidates = sorted(date_dir.glob(f"[0-9][0-9][0-9]-{product}"))
        if len(candidates) == 1:
            lexical_new = task_dir.parent / candidates[0].relative_to(done_root)
            return task_dir, lexical_new
    return None


def migrate(done_root: Path, state_root: Path, *, dry_run: bool = False) -> dict:
    done_root = done_root.resolve(strict=True)
    state_root = state_root.resolve(strict=True)
    _real_directory(done_root, "已做目录")
    _real_directory(state_root, "状态目录")
    pending = sorted(done_root.glob(".swimwear-pending-*.json"))
    if pending:
        raise MigrationError("存在未完成归档事务，拒绝迁移目录布局")

    lock_path = state_root / ".archive-layout-migration.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        moves = _planned_moves(done_root)
        states = []
        unresolved = []
        for path in _state_files(state_root):
            value, raw = _read_state(path)
            active = value.get("active_task")
            if not isinstance(active, dict):
                states.append((path, value, raw, None))
                continue
            task_dir_value = active.get("task_dir")
            if not isinstance(task_dir_value, str):
                raise MigrationError(f"活动任务缺少 task_dir：{path}")
            resolved = _resolve_state_target(Path(task_dir_value), moves, done_root)
            if resolved is None:
                if Path(task_dir_value).parent == done_root:
                    unresolved.append(str(path))
                states.append((path, value, raw, None))
                continue
            old, new = resolved
            updated = _replace_prefix(value, old, new)
            states.append((path, updated, raw, (old, new)))

        result = {
            "moves": [{"from": str(old), "to": str(new)} for old, new in moves],
            "updated_states": [
                str(path) for path, _, _, replacement in states if replacement is not None
            ],
            "unresolved_states": unresolved,
            "dry_run": dry_run,
        }
        if dry_run:
            return result

        completed_moves = []
        written_states = []
        try:
            for old, new in moves:
                new.parent.mkdir(mode=0o700, exist_ok=True)
                _real_directory(new.parent, "日期目录")
                os.rename(old, new)
                completed_moves.append((old, new))
            for path, updated, raw, replacement in states:
                if replacement is None:
                    continue
                _atomic_write(path, updated)
                written_states.append((path, raw))
        except BaseException:
            for path, raw in reversed(written_states):
                temporary = path.with_name(f".{path.name}.rollback")
                temporary.write_bytes(raw)
                os.replace(temporary, path)
            for old, new in reversed(completed_moves):
                if new.exists() and not old.exists():
                    os.rename(new, old)
            raise
        return result
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate swimwear archive layout")
    parser.add_argument("--done-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = migrate(args.done_root, args.state_root, dry_run=args.dry_run)
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (MigrationError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
