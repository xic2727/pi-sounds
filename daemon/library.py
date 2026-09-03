"""Audio library scanner.

Recursively walks ``config.audio_dir`` and returns a flat list of audio
files matching the configured extensions. Used by the UI's file browser
and by ``rescan_library`` to mark missing playlist items.

Title is derived from the file name (stem). Paths are returned relative
to ``audio_dir`` to match how playlists store them.
"""
from __future__ import annotations

import os
from typing import Any

from shared import paths
from daemon import store


def list_audio_files(
    audio_dir: str | None = None,
    extensions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return all audio files under ``audio_dir`` (recursive).

    Each entry: ``{"path": "relative/path.mp3", "title": "Display Name",
    "size": int, "abs_path": "/absolute/path.mp3"}``.

    If ``audio_dir`` or ``extensions`` are not provided, the current
    config is consulted.
    """
    if audio_dir is None or extensions is None:
        cfg = store.load_config()
        if audio_dir is None:
            audio_dir = cfg.get("audio_dir") or str(paths.data_dir().parent / "sounds")
        if extensions is None:
            extensions = list(cfg.get("extensions") or [".mp3", ".wav", ".flac"])

    audio_dir = os.path.expanduser(str(audio_dir))
    if not os.path.isdir(audio_dir):
        return []

    # Normalise extensions to lowercase with leading dot
    ext_set = {
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in extensions
    }

    out: list[dict[str, Any]] = []
    for root, _dirs, files in os.walk(audio_dir):
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext not in ext_set:
                continue
            abs_path = os.path.join(root, name)
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                size = 0
            rel_path = os.path.relpath(abs_path, audio_dir).replace(os.sep, "/")
            out.append({
                "path": rel_path,
                "title": os.path.splitext(name)[0],
                "size": size,
                "abs_path": abs_path,
            })
    out.sort(key=lambda f: f["path"].lower())
    return out


def check_files_exist(
    items: list[dict[str, Any]],
    audio_dir: str | None = None,
) -> list[dict[str, Any]]:
    """For each playlist item, set ``missing`` to True/False based on disk.

    Returns the same list with ``missing`` filled in. ``items`` is
    mutated in place and also returned for convenience.
    """
    if audio_dir is None:
        cfg = store.load_config()
        audio_dir = cfg.get("audio_dir") or "."
    audio_dir = os.path.expanduser(str(audio_dir))

    for item in items:
        path = item.get("path") or ""
        if os.path.isabs(path):
            item["missing"] = not os.path.isfile(path)
        else:
            item["missing"] = not os.path.isfile(os.path.join(audio_dir, path))
    return items


__all__ = ["list_audio_files", "check_files_exist"]