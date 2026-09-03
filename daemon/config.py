"""Logging and shared configuration helpers."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make sure ``shared`` is importable when the daemon is launched directly
# (e.g. ``python daemon/main.py`` from the project root).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger. Idempotent: safe to call multiple times."""
    root = logging.getLogger()
    if root.handlers:  # already configured
        return root
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


DAEMON_NAME = "pi-sounds-daemon"