"""Default values and lightweight validation for JSON state files.

All defaults are intentionally permissive — unknown keys are preserved
when reading, so schema upgrades do not destroy user data.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Enumerations & regexes
# ---------------------------------------------------------------------------

PLAY_MODES: tuple[str, ...] = (
    "single",        # play the current track once, stop
    "sequence",      # play tracks in given order, stop at end
    "shuffle",       # play tracks in randomized order, no repeats, reshuffle per round
    "repeat_one",    # loop the current track forever
    "repeat_all",    # loop the entire sequence forever
)
PLAY_MODE_DEFAULT: str = "sequence"

PLAYER_STATES: tuple[str, ...] = ("idle", "playing", "paused", "stopped", "error")

ID_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")
SCHEDULE_ID_PREFIX = "sch_"
COMMAND_ID_PREFIX = "c_"

DEFAULT_EXTENSIONS: list[str] = [
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus",
]


# ---------------------------------------------------------------------------
# Default document shapes
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "audio_dir": str(Path.home() / "sounds"),
    "audio_device": "auto",
    "volume": 60,
    "play_mode": PLAY_MODE_DEFAULT,
    "extensions": list(DEFAULT_EXTENSIONS),
    "startup": {"autoplay": False, "playlist_id": None},
    "status_interval_sec": 1.0,
    "command_poll_sec": 0.3,
}

DEFAULT_SCHEDULES: dict[str, Any] = {
    "version": 1,
    "schedules": [],
}

DEFAULT_COMMANDS: dict[str, Any] = {
    "version": 1,
    "seq": 0,
    "commands": [],
}

DEFAULT_STATUS: dict[str, Any] = {
    "version": 1,
    "ts": None,
    "daemon": {
        "pid": None,
        "started_at": None,
        "healthy": False,
        "mpv_alive": False,
        "mpv_restarts": 0,
    },
    "player": {
        "state": "idle",
        "playlist_id": None,
        "playlist_name": None,
        "index": 0,
        "track": {"path": None, "title": None},
        "position_sec": 0.0,
        "duration_sec": 0.0,
        "volume": 60,
        "play_mode": PLAY_MODE_DEFAULT,
    },
    "queue": {"length": 0, "order": []},
    "schedules": {"next": None},
    "errors": [],
    "last_commands": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """ISO-8601 timestamp in local time (no microseconds)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_playlist(
    playlist_id: str,
    name: str,
    items: list[dict[str, Any]] | None = None,
    play_mode: str | None = None,
) -> dict[str, Any]:
    """Create a fresh playlist document with the canonical shape."""
    ts = now_iso()
    return {
        "version": 1,
        "id": playlist_id,
        "name": name,
        "created_at": ts,
        "updated_at": ts,
        "play_mode": play_mode,
        "items": list(items) if items else [],
    }


def touch_playlist(playlist: dict[str, Any]) -> dict[str, Any]:
    """Bump the ``updated_at`` timestamp on a playlist document."""
    playlist["updated_at"] = now_iso()
    return playlist


def make_schedule(
    name: str,
    cron: str,
    playlist_id: str,
    *,
    enabled: bool = True,
    volume: int | None = None,
    play_mode: str | None = None,
    priority: int = 0,
    if_busy: str = "preempt",
    schedule_id: str | None = None,
) -> dict[str, Any]:
    """Create a fresh schedule document."""
    import uuid
    return {
        "id": schedule_id or f"{SCHEDULE_ID_PREFIX}{uuid.uuid4().hex[:6]}",
        "name": name,
        "cron": cron,
        "playlist_id": playlist_id,
        "enabled": enabled,
        "volume": volume,
        "play_mode": play_mode,
        "priority": priority,
        "if_busy": if_busy,
        "last_run": None,
        "last_result": None,
    }


def make_command(action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a fresh command document."""
    import uuid
    return {
        "id": f"{COMMAND_ID_PREFIX}{uuid.uuid4().hex[:6]}",
        "ts": now_iso(),
        "action": action,
        "args": args or {},
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_playlist_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.match(value))


def validate_play_mode(value: Any) -> bool:
    return isinstance(value, str) and value in PLAY_MODES


def validate_player_state(value: Any) -> bool:
    return isinstance(value, str) and value in PLAYER_STATES


def validate_volume(value: Any) -> tuple[bool, int]:
    """Return ``(in_range, clamped)``. ``in_range`` is False when the input
    was outside ``[0, 100]`` or could not be parsed as an integer."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False, 60
    clamped = max(0, min(100, n))
    return clamped == n, clamped


def validate_if_busy(value: Any) -> bool:
    return value in ("preempt", "skip")


def deep_merge(default: Any, override: Any) -> Any:
    """Recursively merge ``override`` onto ``default``. ``override`` wins on
    conflicts except when both sides are dicts (then keys are merged)."""
    if isinstance(default, dict) and isinstance(override, dict):
        out = dict(default)
        for k, v in override.items():
            out[k] = deep_merge(out.get(k), v)
        return out
    if override is None:
        return default
    return override