"""
watcher.py | Runs every 5 minutes from Task Scheduler. No AI. Costs nothing.

Usage: watcher.py tick
Each tick: exit if a run holds the lock; write a heartbeat; check every position against its
stop; start DAILY when due (retry every 30 minutes, three retries, then alert and wait for
tomorrow); start WEEKLY once on the review day; alert on a missed or stale run.
A STOP file does not stop the watcher. Protection keeps running.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from state import Paths, atomic_write_json, iso, parse_ts, read_json  # noqa: E402

BREACH_FRAC = 0.005
BREACH_COOLDOWN = timedelta(minutes=30)
RETRY_EVERY = timedelta(minutes=30)
MAX_RETRIES = 3
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _at(now: datetime, hhmm: str) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _success_today(st: dict, job: str, now: datetime, due: datetime) -> bool:
    ts = (st.get("last_success") or {}).get(job)
    return bool(ts) and parse_ts(ts) >= due and parse_ts(ts).date() == now.date()


def public_price(coin: str) -> float | None:
    try:
        from data import BASE_URL, get_json
        return float(get_json(f"{BASE_URL}/{coin}-USD/ticker")["price"])
    except Exception:
        return None


def tick(root: Path, now: datetime, *, run=None, price=None, alert=None) -> list[str]:
    from deskconfig import ConfigError, load_config
    from runner import lock_held, run_job
    paths = Paths(root)
    run = run or (lambda job: run_job(root, job))
    price = price or public_price
    done: list[str] = []
    try:
        cfg = load_config(paths.config)
    except ConfigError as exc:
        _alert(root, {}, alert, "Trend Desk watcher", f"config invalid, nothing started: {exc}", now)
        return ["config invalid"]
    if lock_held(paths, now):
        return ["locked"]
    atomic_write_json(paths.heartbeat, {"ts": iso(now)})
    w = read_json(paths.watcher, {}) or {}
    st = read_json(paths.state, {}) or {}
    sched = cfg["schedule"]
    due = _at(now, sched["daily_run_utc"])
    today = now.date().isoformat()

    breach = []
    last_breach = w.setdefault("breach", {})
    for coin, pos in (st.get("positions") or {}).items():
        p = price(coin)
        if p is None or p >= pos["stop"] * (1 - BREACH_FRAC):
            continue
        last = last_breach.get(coin)
        if last is None or now - parse_ts(last) >= BREACH_COOLDOWN:
            breach.append(coin)

    daily = w.get("daily") if (w.get("daily") or {}).get("date") == today else {"date": today, "attempts": 0}
    job = None
    if now >= due and not _success_today(st, "DAILY", now, due):
        retry_ok = daily["attempts"] == 0 or (
            daily["attempts"] <= MAX_RETRIES and now - parse_ts(daily["last_attempt"]) >= RETRY_EVERY)
        if retry_ok:
            job = "DAILY"
        elif daily["attempts"] > MAX_RETRIES and not daily.get("gave_up"):
            daily["gave_up"] = True
            _alert(root, cfg, alert, "Trend Desk DAILY failed",
                   "Four DAILY attempts failed. Waiting for tomorrow. Stops at Robinhood still protect.", now)
    if job is None and breach:
        job = "RECONCILE"
    if job:
        for coin in breach:
            last_breach[coin] = iso(now)
        result = run(job)
        done.append(f"{job} {'ok' if result.get('ok') else 'failed: ' + str(result.get('reason'))}")
        if job == "DAILY":
            daily["attempts"] += 1
            daily["last_attempt"] = iso(now)
        st = read_json(paths.state, {}) or {}
    w["daily"] = daily

    if DAYS[now.weekday()] == sched["weekly_review_day"] and w.get("weekly_done") != today \
            and _success_today(st, "DAILY", now, due):
        result = run("WEEKLY")
        done.append(f"WEEKLY {'ok' if result.get('ok') else 'failed'}")
        if result.get("ok"):
            w["weekly_done"] = today

    missed = _at(now, sched["missed_daily_alert_utc"])
    if now >= missed and not _success_today(st, "DAILY", now, due) and w.get("missed_alert") != today:
        w["missed_alert"] = today
        _alert(root, cfg, alert, "Trend Desk missed run", "No successful DAILY run yet today.", now)
    stamps = [parse_ts(t) for t in (st.get("last_success") or {}).values() if t]
    stale_limit = timedelta(hours=sched["stale_run_alert_hours"])
    if stamps and now - max(stamps) > stale_limit and w.get("stale_alert") != today:
        w["stale_alert"] = today
        _alert(root, cfg, alert, "Trend Desk stale",
               f"No successful run in over {sched['stale_run_alert_hours']} hours.", now)
    atomic_write_json(paths.watcher, w)
    return done


def _alert(root: Path, cfg: dict, alert, title: str, message: str, now: datetime) -> None:
    if alert is not None:
        alert(title, message)
        return
    from alert import send
    send(root, cfg, title, message, now)


def main(argv: list[str]) -> int:
    if argv != ["tick"]:
        print("usage: watcher.py tick")
        return 1
    now = datetime.now(timezone.utc)
    try:
        for line in tick(ROOT, now):
            print(line)
    except Exception as exc:
        _alert(ROOT, {}, None, "Trend Desk watcher error", f"{type(exc).__name__}: {exc}", now)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
