"""
capture.py | PostToolUse hook: the tape.

Records every Robinhood tool result so prices and fills never pass through the AI's memory.
Never blocks anything. Always exits 0 with nothing on stdout. Errors go to
state/capture_errors.log.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

KEEP_DAYS = 30
CLEANUP_EVERY_SEC = 3600
OUTPUT_KEYS = ("tool_response", "tool_output", "tool_result")   # confirmed in Phase 1 via _keys.json


def capture(stdin_text: str, root: Path, now: datetime) -> None:
    from broker import order_ids_in, parse, TapeError
    from deskconfig import role_of, short_name
    from state import Paths, append_line, atomic_write_json, iso, log_event, read_json

    paths = Paths(root)
    paths.tape.mkdir(parents=True, exist_ok=True)
    data = json.loads(stdin_text)
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    keys_file = paths.tape / "_keys.json"
    if not keys_file.exists():
        atomic_write_json(keys_file, sorted(data.keys()))
    tool_name = str(data["tool_name"])
    response = next((data[k] for k in OUTPUT_KEYS if k in data), None)
    entry = {"ts": iso(now), "tool_name": tool_name, "tool_input": data.get("tool_input"),
             "tool_response": response}
    short = short_name(tool_name)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    path = paths.tape / f"{stamp}_{short}.json"
    n = 1
    while path.exists():
        path = paths.tape / f"{stamp}-{n}_{short}.json"
        n += 1
    atomic_write_json(path, entry)
    atomic_write_json(paths.tape / f"latest_{short}.json", entry)
    log_event(paths, "tape", now, tool=tool_name)

    tools = read_json(paths.tools, {}) or {}
    role = role_of(tools.get("tools", {}).get(tool_name, {}))
    if role in ("orders", "place", "cancel"):
        for order_id in order_ids_in(role, response):
            safe = "".join(ch for ch in order_id if ch.isalnum() or ch in "-_")
            if safe:
                atomic_write_json(paths.tape / f"order_{safe}.json", entry)
    cleanup(paths, tools, now, parse, TapeError)


def cleanup(paths, tools, now: datetime, parse, TapeError) -> None:
    from deskconfig import role_of
    from state import atomic_write_text

    marker = paths.tape / "_cleanup"
    if marker.exists() and now.timestamp() - marker.stat().st_mtime < CLEANUP_EVERY_SEC:
        return
    atomic_write_text(marker, now.isoformat())
    cutoff = (now - timedelta(days=KEEP_DAYS)).timestamp()
    for path in paths.tape.glob("*.json"):
        name = path.name
        if name.startswith("latest_") or name.startswith("_") or path.stat().st_mtime >= cutoff:
            continue
        if name.startswith("order_") and not _order_closed(path, tools, parse, TapeError, role_of):
            continue                       # keep order files unless we can prove the order is done
        path.unlink(missing_ok=True)


def _order_closed(path: Path, tools, parse, TapeError, role_of) -> bool:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        role = role_of(tools.get("tools", {}).get(entry["tool_name"], {}))
        found = parse(role, entry["tool_response"])
        orders = found if isinstance(found, list) else [found]
        order_id = path.stem[len("order_"):]
        return any(o is not None and o.id == order_id and not o.is_live for o in orders)
    except (TapeError, OSError, ValueError, KeyError, TypeError):
        return False


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        capture(sys.stdin.read(), ROOT, now)
    except BaseException:                  # never block, never print
        try:
            log = ROOT / "state" / "capture_errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{now.isoformat()} {traceback.format_exc(limit=3)}\n")
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
