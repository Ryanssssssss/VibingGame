"""Isolated editor workspaces and persistent, conflict-aware file transactions.

The agent never receives the live project as its output directory. Godot can
import/run the disposable copy without racing the editor's resource importer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

IGNORED = {".godot", ".git", ".hg", ".svn", ".vibe", "__pycache__", "node_modules"}


class Conflict(RuntimeError):
    pass


def safe_path(root: Path, relative: str) -> Path:
    relative = relative.removeprefix("res://")
    parts = Path(relative).parts
    if not relative or Path(relative).is_absolute() or any(p in IGNORED or p == ".." for p in parts):
        raise ValueError("不允许的项目路径")
    if ":" in relative or relative.startswith(("/", "\\")):
        raise ValueError("不允许的项目路径")
    target = root / relative
    # Reject all links/junctions, including ones pointing back inside the tree.
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("项目路径包含符号链接或目录联接")
    target.resolve().relative_to(root.resolve())
    return target


def inventory(root: Path) -> dict[str, str]:
    """Return a streaming content-hash inventory without retaining file bodies."""
    result: dict[str, str] = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in IGNORED]
        for name in dirs + files:
            safe_path(root, (Path(directory) / name).relative_to(root).as_posix())
        for name in files:
            path = Path(directory) / name
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            result[path.relative_to(root).as_posix()] = hasher.hexdigest()
    return result


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def atomic_write(path: Path, data: bytes | None):
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".vibe-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Workspace:
    def __init__(self, root: Path, storage: Path, task_id: str):
        self.root = root.resolve()
        self.storage = storage / "transactions" / task_id
        self.storage.mkdir(parents=True, exist_ok=False)
        self.base = inventory(self.root)
        self.before: dict[str, bytes | None] = {}
        self.after: dict[str, str | None] = {}
        self.stage = self.storage / "project"
        shutil.copytree(self.root, self.stage, ignore=shutil.ignore_patterns(*IGNORED))

    def changes(self) -> dict[str, bytes | None]:
        current = inventory(self.stage)
        changed: dict[str, bytes | None] = {}
        for name in self.base.keys() | current.keys():
            if self.base.get(name) != current.get(name):
                path = safe_path(self.stage, name)
                changed[name] = path.read_bytes() if path.exists() else None
        return changed

    def commit(self, dirty: list[str]):
        changes = self.changes()
        dirty = {p.removeprefix("res://").replace("\\", "/") for p in dirty}
        for name in changes:
            path = safe_path(self.root, name)
            actual = path.read_bytes() if path.exists() else None
            if name in dirty or digest(actual) != self.base.get(name):
                raise Conflict(f"文件有未保存内容或已在外部修改：{name}")
        if not changes:
            return []
        previous = {name: (safe_path(self.root, name).read_bytes() if safe_path(self.root, name).exists() else None)
                    for name in changes}
        for name in changes:
            self.before.setdefault(name, previous[name])
            self.after[name] = digest(changes[name])
        # Persist the journal BEFORE touching live files. An interrupted journal
        # is exposed for review, never automatically replayed on restart.
        self._save("applying")
        applied = []
        try:
            for name, data in changes.items():
                path = safe_path(self.root, name)
                actual = path.read_bytes() if path.exists() else None
                if actual != previous[name]:
                    raise Conflict(f"写入前文件发生变化：{name}")
                atomic_write(path, data)
                applied.append(name)
        except BaseException:
            for name in reversed(applied):
                path = safe_path(self.root, name)
                actual = path.read_bytes() if path.exists() else None
                if digest(actual) == digest(changes[name]):
                    atomic_write(path, previous[name])
            self._save("interrupted")
            raise
        for name, content in changes.items():
            if content is None:
                self.base.pop(name, None)
            else:
                self.base[name] = digest(content)
        self._save("applied")
        return list(changes)

    def _save(self, status: str):
        atomic_write(self.storage / "journal.json", json.dumps({
            "root": str(self.root), "status": status,
            "before": {name: base64.b64encode(data).decode() if data is not None else None
                       for name, data in self.before.items()}, "after": self.after,
        }, ensure_ascii=False).encode())

    def close(self):
        # Only the disposable copy, never the live project or recovery journal.
        if self.stage.parent == self.storage and self.stage.name == "project":
            shutil.rmtree(self.stage)


def rollback(journal: Path, root: Path, dirty: list[str]) -> list[str]:
    data = json.loads(journal.read_text(encoding="utf-8"))
    if Path(data["root"]).resolve() != root.resolve() or data["status"] != "applied":
        raise Conflict("此任务不能自动回退；请检查事务日志")
    dirty = {p.removeprefix("res://").replace("\\", "/") for p in dirty}
    for name, expected in data["after"].items():
        path = safe_path(root, name)
        actual = path.read_bytes() if path.exists() else None
        if name in dirty or digest(actual) != expected:
            raise Conflict(f"回退冲突：{name}")
    for name, content in data["before"].items():
        atomic_write(safe_path(root, name), base64.b64decode(content) if content is not None else None)
    data["status"] = "reverted"
    atomic_write(journal, json.dumps(data, ensure_ascii=False).encode())
    return list(data["before"])
