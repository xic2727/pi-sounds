"""mpv watchdog: restart the player if it dies unexpectedly.

If mpv crashes more than ``MAX_RESTARTS_PER_WINDOW`` times within
``RESTART_WINDOW_SEC``, the controller transitions to ``error`` state
and the watchdog stops retrying — the operator must intervene.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from daemon.config import get_logger
from daemon.controller import Controller
from daemon.player import MpvPlayer, PlayerStartError

log = get_logger("watchdog")

MAX_RESTARTS_PER_WINDOW = 5
RESTART_WINDOW_SEC = 60.0


class Watchdog(threading.Thread):
    def __init__(self, player: MpvPlayer, controller: Controller, interval: float = 2.0):  # noqa: N804
        super().__init__(daemon=True, name="watchdog")
        self.player = player
        self.controller = controller
        self.interval = max(0.5, float(interval))
        self._stop_event = threading.Event()
        self._was_alive: bool = True
        self._restart_times: deque[float] = deque(maxlen=32)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("watchdog starting (interval %.2fs)", self.interval)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("watchdog tick failed")
            self._stop_event.wait(self.interval)
        log.info("watchdog stopped")

    def _tick(self) -> None:
        alive = self.player.is_alive
        if alive:
            self._was_alive = True
            return
        # Player just died (transition alive -> dead).
        if self._was_alive:
            log.warning("mpv not alive; checking restart budget")
            self._was_alive = False
        now = time.monotonic()
        self._restart_times.append(now)
        recent = sum(1 for t in self._restart_times if now - t <= RESTART_WINDOW_SEC)
        if recent > MAX_RESTARTS_PER_WINDOW:
            msg = (
                f"mpv crashed >{MAX_RESTARTS_PER_WINDOW} times in "
                f"{RESTART_WINDOW_SEC:.0f}s; giving up"
            )
            log.error(msg)
            self.controller.set_state_error(msg)
            return
        try:
            self.player.restart()
        except PlayerStartError as e:
            log.error("mpv restart failed: %s", e)
            self.controller.set_state_error(str(e))
            return
        if self.player.is_alive:
            log.info("mpv restarted successfully (restarts: %d)",
                     self.player.restart_count)


__all__ = ["Watchdog"]