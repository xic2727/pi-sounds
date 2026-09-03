"""Cron-based scheduler thread.

Runs alongside the command bus in the daemon. For every enabled schedule
it computes the next fire time via ``croniter`` and waits in 1-second
ticks. When the deadline passes, it invokes the controller to play the
scheduled playlist.

Same-minute multiple fires are resolved by ``priority`` (descending) then
``id`` (ascending) — only the highest-priority schedule wins; others are
recorded as ``skipped`` so the UI can show what happened.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any

from croniter import croniter, CroniterBadCronError

from daemon import store
from daemon.config import get_logger
from daemon.controller import Controller
from shared.schemas import now_iso

log = get_logger("scheduler")


TICK_SEC = 1.0
MISSED_WINDOW_SEC = 60


def validate_cron(expr: str) -> tuple[bool, str]:
    """Return ``(ok, message)`` — ``ok`` is True iff ``croniter(expr)`` constructs."""
    if not isinstance(expr, str) or not expr.strip():
        return False, "cron 表达式为空"
    try:
        croniter(expr, datetime.now())
        return True, ""
    except (CroniterBadCronError, ValueError, KeyError, TypeError) as e:
        return False, str(e)


def next_fire(cron_expr: str, after: datetime | None = None) -> datetime | None:
    if after is None:
        after = datetime.now()
    try:
        return croniter(cron_expr, after).get_next(datetime)
    except (CroniterBadCronError, ValueError, KeyError, TypeError):
        return None


class Scheduler(threading.Thread):
    def __init__(self, controller: Controller, poll_sec: float = 5.0):
        super().__init__(daemon=True, name="scheduler")
        self.controller = controller
        self.poll_sec = max(1.0, float(poll_sec))
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._schedules_mtime: float = 0.0
        self._schedules: list[dict[str, Any]] = []
        self._next_fire: dict[str, datetime] = {}
        self._next_fire_lock = threading.Lock()
        self._next_schedule_meta: dict[str, Any] | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("scheduler starting (poll %.1fs)", self.poll_sec)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop_event.wait(self.poll_sec)
        log.info("scheduler stopped")

    # ----- introspection for status writer / UI -----------------------

    def next_fire_summary(self) -> dict[str, Any] | None:
        """Return the soonest enabled schedule's id/name/fire_at or None."""
        with self._next_fire_lock:
            return self._next_schedule_meta

    # ----- internal ---------------------------------------------------

    def _tick(self) -> None:
        self._reload_if_changed()
        now = datetime.now()
        due = self._collect_due(now)
        if not due:
            self._update_next_meta(now)
            return

        # Resolve conflicts: keep the highest-priority schedule only.
        winner = self._pick_winner(due)
        losers = [s for s in due if s["id"] != winner["id"]]
        for s in losers:
            log.info(
                "skipping schedule %r (priority %d) due to conflict with %r",
                s.get("name"), s.get("priority", 0), winner.get("name"),
            )
        self._fire(winner)
        # Bump losers' next_fire so we don't re-consider them next tick.
        for s in losers:
            self._recompute_next(s, now)
        self._update_next_meta(now)

    def _reload_if_changed(self) -> None:
        path = store.paths.schedules_path()
        from pathlib import Path
        p = Path(path)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        with self._lock:
            if mtime == self._schedules_mtime:
                return
            self._schedules_mtime = mtime
        # Reload from disk
        try:
            doc = store.load_schedules()
        except Exception:
            log.exception("failed to load schedules.json")
            return
        schedules = doc.get("schedules") or []
        valid: list[dict[str, Any]] = []
        for s in schedules:
            if not s.get("enabled", True):
                continue
            ok, msg = validate_cron(s.get("cron", ""))
            if not ok:
                log.warning("schedule %r has invalid cron %r: %s",
                            s.get("id"), s.get("cron"), msg)
                continue
            valid.append(s)
        with self._lock:
            self._schedules = valid
        # Recompute next_fire for everything
        self._recompute_all_next(datetime.now())

    def _recompute_all_next(self, now: datetime) -> None:
        with self._next_fire_lock:
            self._next_fire.clear()
            for s in self._schedules:
                self._next_fire[s["id"]] = self._calc_next(s, now)

    def _recompute_next(self, s: dict[str, Any], now: datetime) -> None:
        with self._next_fire_lock:
            self._next_fire[s["id"]] = self._calc_next(s, now)

    def _calc_next(self, s: dict[str, Any], now: datetime) -> datetime | None:
        return next_fire(s.get("cron", ""), now)

    def _collect_due(self, now: datetime) -> list[dict[str, Any]]:
        with self._next_fire_lock, self._lock:
            due: list[dict[str, Any]] = []
            for s in self._schedules:
                nf = self._next_fire.get(s["id"])
                if nf is None:
                    continue
                if nf <= now:
                    # Don't fire if we missed the deadline by more than the window
                    if (now - nf) > timedelta(seconds=MISSED_WINDOW_SEC):
                        log.info(
                            "schedule %r missed by %s, skipping",
                            s.get("name"), now - nf,
                        )
                        self._next_fire[s["id"]] = self._calc_next(s, now)
                        continue
                    due.append(s)
        return due

    def _pick_winner(self, due: list[dict[str, Any]]) -> dict[str, Any]:
        # Highest priority (descending), then smallest id (ascending).
        return sorted(
            due,
            key=lambda s: (-int(s.get("priority", 0)), str(s.get("id", ""))),
        )[0]

    def _fire(self, s: dict[str, Any]) -> None:
        pid = s.get("playlist_id")
        name = s.get("name") or s.get("id")
        if_busy = s.get("if_busy", "preempt")
        log.info("firing schedule %r -> playlist %r (if_busy=%s)",
                 name, pid, if_busy)
        try:
            playlist = store.load_playlist(str(pid)) if pid else None
        except Exception as e:
            log.warning("could not load playlist %r: %s", pid, e)
            playlist = None
        if playlist is None:
            self._record_result(s, "playlist_missing")
            return

        # Check if_busy
        playing_now = self.controller.state == "playing"
        if playing_now and if_busy == "skip":
            self._record_result(s, "skipped_busy")
            return

        # Apply per-schedule volume/mode overrides
        vol_override = s.get("volume")
        if vol_override is not None:
            try:
                self.controller.set_volume(int(vol_override), persist=False)
            except Exception:
                log.exception("schedule volume override failed")
        mode_override = s.get("play_mode")
        if mode_override and schemas_validate(mode_override):
            try:
                self.controller.set_mode(str(mode_override))
            except Exception:
                log.exception("schedule mode override failed")

        try:
            self.controller.play_playlist(str(pid), 0)
            self._record_result(s, "ok")
        except Exception as e:
            log.exception("schedule fire failed")
            self._record_result(s, f"error: {e}")

    def _record_result(self, s: dict[str, Any], result: str) -> None:
        """Persist last_run / last_result into schedules.json."""
        sid = s.get("id")
        if not sid:
            return

        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            for item in doc.get("schedules", []):
                if item.get("id") == sid:
                    item["last_run"] = now_iso()
                    item["last_result"] = result
                    break
            return doc

        try:
            store.update_json(store.paths.schedules_path(), _mutate,
                              default={"version": 1, "schedules": []})
            # Invalidate our cache so next reload picks up the change
            with self._lock:
                self._schedules_mtime = 0.0
        except Exception:
            log.exception("failed to record schedule result")

    def _update_next_meta(self, now: datetime) -> None:
        with self._next_fire_lock, self._lock:
            best: tuple[str, dict, datetime] | None = None
            for s in self._schedules:
                nf = self._next_fire.get(s["id"])
                if nf is None:
                    continue
                if best is None or nf < best[2]:
                    best = (s["id"], s, nf)
            if best is None:
                self._next_schedule_meta = None
                return
            sid, s, nf = best
            self._next_schedule_meta = {
                "id": sid,
                "name": s.get("name"),
                "at": nf.isoformat(timespec="seconds"),
            }


def schemas_validate(mode: str) -> bool:
    from shared import schemas
    return schemas.validate_play_mode(mode)


__all__ = ["Scheduler", "validate_cron", "next_fire"]