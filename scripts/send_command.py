#!/usr/bin/env python3
"""Send a command to the daemon via commands.json.

Useful for ad-hoc control and integration testing before the UI exists.

Examples:
    python scripts/send_command.py ping
    python scripts/send_command.py play --playlist morning
    python scripts/send_command.py set_volume 80
    python scripts/send_command.py next
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from shared import paths  # noqa: E402
from daemon.store import update_json  # noqa: E402
from shared.schemas import make_command  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Send a command to the pi-sounds daemon.")
    p.add_argument("action", help="action name (e.g. play, pause, set_volume, ping)")
    p.add_argument("--show", action="store_true", help="print the generated command and exit")
    p.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="key=value pairs to use as the action's args (use -- to separate)",
    )
    args = p.parse_args()
    # argparse.REMAINDER captures everything after the first positional,
    # including option-like strings. Strip --show out of the remainder so
    # users can write `send_command.py play --show key=value ...`.
    if "--show" in args.args:
        args.show = True
        args.args = [a for a in args.args if a != "--show"]

    action_args = {}
    for item in args.args:
        if "=" not in item:
            print(f"arg must be key=value: {item}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        # Try to coerce types
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            pass  # keep as string
        action_args[k] = v

    cmd = make_command(args.action, action_args)
    if args.show:
        print(json.dumps(cmd, ensure_ascii=False, indent=2))
        return 0

    def _append(doc):
        doc.setdefault("commands", [])
        doc["commands"].append(cmd)
        doc["seq"] = doc.get("seq", 0) + 1
        # Trim to last 50 to keep file small
        doc["commands"] = doc["commands"][-50:]
        return doc

    update_json(paths.commands_path(), _append, default={"version": 1, "seq": 0, "commands": []})
    print(f"sent: {cmd['action']} {action_args} (id={cmd['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())