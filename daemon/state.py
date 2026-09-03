"""Status writer: periodically serialises the controller snapshot to ``status.json``.

Runs in its own thread. Uses the ``Controller.full_snapshot`` helper so
all rolling logs (errors, recent commands) come along for free. The
status file is the single contract the Streamlit UI consumes, so it must
always be valid JSON.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from daemon.config import get_logger
from daemon.controller import Controller
from daemon import store

log = get_logger("state-writer")


class StateWriter(threading.Thread):
    def __init__(
        self,
        controller: Controller,
        daemon_info_fn: Callable[[], dict],
        interval: float = 1.0,
    ):
        super().__init__(daemon=True, name="state-writer")
        self.controller = controller
        self._daemon_info_fn = daemon_info_fn
        self.interval = max(0.2, float(interval))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("state writer starting (interval %.2fs)", self.interval)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("state writer tick failed")
            self._stop_event.wait(self.interval)
        log.info("state writer stopped")

    def _tick(self) -> None:
        snap = self.controller.full_snapshot(self._daemon_info_fn())
        store.save_status(snap)


__all__ = ["StateWriter"]