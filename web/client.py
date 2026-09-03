"""Streamlit-side helpers: read status, send commands, manage playlists.

The UI and the daemon are separate processes. They communicate through
the JSON files in ``data/`` (read status, write commands) and acquire
file locks to avoid corrupting each other.

All caching is short (under one second) so the UI feels live without
hammering the disk.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from shared import paths, schemas
from shared.locking import file_lock
from shared.schemas import make_command


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def read_status() -> dict[str, Any]:
    """Return the daemon's status snapshot (merged with defaults)."""
    from daemon import store
    return store.load_status()


def daemon_is_responsive(status: dict[str, Any] | None = None,
                         stale_sec: float = 10.0) -> bool:
    """Treat the daemon as alive if ``status.ts`` is recent enough."""
    if status is None:
        status = read_status()
    ts = status.get("ts")
    if not ts:
        return False
    try:
        from datetime import datetime
        last = datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return False
    return (time.time() - last) < stale_sec


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def send_command(action: str, **args: Any) -> str:
    """Append a command to commands.json and return its id."""
    cmd = make_command(action, args)
    cmd_id = cmd["id"]

    def _append(doc: dict[str, Any]) -> dict[str, Any]:
        doc.setdefault("commands", [])
        doc.setdefault("seq", 0)
        doc["commands"].append(cmd)
        doc["commands"] = doc["commands"][-50:]
        doc["seq"] = doc.get("seq", 0) + 1
        return doc

    from daemon import store
    store.update_json(paths.commands_path(), _append,
                      default=schemas.DEFAULT_COMMANDS)
    return cmd_id


def send_play(playlist_id: str | None = None, *,
              path: str | None = None, index: int = 0, title: str | None = None) -> str:
    args: dict[str, Any] = {"index": int(index)}
    if playlist_id:
        args["playlist_id"] = playlist_id
    if path:
        args["path"] = path
    if title:
        args["title"] = title
    return send_command("play", **args)


def send_toggle_pause() -> str:
    return send_command("toggle_pause")


def send_stop() -> str:
    return send_command("stop")


def send_next() -> str:
    return send_command("next")


def send_prev() -> str:
    return send_command("prev")


def send_set_volume(volume: int) -> str:
    return send_command("set_volume", volume=int(volume))


def send_set_mode(mode: str) -> str:
    return send_command("set_mode", mode=str(mode))


def send_ping() -> str:
    return send_command("ping")


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def list_playlists() -> list[str]:
    from daemon import store
    return store.list_playlists()


def load_playlist(pid: str) -> dict[str, Any] | None:
    from daemon import store
    return store.load_playlist(pid)


def save_playlist(playlist: dict[str, Any]) -> None:
    from daemon import store
    store.save_playlist(playlist)


def delete_playlist(pid: str) -> bool:
    from daemon import store
    return store.delete_playlist(pid)


def list_audio_files() -> list[dict[str, Any]]:
    from daemon import library
    return library.list_audio_files()


def rescan_library() -> None:
    send_command("rescan_library")


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def load_schedules() -> dict[str, Any]:
    from daemon import store
    return store.load_schedules()


def save_schedules(doc: dict[str, Any]) -> None:
    from daemon import store
    store.save_schedules(doc)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    from daemon import store
    return store.load_config()


def save_config(cfg: dict[str, Any]) -> None:
    from daemon import store
    store.save_config(cfg)


def load_commands() -> dict[str, Any]:
    from daemon import store
    return store.load_commands()


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def fmt_mmss(seconds: float) -> str:
    """Format a number of seconds as ``M:SS`` or ``H:MM:SS``."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "--:--"
    if s < 0:
        return "--:--"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def state_label(state: str | None) -> str:
    mapping = {
        "idle": "空闲",
        "playing": "播放中",
        "paused": "已暂停",
        "stopped": "已停止",
        "error": "错误",
    }
    return mapping.get(state or "", state or "未知")


__all__ = [
    "read_status", "daemon_is_responsive",
    "send_command", "send_play", "send_toggle_pause", "send_stop",
    "send_next", "send_prev", "send_set_volume", "send_set_mode", "send_ping",
    "list_playlists", "load_playlist", "save_playlist", "delete_playlist",
    "list_audio_files", "rescan_library",
    "load_schedules", "save_schedules",
    "load_config", "save_config",
    "fmt_mmss", "state_label",
]