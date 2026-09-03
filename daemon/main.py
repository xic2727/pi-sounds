"""Daemon entry point: start the player, the worker threads, and block on signals.

Usage::

    python -m daemon.main                 # foreground
    systemctl start pi-sounds-daemon      # via systemd

The process is intended to be supervised by systemd (``Restart=always``);
this main function just runs forever until SIGTERM/SIGINT arrives.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

from daemon import store
from daemon.command_bus import CommandBus
from daemon.config import DAEMON_NAME, get_logger, setup_logging
from daemon.controller import Controller
from daemon.player import MpvPlayer, PlayerStartError
from daemon.scheduler import Scheduler
from daemon.state import StateWriter
from daemon.watchdog import Watchdog
from shared import paths


log = get_logger("main")


def main() -> int:
    setup_logging()
    log.info("%s starting (pid=%d)", DAEMON_NAME, os.getpid())

    # 1. Load configuration
    cfg = store.load_config()
    paths.runtime_dir().mkdir(parents=True, exist_ok=True)

    # 2. Construct the player
    player = MpvPlayer(
        socket_path=paths.mpv_socket_path(),
        audio_device=cfg.get("audio_device", "auto"),
        initial_volume=int(cfg.get("volume", 60)),
    )

    # 3. Wrap player in controller
    controller = Controller(player)
    controller.apply_config(cfg)

    # 4. Start mpv (retry once if the socket dir doesn't exist yet)
    try:
        player.start()
    except PlayerStartError as e:
        log.error("mpv failed to start: %s", e)
        # We still continue so the UI can surface the error via status.json.
        controller.set_state_error(f"mpv start failed: {e}")

    if player.is_alive:
        try:
            player.set_volume(int(cfg.get("volume", 60)))
        except Exception:
            log.exception("failed to apply initial volume")

    started_at = time.time()
    started_iso = _iso_from_ts(started_at)

    def daemon_info() -> dict:
        return {
            "pid": os.getpid(),
            "started_at": started_iso,
            "healthy": player.is_alive and controller.state != "error",
            "mpv_alive": player.is_alive,
            "mpv_restarts": player.restart_count,
        }

    # 5. Start worker threads
    cmd_bus = CommandBus(controller, poll_sec=cfg.get("command_poll_sec", 0.3))
    state_writer = StateWriter(
        controller,
        daemon_info,
        interval=cfg.get("status_interval_sec", 1.0),
    )
    watchdog = Watchdog(player, controller, interval=2.0)
    scheduler = Scheduler(controller, poll_sec=5.0)

    for t in (cmd_bus, state_writer, watchdog, scheduler):
        t.start()

    # 6. Signal handling
    stop_evt = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        stop_evt.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("daemon ready")
    try:
        while not stop_evt.is_set():
            stop_evt.wait(timeout=1.0)
    except KeyboardInterrupt:
        log.info("keyboard interrupt, shutting down")
        stop_evt.set()
    finally:
        _shutdown(cmd_bus, state_writer, watchdog, scheduler, player)
    return 0


def _shutdown(cmd_bus, state_writer, watchdog, scheduler, player):
    log.info("shutting down workers")
    for t in (cmd_bus, state_writer, watchdog, scheduler):
        t.stop()
    for t in (cmd_bus, state_writer, watchdog, scheduler):
        t.join(timeout=3.0)
        if t.is_alive():
            log.warning("thread %s did not stop in time", t.name)
    try:
        player.stop()
    except Exception:
        log.exception("error stopping player")
    log.info("daemon stopped")


def _iso_from_ts(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(main())