"""Daemon-side controller: owns the player and tracks playback state.

The Controller is the single source of truth for "what is the daemon
doing right now". All mutations go through it under an ``RLock`` so the
command bus thread, the watchdog, and the cron scheduler cannot trample
each other.

This module only deals with single-track playback. Playlist support,
queue advancement, and mode logic live in ``queue_engine.py`` (Step 4).
"""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any

from daemon.config import get_logger
from daemon.player import MpvPlayer, PlayerError
from daemon import store

log = get_logger("controller")


_COMMAND_RESULTS_MAX = 20
_ERRORS_MAX = 20


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class Controller:
    """State machine + thin facade over ``MpvPlayer``."""

    def __init__(self, player: MpvPlayer):
        self.player = player
        self._lock = threading.RLock()
        self._state: str = "idle"
        self._current_track: dict[str, Any] | None = None
        self._volume: int = 60
        self._play_mode: str = "sequence"
        self._playlist_id: str | None = None
        self._playlist_name: str | None = None
        self._last_error: str | None = None
        self._command_results: deque[dict[str, Any]] = deque(maxlen=_COMMAND_RESULTS_MAX)
        self._errors: deque[dict[str, Any]] = deque(maxlen=_ERRORS_MAX)
        self._queue_length: int = 0
        self._queue_order: list[int] = []
        self._queue_index: int = -1
        # Late import to avoid a circular dependency on daemon.queue_engine.
        from daemon.queue_engine import QueueEngine
        self.queue: QueueEngine = QueueEngine(player, self)
        # Hook the player events into the queue engine.
        self.player.on_event = self._on_mpv_event

    # ----- state transitions ------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def set_state_error(self, msg: str) -> None:
        with self._lock:
            self._state = "error"
            self._last_error = msg
            self._record_error(msg, "error")
            log.error("controller entered error state: %s", msg)

    def _record_error(self, msg: str, level: str = "error") -> None:
        from shared.schemas import now_iso
        self._errors.appendleft({"ts": now_iso(), "level": level, "msg": msg})

    def record_warning(self, msg: str) -> None:
        with self._lock:
            self._record_error(msg, "warning")

    def record_command_results(self, results: list[dict[str, Any]]) -> None:
        """Append command results to the rolling history (newest first)."""
        with self._lock:
            for r in reversed(results):
                self._command_results.appendleft(r)

    def command_results_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._command_results)

    def errors_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._errors)

    # ----- playback commands ------------------------------------------

    def play_file(self, path: str, title: str | None = None) -> None:
        """Load and start playing a single file (no queue logic)."""
        if not path:
            raise ValueError("path is required")
        with self._lock:
            try:
                self.player.loadfile(path)
                self.player.resume()
            except PlayerError as e:
                self._state = "error"
                self._last_error = str(e)
                raise
            self._current_track = {
                "path": path,
                "title": title or _title_from_path(path),
            }
            self._state = "playing"
            self._last_error = None
            log.info("play_file: %s", path)

    def pause(self) -> None:
        with self._lock:
            if self._state == "playing":
                self.player.pause()
                self._state = "paused"

    def resume(self) -> None:
        with self._lock:
            if self._state == "paused":
                self.player.resume()
                self._state = "playing"

    def toggle_pause(self) -> None:
        with self._lock:
            if self._state == "playing":
                self.player.pause()
                self._state = "paused"
            elif self._state == "paused":
                self.player.resume()
                self._state = "playing"

    def stop(self) -> None:
        with self._lock:
            self.player.stop_playback()
            self._state = "stopped"
            self._current_track = None

    # ----- queue control -----------------------------------------------

    def play_playlist(self, playlist_id: str, index: int = 0) -> None:
        """Load a playlist from disk and start playback."""
        if not playlist_id:
            raise ValueError("playlist_id is required")
        from daemon import store
        playlist = store.load_playlist(playlist_id)
        if playlist is None:
            raise ValueError(f"playlist not found: {playlist_id!r}")
        self.queue.load_playlist(playlist, start_index=index)

    def set_mode(self, mode: str) -> None:
        self.queue.set_mode(mode)

    def next(self) -> None:
        self.queue.next()

    def prev(self) -> None:
        self.queue.prev()

    def seek(self, seconds: float, mode: str = "relative") -> None:
        with self._lock:
            self.player.seek(seconds, mode)

    # ----- volume -----------------------------------------------------

    def set_volume(self, vol: int, *, persist: bool = True) -> None:
        n = max(0, min(100, int(vol)))
        with self._lock:
            self.player.set_volume(n)
            self._volume = n
        if persist:
            try:
                cfg = store.load_config()
                cfg["volume"] = n
                store.save_config(cfg)
            except Exception:
                log.exception("failed to persist volume")

    # ----- config sync -----------------------------------------------

    def apply_config(self, cfg: dict[str, Any]) -> None:
        """Push relevant config values into in-memory state."""
        with self._lock:
            self._volume = int(cfg.get("volume", self._volume))
            self._play_mode = str(cfg.get("play_mode", self._play_mode))

    # ----- status snapshot -------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            track = self._current_track or {"path": None, "title": None}
            return {
                "state": self._state,
                "playlist_id": self._playlist_id,
                "playlist_name": self._playlist_name,
                "index": self._queue_index,
                "track": {"path": track.get("path"), "title": track.get("title")},
                "position_sec": float(self.player.get_cached_property("time-pos") or 0.0),
                "duration_sec": float(self.player.get_cached_property("duration") or 0.0),
                "volume": self._volume,
                "play_mode": self._play_mode,
                "error": self._last_error,
            }

    def queue_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"length": self._queue_length, "order": list(self._queue_order)}

    def full_snapshot(self, daemon_info: dict[str, Any] | None = None) -> dict[str, Any]:
        """Snapshot including the rolling logs, for the status writer."""
        from shared.schemas import now_iso
        snap = self.snapshot()
        return {
            "version": 1,
            "ts": now_iso(),
            "daemon": daemon_info or {},
            "player": snap,
            "queue": self.queue_snapshot(),
            "schedules": {"next": None},
            "errors": self.errors_snapshot(),
            "last_commands": self.command_results_snapshot(),
        }

    # ----- queue integration (called by QueueEngine) -------------------

    def set_track(
        self,
        *,
        playlist_id: str | None,
        playlist_name: str | None,
        index: int,
        track: dict[str, Any] | None,
        mode: str,
        queue_length: int,
        order: list[int],
    ) -> None:
        with self._lock:
            self._playlist_id = playlist_id
            self._playlist_name = playlist_name
            self._queue_index = index
            self._queue_length = queue_length
            self._queue_order = list(order)
            self._play_mode = mode
            if track is not None:
                self._current_track = track
                self._state = "playing"
                self._last_error = None
            elif self._state == "playing":
                self._state = "idle"

    def clear_queue(self) -> None:
        with self._lock:
            self._playlist_id = None
            self._playlist_name = None
            self._queue_index = -1
            self._queue_length = 0
            self._queue_order = []
            self._current_track = None

    def _on_mpv_event(self, evt: dict[str, Any]) -> None:
        name = evt.get("event")
        if name == "end-file":
            try:
                self.queue.on_track_end(evt.get("reason") or "eof")
            except Exception:
                log.exception("queue on_track_end failed")


def _title_from_path(path: str) -> str:
    base = os.path.basename(path)
    stem, _ = os.path.splitext(base)
    return stem or path