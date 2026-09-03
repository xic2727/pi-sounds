"""Cross-process file locking built on ``fcntl.flock``.

The lock file is the data file with a ``.lock`` suffix appended, because
``os.replace`` swaps the inode and would invalidate any lock held on the
data file itself. Strict lock ordering — config > playlists > schedules >
commands > status — combined with a hard timeout eliminates deadlocks.

On non-POSIX platforms (Windows dev box) we fall back to an in-process
``threading.Lock``. That is sufficient for unit tests on a single process
but is **not** safe across processes; deployment target is Linux/POSIX.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Generator

_IS_POSIX = os.name == "posix"
if _IS_POSIX:
    import fcntl  # type: ignore[import-not-found]


# Fallback for non-POSIX: a single global mutex. Sufficient for unit tests
# in a single process; not safe for cross-process locking.
_INPROC_LOCK = threading.Lock()


class LockTimeout(RuntimeError):
    """Raised when the lock cannot be acquired before ``timeout`` seconds."""


@contextlib.contextmanager
def file_lock(
    path: str,
    shared: bool = False,
    timeout: float = 5.0,
) -> Generator[None, None, None]:
    """Acquire an advisory lock for ``path``.

    Args:
        path: Path to the data file. The actual lock file is ``path + ".lock"``.
        shared: ``True`` for a read/shared lock (POSIX only); ``False`` for exclusive.
        timeout: Maximum seconds to wait before raising ``LockTimeout``.
    """
    if _IS_POSIX:
        _posix_lock(path, shared, timeout)
    else:
        _dev_lock(path, shared, timeout)
    try:
        yield
    finally:
        if _IS_POSIX:
            _posix_unlock(path)
        else:
            _dev_unlock()


# ---------------------------------------------------------------------------
# POSIX implementation (fcntl.flock)
# ---------------------------------------------------------------------------

# Per-thread file descriptors so concurrent locks on the same path from the
# same process don't accidentally release someone else's lock. Linux's flock
# is per-fd (advisory on the open file description), so we keep a thread-local
# cache keyed by (path, shared).
import threading as _threading
_TLS = _threading.local()


def _posix_lock(path: str, shared: bool, timeout: float) -> None:
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o664)
    flag = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, flag)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                os.close(fd)
                raise LockTimeout(f"file lock timeout: {path}")
            time.sleep(0.02)
    # Stash the fd for the matching _posix_unlock call
    if not hasattr(_TLS, "_fds"):
        _TLS._fds = []
    _TLS._fds.append((path, fd))


def _posix_unlock(_path: str) -> None:
    if not hasattr(_TLS, "_fds") or not _TLS._fds:
        return
    # Pop the most recent matching entry; the lock context manager is
    # expected to be properly nested.
    for i in range(len(_TLS._fds) - 1, -1, -1):
        path, fd = _TLS._fds[i]
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        _TLS._fds.pop(i)
        return


# ---------------------------------------------------------------------------
# Non-POSIX fallback (single-process only)
# ---------------------------------------------------------------------------

def _dev_lock(path: str, shared: bool, timeout: float) -> None:
    # Shared/exclusive distinction is meaningless for a single mutex; treat
    # shared as exclusive so reads block on writes.
    if not _INPROC_LOCK.acquire(timeout=timeout):
        raise LockTimeout(f"in-proc lock timeout: {path}")


def _dev_unlock() -> None:
    try:
        _INPROC_LOCK.release()
    except RuntimeError:
        # Defensive: should never happen if locking protocol is respected.
        pass