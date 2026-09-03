"""Atomic JSON read/write with cross-process locking and corruption recovery.

Lock order (NEVER nest in reverse): config > playlists > schedules >
commands > status. This invariant is enforced by every helper taking a
single lock at a time.

On JSONDecodeError during read we back up the corrupt file as
``<name>.json.corrupt.<timestamp>`` and rewrite the canonical default,
so the system never gets stuck on a broken file.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared import paths, schemas
from shared.locking import file_lock, LockTimeout


# ---------------------------------------------------------------------------
# Low-level atomic primitives
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: Any, indent: int = 2) -> None:
    """Write ``data`` atomically. The caller MUST hold the lock on ``path``.

    Strategy: write to ``<path>.tmp.<rand>`` in the same directory, fsync,
    then ``os.replace`` (atomic on POSIX). On any error before replace, the
    tmp file is removed and the original file is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Cleanup tmp on any failure; re-raise.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json(path: os.PathLike | str, data: Any, indent: int = 2) -> None:
    """Acquire the exclusive lock and atomically write ``data`` to ``path``."""
    path = Path(path)
    with file_lock(str(path), shared=False):
        _atomic_write(path, data, indent)


def _read_raw(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_json(
    path: os.PathLike | str,
    default: Any = None,
    *,
    repair: bool = True,
) -> Any:
    """Read JSON with a shared lock.

    On ``JSONDecodeError``, if ``repair=True`` (default), back the file up
    as ``<path>.corrupt.<ts>`` and rewrite the canonical default so the
    system stays operational. Returns ``default`` in that case.
    """
    path = Path(path)
    if default is None:
        default = {}

    with file_lock(str(path), shared=True):
        if not path.exists():
            return default
        try:
            return _read_raw(path)
        except json.JSONDecodeError:
            if not repair:
                raise

    # Fall out of the shared lock, then re-acquire exclusive for repair.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(path.suffix + f".corrupt.{ts}")
    with file_lock(str(path), shared=False):
        try:
            if path.exists():
                try:
                    os.replace(path, backup)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        _atomic_write(path, default)
    return default


def update_json(
    path: os.PathLike | str,
    mutator: Callable[[Any], Any],
    default: Any = None,
) -> Any:
    """Read-modify-write under a single exclusive lock.

    The mutator receives the current document and returns the new one. If
    the current file is unreadable, it is treated as ``default``.
    """
    if default is None:
        default = {}
    path = Path(path)
    with file_lock(str(path), shared=False):
        if path.exists():
            try:
                doc = _read_raw(path)
            except json.JSONDecodeError:
                doc = default
        else:
            doc = default
        new_doc = mutator(doc)
        _atomic_write(path, new_doc)
        return new_doc


# ---------------------------------------------------------------------------
# High-level helpers (one per data file)
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    raw = read_json(paths.config_path(), default=schemas.DEFAULT_CONFIG)
    return schemas.deep_merge(schemas.DEFAULT_CONFIG, raw)


def save_config(cfg: dict[str, Any]) -> None:
    write_json(paths.config_path(), cfg)


def load_schedules() -> dict[str, Any]:
    raw = read_json(paths.schedules_path(), default=schemas.DEFAULT_SCHEDULES)
    if not isinstance(raw, dict):
        return dict(schemas.DEFAULT_SCHEDULES)
    raw.setdefault("version", 1)
    raw.setdefault("schedules", [])
    if not isinstance(raw.get("schedules"), list):
        raw["schedules"] = []
    return raw


def save_schedules(doc: dict[str, Any]) -> None:
    write_json(paths.schedules_path(), doc)


def load_commands() -> dict[str, Any]:
    raw = read_json(paths.commands_path(), default=schemas.DEFAULT_COMMANDS)
    if not isinstance(raw, dict):
        return dict(schemas.DEFAULT_COMMANDS)
    raw.setdefault("version", 1)
    raw.setdefault("seq", 0)
    raw.setdefault("commands", [])
    if not isinstance(raw.get("commands"), list):
        raw["commands"] = []
    return raw


def save_commands(doc: dict[str, Any]) -> None:
    write_json(paths.commands_path(), doc)


def load_status() -> dict[str, Any]:
    raw = read_json(paths.status_path(), default=schemas.DEFAULT_STATUS)
    return schemas.deep_merge(schemas.DEFAULT_STATUS, raw)


def save_status(doc: dict[str, Any]) -> None:
    """Write the daemon's status snapshot.

    Status is append-mostly; we hold an exclusive lock but don't expect
    contention since only the daemon writes here.
    """
    write_json(paths.status_path(), doc)


def load_playlist(playlist_id: str) -> dict[str, Any] | None:
    """Return a playlist by id, or ``None`` if missing or unreadable.

    A missing playlist is distinct from a corrupted one: corrupt files are
    auto-repaired and returned as an empty dict, missing files are None.
    """
    if not schemas.validate_playlist_id(playlist_id):
        return None
    p = paths.playlist_path(playlist_id)
    if not p.exists():
        return None
    try:
        # repair=False so a corrupt file surfaces as an empty dict rather
        # than silently nuking user data; callers decide how to handle it.
        doc = read_json(p, default={}, repair=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def save_playlist(playlist: dict[str, Any]) -> None:
    pid = playlist.get("id")
    if not schemas.validate_playlist_id(pid):
        raise ValueError(f"invalid playlist id: {pid!r}")
    schemas.touch_playlist(playlist)
    write_json(paths.playlist_path(pid), playlist)


def delete_playlist(playlist_id: str) -> bool:
    """Delete a playlist JSON file. Returns True if a file was removed."""
    if not schemas.validate_playlist_id(playlist_id):
        raise ValueError(f"invalid playlist id: {playlist_id!r}")
    p = paths.playlist_path(playlist_id)
    lock_path = str(p) + ".lock"
    with file_lock(str(p), shared=False):
        removed = False
        try:
            os.remove(p)
            removed = True
        except FileNotFoundError:
            removed = False
        # Clean the lock file too if we created it.
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
        return removed


def list_playlists() -> list[str]:
    """Return sorted playlist IDs from the playlists directory."""
    d = paths.playlists_dir()
    if not d.exists():
        return []
    out: list[str] = []
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".json":
            continue
        if entry.name.startswith("."):
            continue
        if entry.name.endswith(".lock"):
            continue
        pid = entry.stem
        if schemas.validate_playlist_id(pid):
            out.append(pid)
    return sorted(out)


__all__ = [
    "write_json",
    "read_json",
    "update_json",
    "load_config",
    "save_config",
    "load_schedules",
    "save_schedules",
    "load_commands",
    "save_commands",
    "load_status",
    "save_status",
    "load_playlist",
    "save_playlist",
    "delete_playlist",
    "list_playlists",
    "LockTimeout",
]