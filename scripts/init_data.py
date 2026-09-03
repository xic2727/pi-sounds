#!/usr/bin/env python3
"""Initialize the pi-sounds data directory. Idempotent — safe to re-run.

Creates the data and playlists directories, writes the canonical config,
schedules, and commands JSON files (only if they don't already exist), and
optionally creates the audio directory.

Usage:
    python scripts/init_data.py --audio-dir /home/pi/sounds
    PI_SOUNDS_DATA=/var/lib/pi-sounds python scripts/init_data.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from shared import paths, schemas  # noqa: E402
from daemon.store import write_json  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Initialize pi-sounds data dir.")
    p.add_argument(
        "--audio-dir",
        default=str(Path.home() / "sounds"),
        help="Audio directory (default: ~/sounds)",
    )
    p.add_argument(
        "--data-dir",
        default=None,
        help="Override data dir (default: $PI_SOUNDS_DATA or ~/.local/share/pi-sounds/data)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config.json",
    )
    args = p.parse_args()

    if args.data_dir:
        os.environ["PI_SOUNDS_DATA"] = args.data_dir

    data_dir = paths.data_dir()
    playlists_dir = paths.playlists_dir()
    cfg_path = paths.config_path()
    sch_path = paths.schedules_path()
    cmd_path = paths.commands_path()

    # Ensure directories
    data_dir.mkdir(parents=True, exist_ok=True)
    playlists_dir.mkdir(parents=True, exist_ok=True)

    # Audio dir (may not be creatable if it's on an unmounted volume; warn but don't fail)
    audio_dir = Path(args.audio_dir).expanduser()
    try:
        audio_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"WARN: cannot create audio dir {audio_dir}: {e}", file=sys.stderr)

    # Config
    cfg = dict(schemas.DEFAULT_CONFIG)
    cfg["audio_dir"] = str(audio_dir)
    if not cfg_path.exists() or args.force:
        write_json(cfg_path, cfg)
        print(f"Created  {cfg_path}")
    else:
        print(f"Exists   {cfg_path}")

    # Schedules
    if not sch_path.exists() or args.force:
        write_json(sch_path, dict(schemas.DEFAULT_SCHEDULES))
        print(f"Created  {sch_path}")
    else:
        print(f"Exists   {sch_path}")

    # Commands
    if not cmd_path.exists() or args.force:
        write_json(cmd_path, dict(schemas.DEFAULT_COMMANDS))
        print(f"Created  {cmd_path}")
    else:
        print(f"Exists   {cmd_path}")

    # status.json is owned by the daemon; we don't write it here.

    print()
    print(f"Data dir:      {data_dir}")
    print(f"Playlists dir: {playlists_dir}")
    print(f"Audio dir:     {audio_dir}")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())