"""Queue engine: playlist ordering, automatic advancement, and play modes.

Driven by ``end-file`` events from mpv (forwarded via the Controller's
``_on_mpv_event`` hook). Owns no state of its own beyond the active
playlist and a permutation of its items.

The five play modes map directly to advancement behaviour:

* ``single``     — play the current track once, then stop.
* ``sequence``   — play in given order; stop at end.
* ``shuffle``    — random order, no repeats within a round, reshuffle per round.
* ``repeat_one`` — loop the current track forever.
* ``repeat_all`` — loop the entire sequence forever.
"""
from __future__ import annotations

import os
import random
import threading
from typing import Any

from daemon.config import get_logger
from daemon.controller import Controller
from daemon.player import MpvPlayer, PlayerError
from shared import schemas

log = get_logger("queue")


class QueueEngine:
    MAX_SKIP_ATTEMPTS = 8

    def __init__(self, player: MpvPlayer, controller: Controller):
        self.player = player
        self.controller = controller
        self._lock = threading.RLock()

        self._playlist: dict[str, Any] | None = None
        self._playlist_id: str | None = None
        self._playlist_name: str | None = None
        self._items: list[dict[str, Any]] = []
        self._order: list[int] = []
        self._index: int = -1
        self._mode: str = "sequence"
        self._skip_attempts: int = 0

    # ----- read-only accessors ----------------------------------------

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def playlist_id(self) -> str | None:
        with self._lock:
            return self._playlist_id

    @property
    def playlist_name(self) -> str | None:
        with self._lock:
            return self._playlist_name

    @property
    def current_item(self) -> dict[str, Any] | None:
        with self._lock:
            idx = self._item_index()
            if 0 <= idx < len(self._items):
                return self._items[idx]
            return None

    @property
    def length(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def position(self) -> int:
        """Index inside the order permutation (0-based), or -1."""
        with self._lock:
            return self._index if 0 <= self._index < len(self._order) else -1

    # ----- public API -------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if not schemas.validate_play_mode(mode):
            raise ValueError(f"invalid mode: {mode!r}")
        with self._lock:
            old_mode = self._mode
            self._mode = mode
            if mode == "shuffle" and old_mode != "shuffle":
                self._reshuffle()
            elif mode != "shuffle" and old_mode == "shuffle":
                self._rebuild_order_sequential()

    def load_playlist(
        self,
        playlist: dict[str, Any],
        *,
        mode: str | None = None,
        start_index: int = 0,
    ) -> None:
        """Switch to a new playlist and start playback from ``start_index``.

        ``mode`` overrides the playlist's own ``play_mode`` when given.
        """
        items = list(playlist.get("items") or [])
        effective_mode = (
            mode
            or playlist.get("play_mode")
            or self._mode
            or "sequence"
        )
        if not schemas.validate_play_mode(effective_mode):
            effective_mode = "sequence"
        with self._lock:
            self._playlist = playlist
            self._playlist_id = playlist.get("id")
            self._playlist_name = playlist.get("name") or self._playlist_id
            self._items = items
            self._mode = effective_mode
            self._skip_attempts = 0
            self._build_order()
            if not items:
                self._index = -1
                self.controller.clear_queue()
                self.controller.set_state_error("playlist is empty")
                log.warning("playlist %r is empty", self._playlist_id)
                return
            start = max(0, min(len(self._order) - 1, int(start_index)))
            self._index = start
            self._play_current()

    def next(self, user_initiated: bool = True) -> None:
        with self._lock:
            self._advance(user_initiated=user_initiated)

    def prev(self) -> None:
        with self._lock:
            self._regress()

    def on_track_end(self, reason: str) -> None:
        """Handle an ``end-file`` event from mpv.

        ``reason`` is one of ``eof``, ``stop``, ``error``, ``redirect``,
        ``unknown`` — anything else is treated like ``unknown`` and ignored.
        """
        with self._lock:
            if not self._items:
                return
            if reason == "stop":
                # User pressed stop — do not auto-advance.
                self._skip_attempts = 0
                return
            if reason in ("error", "redirect"):
                self._skip_attempts += 1
                if self._skip_attempts > self.MAX_SKIP_ATTEMPTS:
                    self.controller.stop()
                    self.controller.record_warning(
                        f"too many playback errors in a row; stopped"
                    )
                    return
                self._advance(skipped_due_to_error=True)
                return
            if reason == "eof":
                self._skip_attempts = 0
                self._advance_after_eof()
                return
            # unknown / anything else: ignore
            log.debug("ignoring end-file reason=%r", reason)

    # ----- internal helpers -------------------------------------------

    def _build_order(self) -> None:
        if self._mode == "shuffle":
            self._reshuffle()
        else:
            self._rebuild_order_sequential()

    def _rebuild_order_sequential(self) -> None:
        self._order = list(range(len(self._items)))

    def _reshuffle(self) -> None:
        self._order = list(range(len(self._items)))
        random.shuffle(self._order)
        self._index = 0

    def _item_index(self) -> int:
        if 0 <= self._index < len(self._order):
            return self._order[self._index]
        return -1

    def _play_current(self) -> None:
        if not (0 <= self._index < len(self._order)):
            return
        item_idx = self._order[self._index]
        if not (0 <= item_idx < len(self._items)):
            return
        item = self._items[item_idx]
        path = item.get("path")
        if not path:
            log.warning("item %d has no path", item_idx)
            self._advance(skipped_due_to_error=True)
            return

        abs_path = self._resolve_path(path)
        if not os.path.isfile(abs_path):
            log.warning("missing file: %s", abs_path)
            self.controller.record_warning(f"missing file: {path}")
            item["missing"] = True
            self._advance(skipped_due_to_error=True)
            return

        try:
            self.player.loadfile(abs_path)
            self.player.resume()
        except PlayerError as e:
            log.warning("loadfile failed for %s: %s", abs_path, e)
            self.controller.record_warning(f"loadfile failed: {path}: {e}")
            item["missing"] = True
            self._advance(skipped_due_to_error=True)
            return

        item["missing"] = False
        title = item.get("title") or _title_from_path(path)
        self.controller.set_track(
            playlist_id=self._playlist_id,
            playlist_name=self._playlist_name,
            index=self._index,
            track={"path": path, "title": title},
            mode=self._mode,
            queue_length=len(self._items),
            order=list(self._order),
        )

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        from shared import paths
        audio_dir = paths.data_dir().parent  # default audio_dir is sibling of data
        # The audio_dir is read from config at startup; we don't keep it
        # here to avoid stale state. Use the audio_dir from config.
        try:
            from daemon import store
            cfg = store.load_config()
            base = cfg.get("audio_dir") or str(audio_dir)
        except Exception:
            base = str(audio_dir)
        return os.path.join(base, path)

    def _advance_after_eof(self) -> None:
        if self._mode == "single":
            self.controller.stop()
            return
        if self._mode == "repeat_one":
            self._play_current()
            return
        self._advance()

    def _advance(
        self,
        *,
        user_initiated: bool = False,
        skipped_due_to_error: bool = False,
    ) -> None:
        if not self._order:
            return
        n = len(self._order)
        if self._index < 0:
            self._index = 0
        else:
            self._index += 1
        if self._index >= n:
            # Wrap or stop depending on mode.
            if self._mode in ("repeat_all", "shuffle"):
                if self._mode == "shuffle":
                    self._reshuffle()
                else:
                    self._rebuild_order_sequential()
                self._index = 0
            else:
                # sequence or single: stop at end
                self._index = n - 1
                self.controller.stop()
                return
        self._play_current()

    def _regress(self) -> None:
        if not self._order:
            return
        n = len(self._order)
        if n == 0:
            return
        if self._index <= 0:
            # At first track — restart it from the top rather than wrapping.
            self._play_current()
            return
        self._index -= 1
        self._play_current()


def _title_from_path(path: str) -> str:
    base = os.path.basename(path)
    stem, _ = os.path.splitext(base)
    return stem or path


__all__ = ["QueueEngine"]