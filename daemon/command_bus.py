"""Poll ``commands.json`` and dispatch each command to a handler.

Commands are processed in arrival order; the bus clears the ``commands``
list (and bumps ``seq``) once each batch has been handled. Each handler's
result is appended to the controller's command-result log so the UI can
display recent successes/failures.

Unknown actions and handler exceptions are recorded as failures, never
crashed. The bus always survives transient storage errors.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from daemon.config import get_logger
from daemon.controller import Controller
from daemon import store
from shared.schemas import now_iso

log = get_logger("command-bus")


Handler = Callable[[Controller, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Per-action handlers
# ---------------------------------------------------------------------------

def _cmd_pause(c: Controller, _a: dict[str, Any]) -> None:
    c.pause()


def _cmd_resume(c: Controller, _a: dict[str, Any]) -> None:
    c.resume()


def _cmd_toggle_pause(c: Controller, _a: dict[str, Any]) -> None:
    c.toggle_pause()


def _cmd_stop(c: Controller, _a: dict[str, Any]) -> None:
    c.stop()


def _cmd_set_volume(c: Controller, a: dict[str, Any]) -> None:
    if "volume" not in a:
        raise ValueError("'volume' arg required")
    c.set_volume(int(a["volume"]))


def _cmd_seek(c: Controller, a: dict[str, Any]) -> None:
    if "seconds" not in a:
        raise ValueError("'seconds' arg required")
    mode = a.get("mode", "relative")
    c.seek(float(a["seconds"]), mode)


def _cmd_ping(_c: Controller, _a: dict[str, Any]) -> None:
    return None


def _cmd_next(c: Controller, _a: dict[str, Any]) -> None:
    c.next()


def _cmd_prev(c: Controller, _a: dict[str, Any]) -> None:
    c.prev()


def _cmd_set_mode(c: Controller, a: dict[str, Any]) -> None:
    mode = a.get("mode")
    if not mode:
        raise ValueError("'mode' arg required")
    c.set_mode(str(mode))


def _cmd_set_playlist(c: Controller, a: dict[str, Any]) -> None:
    pid = a.get("playlist_id")
    if not pid:
        raise ValueError("'playlist_id' arg required")
    autoplay = bool(a.get("autoplay", True))
    index = int(a.get("index", 0))
    if autoplay:
        c.play_playlist(str(pid), index)
    else:
        # Just queue, don't start
        from daemon import store
        playlist = store.load_playlist(str(pid))
        if playlist is None:
            raise ValueError(f"playlist not found: {pid!r}")
        # Load playlist into the engine but do not start playback.
        c.queue.load_playlist(playlist, start_index=index)
        c.pause()  # ensure not playing


def _cmd_reload_config(c: Controller, _a: dict[str, Any]) -> None:
    cfg = store.load_config()
    c.apply_config(cfg)


def _cmd_rescan_library(_c: Controller, _a: dict[str, Any]) -> None:
    """Mark missing flags on all playlists' items and bump an mtime marker.

    The UI's settings page surfaces rescan results via the playlist
    files (each item has ``missing`` updated). This handler does the
    actual disk check.
    """
    from daemon import library
    from shared import paths
    from shared.locking import file_lock

    cfg = store.load_config()
    audio_dir = cfg.get("audio_dir") or ""
    # Iterate playlist files and update each in place.
    playlists_dir = paths.playlists_dir()
    if not playlists_dir.exists():
        return
    for entry in playlists_dir.iterdir():
        if not entry.name.endswith(".json"):
            continue
        if entry.name.startswith("."):
            continue
        try:
            doc = store.read_json(entry, default=None, repair=False)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        items = doc.get("items") or []
        if items:
            library.check_files_exist(items, audio_dir)
            store.write_json(entry, doc)


def _cmd_play(c: Controller, a: dict[str, Any]) -> None:
    playlist_id = a.get("playlist_id")
    path = a.get("path")
    title = a.get("title")
    if playlist_id:
        c.play_playlist(str(playlist_id), int(a.get("index", 0)))
        return
    if not path:
        raise ValueError("either 'playlist_id' or 'path' arg required")
    c.play_file(str(path), title)


COMMANDS: dict[str, Handler] = {
    "ping": _cmd_ping,
    "play": _cmd_play,
    "pause": _cmd_pause,
    "resume": _cmd_resume,
    "toggle_pause": _cmd_toggle_pause,
    "stop": _cmd_stop,
    "set_volume": _cmd_set_volume,
    "seek": _cmd_seek,
    "next": _cmd_next,
    "prev": _cmd_prev,
    "set_mode": _cmd_set_mode,
    "set_playlist": _cmd_set_playlist,
    "reload_config": _cmd_reload_config,
    "rescan_library": _cmd_rescan_library,
}


# ---------------------------------------------------------------------------
# CommandBus
# ---------------------------------------------------------------------------

class CommandBus(threading.Thread):
    """Poll-and-dispatch loop running in its own thread."""

    def __init__(self, controller: Controller, poll_sec: float = 0.3):
        super().__init__(daemon=True, name="command-bus")
        self.controller = controller
        self.poll_sec = max(0.05, float(poll_sec))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("command bus starting (poll %.2fs)", self.poll_sec)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("command bus tick failed")
            self._stop_event.wait(self.poll_sec)
        log.info("command bus stopped")

    def _tick(self) -> None:
        doc = store.load_commands()
        cmds = list(doc.get("commands") or [])
        if not cmds:
            return
        results = [self._dispatch(cmd) for cmd in cmds]
        # Clear the queue once everything is handled. seq bumps on every
        # consumed batch so the UI can tell its writes were acknowledged.
        store.save_commands({
            "version": 1,
            "seq": doc.get("seq", 0) + 1,
            "commands": [],
        })
        self.controller.record_command_results(results)

    def _dispatch(self, cmd: dict[str, Any]) -> dict[str, Any]:
        cid = cmd.get("id")
        action = cmd.get("action") or ""
        args = cmd.get("args") or {}
        handler = COMMANDS.get(action)
        result: dict[str, Any] = {
            "id": cid,
            "action": action,
            "ok": True,
            "msg": "",
            "ts": now_iso(),
        }
        if handler is None:
            result.update({"ok": False, "msg": f"unknown action: {action!r}"})
            log.warning("unknown action: %r", action)
            return result
        try:
            handler(self.controller, args)
        except NotImplementedError as e:
            result.update({"ok": False, "msg": str(e)})
        except Exception as e:
            result.update({"ok": False, "msg": f"{type(e).__name__}: {e}"})
            log.exception("command %r failed", action)
        return result


__all__ = ["CommandBus", "COMMANDS"]