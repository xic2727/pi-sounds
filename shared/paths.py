"""Resolve filesystem paths for the pi-sounds data and runtime directories.

* Data directory is taken from the ``PI_SOUNDS_DATA`` env var when set,
  otherwise ``~/.local/share/pi-sounds/data`` (XDG default).
* Runtime directory (mpv IPC socket) is taken from ``XDG_RUNTIME_DIR``
  when set (the systemd unit sets this), otherwise ``/tmp/pi-sounds``.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_DATA = "PI_SOUNDS_DATA"
_ENV_XDG_RUNTIME = "XDG_RUNTIME_DIR"

_DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "pi-sounds" / "data"


def data_dir() -> Path:
    """Return the directory holding all JSON state files."""
    p = os.environ.get(_ENV_DATA)
    return Path(p) if p else _DEFAULT_DATA_DIR


def config_path() -> Path:
    return data_dir() / "config.json"


def schedules_path() -> Path:
    return data_dir() / "schedules.json"


def commands_path() -> Path:
    return data_dir() / "commands.json"


def status_path() -> Path:
    return data_dir() / "status.json"


def playlists_dir() -> Path:
    return data_dir() / "playlists"


def playlist_path(playlist_id: str) -> Path:
    return playlists_dir() / f"{playlist_id}.json"


def runtime_dir() -> Path:
    """Directory for transient runtime sockets (mpv IPC)."""
    base = os.environ.get(_ENV_XDG_RUNTIME)
    if base:
        return Path(base) / "pi-sounds"
    return Path("/tmp") / "pi-sounds"


def mpv_socket_path() -> Path:
    return runtime_dir() / "mpv.sock"