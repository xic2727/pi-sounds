"""mpv subprocess wrapper using JSON-over-Unix-socket IPC.

Thread model
------------
* API methods (loadfile, pause, ...) are safe to call from any thread.
* A single reader thread reads lines from the IPC socket and dispatches
  to either a pending ``Future`` (response to a command) or to the
  event callback (events like ``end-file``).
* A single write lock serialises command sends so ``request_id`` never
  collides.

Watchdog
--------
``is_alive`` checks whether the mpv subprocess has exited. The daemon
polls this on a timer and calls ``restart()`` if mpv died.

Crash recovery
--------------
``restart()`` records the last known position, respawns mpv with the
same volume, and reloads the previously playing track near where it
crashed (best-effort). The track path is set whenever ``loadfile``
succeeds.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

from daemon.config import get_logger

log = get_logger("player")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PlayerError(RuntimeError):
    """Generic mpv control error."""


class PlayerStartError(PlayerError):
    """Raised when mpv cannot be started."""


# Property names we mirror into the local cache via observe_property.
_OBSERVED_PROPERTIES = ("time-pos", "duration", "pause", "volume", "path")


# ---------------------------------------------------------------------------
# MpvPlayer
# ---------------------------------------------------------------------------

EventCallback = Callable[[dict], None]
PropertyCallback = Callable[[str, Any], None]


class MpvPlayer:
    """Controls a single mpv subprocess via JSON IPC."""

    def __init__(
        self,
        socket_path: os.PathLike | str,
        *,
        mpv_binary: str = "mpv",
        audio_device: str = "auto",
        initial_volume: int = 60,
        on_event: EventCallback | None = None,
        on_property: PropertyCallback | None = None,
    ):
        self.socket_path = Path(socket_path)
        self.mpv_binary = mpv_binary
        self.audio_device = audio_device
        self.initial_volume = max(0, min(100, int(initial_volume)))
        self.on_event = on_event
        self.on_property = on_property

        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, Future] = {}
        self._pending_lock = threading.Lock()

        # Last track we successfully asked to play; used for crash recovery.
        self._current_track_path: str | None = None
        self._was_playing_before: bool = False
        self._restart_count: int = 0

        # Cached properties updated via observe_property events.
        self._properties: dict[str, Any] = {
            "pause": True,
            "time-pos": 0.0,
            "duration": 0.0,
            "volume": float(self.initial_volume),
            "path": None,
        }

    # ----- lifecycle ----------------------------------------------------

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def restart_count(self) -> int:
        return self._restart_count

    def get_cached_property(self, name: str, default: Any = None) -> Any:
        return self._properties.get(name, default)

    def start(self) -> None:
        """Spawn mpv and connect to its IPC socket. Raises on failure."""
        if self.is_alive:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove stale socket file left over from a crashed mpv.
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

        cmd = [
            self.mpv_binary,
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--really-quiet",
            f"--input-ipc-server={self.socket_path}",
            "--audio-display=no",
            f"--volume={self.initial_volume}",
            f"--audio-device={self.audio_device}",
        ]
        log.info("spawning mpv: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise PlayerStartError(
                f"mpv binary not found: {self.mpv_binary}"
            ) from e

        # Wait up to 5s for the socket file to appear.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                break
            if self._proc.poll() is not None:
                rc = self._proc.returncode
                self._proc = None
                raise PlayerStartError(f"mpv exited during startup (code={rc})")
            time.sleep(0.05)
        else:
            self._terminate_proc()
            raise PlayerStartError(
                f"mpv socket did not appear within 5s: {self.socket_path}"
            )

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(self.socket_path))
            sock.settimeout(None)  # blocking reads in reader thread
        except OSError as e:
            self._terminate_proc()
            raise PlayerStartError(f"cannot connect to mpv socket: {e}") from e

        self._sock = sock
        self._stop_reader.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="mpv-reader", daemon=True
        )
        self._reader_thread.start()

        # Subscribe to the properties we care about. observe_property ids
        # 1..N correspond to _OBSERVED_PROPERTIES indices.
        for idx, name in enumerate(_OBSERVED_PROPERTIES, start=1):
            try:
                self.command("observe_property", idx, name, timeout=3.0)
            except Exception as e:
                log.warning("observe_property %s failed: %s", name, e)

    def stop(self) -> None:
        """Gracefully stop mpv and disconnect."""
        self._stop_reader.set()
        if self._sock is not None:
            try:
                # Best-effort quit; ignore errors if mpv already died.
                with self._write_lock:
                    msg = {"command": ["quit"]}
                    try:
                        self._sock.sendall(
                            (json.dumps(msg) + "\n").encode("utf-8")
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._terminate_proc()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3.0)
            self._reader_thread = None
        # Cancel any pending requests so callers don't block forever.
        with self._pending_lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PlayerError("mpv stopped"))
            self._pending.clear()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def restart(self) -> None:
        """Stop and start mpv, then attempt to resume the last track."""
        log.warning("restarting mpv (restarts so far: %d)", self._restart_count)
        # Capture state before tearing down.
        last_pos = self._properties.get("time-pos") or 0.0
        last_path = self._current_track_path
        was_playing = not (self._properties.get("pause", True))
        self._restart_count += 1
        self.stop()
        try:
            self.start()
        except PlayerStartError:
            log.exception("mpv restart failed; staying down")
            return
        try:
            self.set_property("volume", self.initial_volume)
        except Exception:
            pass
        if last_path:
            try:
                self.loadfile(last_path)
                if was_playing and last_pos > 1.0:
                    self.command("seek", last_pos, "absolute", timeout=3.0)
                if not was_playing:
                    self.pause()
            except Exception as e:
                log.warning("resume after restart failed: %s", e)

    # ----- command helpers ---------------------------------------------

    def command(self, *args: Any, timeout: float = 5.0) -> Any:
        """Send a raw mpv command and wait for the response."""
        if self._sock is None:
            raise PlayerError("mpv is not running")
        with self._write_lock:
            with self._pending_lock:
                req_id = self._next_id
                self._next_id += 1
                fut: Future = Future()
                self._pending[req_id] = fut
            payload = {"command": list(args), "request_id": req_id}
            try:
                data = (json.dumps(payload) + "\n").encode("utf-8")
                self._sock.sendall(data)
            except Exception as e:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                raise PlayerError(f"send to mpv failed: {e}") from e
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout as e:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise PlayerError(f"mpv command timeout: {args!r}") from e

    def loadfile(self, path: str, mode: str = "replace") -> None:
        """Load a file into mpv. mode is one of replace/append/play-next."""
        self._current_track_path = path
        self.command("loadfile", path, mode)

    def stop_playback(self) -> None:
        """Stop current playback (mpv 'stop' command)."""
        if self._sock is None:
            return
        try:
            self.command("stop")
        except PlayerError:
            pass

    def pause(self) -> None:
        self.set_property("pause", True)
        self._was_playing_before = False

    def resume(self) -> None:
        self.set_property("pause", False)
        self._was_playing_before = True

    def toggle_pause(self) -> None:
        cur = bool(self._properties.get("pause", False))
        self.set_property("pause", not cur)
        self._was_playing_before = cur  # will become the opposite of what it was

    def set_volume(self, vol: int) -> None:
        n = max(0, min(150, int(vol)))  # mpv allows >100, but UI clamps to 100
        self.set_property("volume", n)

    def get_property(self, name: str) -> Any:
        return self.command("get_property", name)

    def set_property(self, name: str, value: Any) -> None:
        self.command("set_property", name, value)

    def seek(self, seconds: float, mode: str = "relative") -> None:
        self.command("seek", float(seconds), mode)

    # ----- reader thread -----------------------------------------------

    def _reader_loop(self) -> None:
        assert self._sock is not None
        sock = self._sock
        buf = b""
        while not self._stop_reader.is_set():
            try:
                chunk = sock.recv(65536)
            except OSError as e:
                if self._stop_reader.is_set():
                    break
                log.warning("mpv socket read error: %s", e)
                break
            if not chunk:
                log.info("mpv socket EOF (process likely exited)")
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    log.warning("non-JSON line from mpv: %r", line[:120])
                    continue
                self._dispatch(msg)
        log.info("mpv reader thread exiting")

    def _dispatch(self, msg: dict) -> None:
        if "request_id" in msg:
            req_id = msg["request_id"]
            with self._pending_lock:
                fut = self._pending.pop(req_id, None)
            if fut is not None and not fut.done():
                if msg.get("error") and msg["error"] != "success":
                    fut.set_exception(
                        PlayerError(f"mpv error: {msg['error']}")
                    )
                else:
                    fut.set_result(msg.get("data"))
            return
        if "event" in msg:
            self._handle_event(msg)
            return
        if "name" in msg and "data" in msg:
            self._handle_property(msg)
            return
        log.debug("unknown mpv message: %s", msg)

    def _handle_event(self, evt: dict) -> None:
        log.debug("mpv event: %s", evt.get("event"))
        if self.on_event is not None:
            try:
                self.on_event(evt)
            except Exception:
                log.exception("on_event callback raised")

    def _handle_property(self, msg: dict) -> None:
        name = msg["name"]
        value = msg["data"]
        self._properties[name] = value
        if self.on_property is not None:
            try:
                self.on_property(name, value)
            except Exception:
                log.exception("on_property callback raised")

    # ----- helpers ------------------------------------------------------

    def _terminate_proc(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            log.exception("error terminating mpv")
        self._proc = None


# ---------------------------------------------------------------------------
# Self-test entry point
# ---------------------------------------------------------------------------

def _self_test(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Self-test the MpvPlayer wrapper.")
    p.add_argument("file", nargs="?", help="audio file to play (omit to just start/stop)")
    p.add_argument("--volume", type=int, default=60)
    p.add_argument("--device", default="auto")
    p.add_argument("--socket", default="/tmp/pi-sounds-test.sock")
    p.add_argument("--duration", type=float, default=5.0,
                   help="how long to play before exiting (seconds)")
    args = p.parse_args(argv)

    events: list[dict] = []
    player = MpvPlayer(
        socket_path=args.socket,
        audio_device=args.device,
        initial_volume=args.volume,
        on_event=lambda e: events.append(e),
    )

    try:
        player.start()
    except PlayerStartError as e:
        print(f"START FAILED: {e}", file=sys.stderr)
        return 2
    print(f"started, mpv pid={player._proc.pid if player._proc else '?'}")

    if args.file:
        player.loadfile(args.file)
        player.resume()
        print(f"playing {args.file} for up to {args.duration}s")
        end = time.monotonic() + args.duration
        last_print = 0.0
        while time.monotonic() < end:
            time.sleep(0.5)
            now = time.monotonic()
            if now - last_print >= 1.0:
                last_print = now
                pos = player.get_cached_property("time-pos", 0.0)
                dur = player.get_cached_property("duration", 0.0)
                pause = player.get_cached_property("pause", True)
                print(f"  t={pos:.2f}/{dur:.2f} pause={pause}")
        print("events:", [e.get("event") for e in events])
    else:
        print("(no file given, just verifying start/stop)")

    player.stop()
    print("stopped OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test(sys.argv[1:]))