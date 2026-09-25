"""
state.py | State files for the Trend Desk: atomic writes, file locks, schema checks.

JSON documents (state.json, approvals.json, plans) are written atomically: temp file in
the same folder, flush, fsync, os.replace (which overwrites on Windows, see setup/WINDOWS.md).
Logs (events.jsonl, incidents.jsonl, runs.jsonl, journal.csv) are append-only: one locked,
fsynced write per line. No function here rewrites or deletes a journal row.
Locks: msvcrt on Windows, fcntl on macOS and Linux.
"""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 1
STATES = ("SCANNING", "IN_TRADE", "COOLDOWN_DAY", "COOLDOWN_WEEK", "HALTED", "PAUSED", "DRY")
STICKY = ("HALTED", "PAUSED")          # only a human clears these
JOURNAL_COLUMNS = ["trade_id", "coin", "entry_ts", "entry_fill", "qty", "initial_stop", "exit_ts",
                   "exit_fill", "exit_reason", "pnl_usd", "r_multiple", "spread_paid_usd", "days_held"]
EXIT_REASONS = ("stop", "trail", "emergency", "unknown")
POSITION_KEYS = ("qty", "entry_fill", "entry_ts", "n_at_entry", "initial_stop", "stop")


class StateError(Exception):
    pass


# ---------- time ----------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- paths ----------

class Paths:
    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.tape = self.state_dir / "tape"
        self.plans = self.state_dir / "plans"
        self.state = self.state_dir / "state.json"
        self.approvals = self.state_dir / "approvals.json"
        self.journal = self.state_dir / "journal.csv"
        self.events = self.state_dir / "events.jsonl"
        self.incidents = self.state_dir / "incidents.jsonl"
        self.runs = self.state_dir / "runs.jsonl"
        self.lessons = self.state_dir / "LESSONS.md"
        self.runner_lock = self.state_dir / "runner.lock"
        self.heartbeat = self.state_dir / "watcher_heartbeat.json"
        self.watcher = self.state_dir / "watcher.json"
        self.config = self.root / "config" / "risk.json"
        self.tools = self.root / "config" / "tools.json"
        self.data = self.root / "data"
        self.stop_file = self.root / "STOP"
        self.status_md = self.root / "STATUS.md"
        self.reports = self.root / "reports"
        self.logs = self.root / "logs" / "runs"


# ---------- atomic writes and locks ----------

def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):          # Windows: a reader holding the file blocks replace briefly
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=False) + "\n")


def _lock(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(target: Path, timeout: float = 10.0):
    lock_path = Path(str(target) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock(fh)
                locked = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise StateError(f"lock timeout on {lock_path.name}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                _unlock(fh)
            except OSError:
                pass
        fh.close()


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def append_line(path: Path, line: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line if line.endswith("\n") else line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def append_jsonl(path: Path, obj: dict) -> None:
    append_line(path, json.dumps(obj, separators=(",", ":"), default=str))


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue                   # a torn line is skipped, never trusted
            if isinstance(obj, dict):
                out.append(obj)
    return out


# ---------- state.json ----------

def default_state(cfg: dict) -> dict:
    cap = float(cfg["account"]["starting_capital"])
    return {"schema": SCHEMA, "state": "SCANNING", "reason": "", "peak_equity": cap,
            "last_equity": cap, "account": None, "account_number": None,
            "day": {"date": None, "start_equity": cap, "realized": 0.0},
            "week": {"iso": None, "start_equity": cap, "realized": 0.0},
            "starts": {}, "positions": {}, "exited": {}, "vanished": {},
            "last_success": {"DAILY": None, "RECONCILE": None, "WEEKLY": None}}


def validate_state(st: dict) -> None:
    if not isinstance(st, dict) or st.get("schema") != SCHEMA:
        raise StateError("state.json schema missing or wrong")
    if st.get("state") not in STATES:
        raise StateError(f"state.json has unknown state {st.get('state')!r}")
    for key, kind in (("positions", dict), ("exited", dict), ("last_success", dict),
                      ("day", dict), ("week", dict), ("starts", dict)):
        if not isinstance(st.get(key), kind):
            raise StateError(f"state.json {key} missing or wrong type")
    for num in ("peak_equity", "last_equity"):
        if not isinstance(st.get(num), (int, float)) or st[num] < 0:
            raise StateError(f"state.json {num} invalid")
    for coin, pos in st["positions"].items():
        missing = [k for k in POSITION_KEYS if k not in pos]
        if missing:
            raise StateError(f"position {coin} missing {missing}")
        if not (pos["qty"] > 0 and pos["stop"] > 0 and pos["entry_fill"] > 0):
            raise StateError(f"position {coin} has non-positive numbers")


def load_state(paths: Paths, cfg: dict) -> dict:
    st = read_json(paths.state)
    if st is None:
        return default_state(cfg)
    for key, value in default_state(cfg).items():   # forward-compatible optional keys
        st.setdefault(key, value)
    validate_state(st)
    return st


def save_state(paths: Paths, st: dict) -> None:
    validate_state(st)
    with file_lock(paths.state):
        atomic_write_json(paths.state, st)


# ---------- approvals ----------

def load_approvals(paths: Paths) -> list[dict]:
    items = read_json(paths.approvals, [])
    if not isinstance(items, list):
        raise StateError("approvals.json is not a list")
    return items


@contextmanager
def approvals_locked(paths: Paths):
    """Load, let the caller edit in place, save atomically. All under one lock."""
    with file_lock(paths.approvals):
        items = load_approvals(paths)
        yield items
        atomic_write_json(paths.approvals, items)


# ---------- journal and logs ----------

def append_journal(paths: Paths, row: dict) -> None:
    missing = [c for c in JOURNAL_COLUMNS if c not in row]
    if missing:
        raise StateError(f"journal row missing {missing}")
    if row["exit_reason"] not in EXIT_REASONS:
        raise StateError(f"bad exit_reason {row['exit_reason']!r}")
    need_header = not paths.journal.exists() or paths.journal.stat().st_size == 0
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=JOURNAL_COLUMNS, lineterminator="\n")
    if need_header:
        writer.writeheader()
    writer.writerow({c: row[c] for c in JOURNAL_COLUMNS})
    append_line(paths.journal, buf.getvalue())


def read_journal(paths: Paths) -> list[dict]:
    if not paths.journal.exists():
        return []
    with open(paths.journal, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def log_event(paths: Paths, type_: str, now: datetime | None = None, **fields) -> None:
    append_jsonl(paths.events, {"ts": iso(now or utcnow()), "type": type_, **fields})
