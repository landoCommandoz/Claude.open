"""
entries.py | engine.py plan, steps 6 to 11: data check, ratchets, entries, stop refresh,
then numbering, approvals, and the saved plan. All signal, stop, and sizing math comes
from strategy.py.
"""
from __future__ import annotations

import math
from datetime import timedelta

from actions import add_actions, cancel_action, exit_action, order_action, price_round, save_plan
from deskconfig import increments, min_order_ok
from planner import begin, coverage, orphans, reconcile, risk_state
from state import STICKY, parse_ts
from strategy import (initial_stop, ratchet_stop, round_down, should_ratchet, size_position,
                      worst_case_loss)


def ratchets(ctx, snap, actions: list, touched: set, session, planned: dict) -> None:
    """Step 7: raise stops to the 10-day low of closes. Never down."""
    for coin, pos in ctx.st["positions"].items():
        if coin in touched:
            continue
        row = ctx.row(coin, session)
        if row is None or not pos.get("stop_order_id"):
            continue
        n = float(row["n"])
        new = price_round(ctx, coin, ratchet_stop(pos["stop"], row["exit_level"]))
        if not should_ratchet(pos["stop"], new, n, ctx.p.min_ratchet_n):
            continue
        touched.add(coin)
        planned[coin] = new
        q = snap.quotes.get(coin)
        if q is not None and q.bid <= new:
            follow = exit_action(ctx, coin, pos["qty"], q.bid, "trail",
                                 f"bid {q.bid:g} at or below new trail {new:g}", stop_ref=new)
            actions.append(cancel_action(ctx, coin, pos["stop_order_id"],
                                         f"exit {coin}: bid below the new trail level", then=follow))
        else:
            actions.append(cancel_action(ctx, coin, pos["stop_order_id"],
                                         f"raise stop {pos['stop']:g} -> {new:g}",
                                         kind="RATCHET_CANCEL", new_stop=new, old_stop=pos["stop"]))


def entries(ctx, snap, actions: list, plan: dict, session, stale: dict, planned: dict,
            brakes: tuple[bool, bool]) -> None:
    """Step 8: new trades on yesterday's signals, sized so the floor survives the worst case."""
    blocked = [why for why, on in (
        (f"state {ctx.st['state']}", ctx.st["state"] in STICKY), ("daily brake", brakes[0]),
        ("weekly brake", brakes[1]), ("STOP file", ctx.stop_file()),
        (f"mode {ctx.mode}", ctx.mode not in ("LIVE", "DRY"))) if on]
    if blocked:
        plan["notes"].append("no entries: " + ", ".join(blocked))
        return
    held, exited = ctx.st["positions"], ctx.st["exited"]
    recent_exit = {session.isoformat(), ctx.today.isoformat()}
    cands = []
    for coin in ctx.cfg["universe"]:
        row = ctx.row(coin, session)
        if row is None or not bool(row["signal"]):
            continue
        if coin in held or exited.get(coin) in recent_exit:
            plan["skipped"].append({"coin": coin, "reason": "held or exited yesterday"})
        elif coin in stale:
            plan["skipped"].append({"coin": coin, "reason": stale[coin]})
        else:
            mom = row["momentum"]
            cands.append((float(mom) if mom == mom else -1e9, coin, row))
    cands.sort(key=lambda c: c[0], reverse=True)

    gap, floor = ctx.acct["floor_gap_allowance_frac"], ctx.acct["floor_equity"]
    equity, cash, peak = snap.account.equity, snap.account.cash, ctx.st["peak_equity"]
    open_risk = sum(p["qty"] * max(0.0, p["entry_fill"] - planned.get(c, p["stop"]))
                    for c, p in held.items())
    worst = sum(worst_case_loss(p["qty"], snap.quotes[c].bid if c in snap.quotes else p["entry_fill"],
                                planned.get(c, p["stop"]), gap) for c, p in held.items())
    npos = len(held)
    for _, coin, row in cands:
        skip = _quote_checks(ctx, snap, coin, row)
        if skip:
            plan["skipped"].append({"coin": coin, "reason": skip})
            continue
        q, n = snap.quotes[coin], float(row["n"])
        qty_inc, _ = increments(ctx.tools, coin)
        limit = price_round(ctx, coin, q.ask * (1 + ctx.ex["entry_limit_slippage_frac"]))
        stop = initial_stop(limit, n, ctx.p)
        if not math.isfinite(stop):
            plan["skipped"].append({"coin": coin, "reason": "no valid stop"})
            continue
        s = size_position(equity=equity, peak=peak, cash=cash, fill=limit, stop=stop,
                          open_risk=open_risk, open_positions=npos, p=ctx.p,
                          floor=floor, gap_frac=gap, open_worst_case=worst)
        if not s.ok:
            plan["skipped"].append({"coin": coin, "reason": s.reason})
            continue
        qty = round_down(s.qty, qty_inc)
        notional, risk = qty * limit, qty * (limit - stop)
        if notional < ctx.p.min_notional or not min_order_ok(ctx.tools, coin, qty, limit):
            plan["skipped"].append({"coin": coin, "reason": "position below minimum size after rounding"})
            continue
        actions.append(order_action(
            ctx, "BUY", coin, "buy", "limit", qty, limit=limit,
            why=f"close {row['close']:g} above 20-day high {row['entry_level']:g}, N {n:g}",
            n=n, signal_date=session.isoformat(), planned_stop=stop, mid=q.mid))
        cash -= notional
        open_risk += risk
        npos += 1
        worst += worst_case_loss(qty, limit, stop, gap)


def _quote_checks(ctx, snap, coin: str, row) -> str | None:
    q = snap.quotes.get(coin)
    if q is None:
        return "no Robinhood quote"
    spread = (q.ask - q.bid) / q.mid
    if spread > ctx.ex["max_spread_frac"]:
        return f"spread {spread:.2%} above {ctx.ex['max_spread_frac']:.2%}"
    pub = ctx.public(coin)
    if pub is None or pub <= 0:
        return "no public price to check against"
    if abs(q.mid - pub) / pub > ctx.ex["price_check_tolerance_frac"]:
        return f"Robinhood mid {q.mid:g} more than {ctx.ex['price_check_tolerance_frac']:.1%} from public {pub:g}"
    chase = float(row["close"]) + ctx.ex["chase_limit_n"] * float(row["n"])
    if q.ask > chase:
        return f"ask {q.ask:g} above chase limit {chase:g}"
    return None


def refresh(ctx, actions: list, touched: set) -> None:
    """Step 9: good-till-canceled crypto stops expire after 90 days. Replace them early."""
    limit = timedelta(days=ctx.ex["stop_refresh_days"])
    for coin, pos in ctx.st["positions"].items():
        placed = pos.get("stop_placed_at")
        if coin in touched or not placed or not pos.get("stop_order_id"):
            continue
        if ctx.now - parse_ts(placed) >= limit:
            touched.add(coin)
            actions.append(cancel_action(ctx, coin, pos["stop_order_id"], f"refresh {coin} stop before expiry",
                                         kind="RATCHET_CANCEL", new_stop=pos["stop"], old_stop=pos["stop"]))


def make_plan(ctx, job: str) -> dict:
    plan = {"job": job, "mode": ctx.mode, "state": None, "actions": [], "skipped": [], "notes": [],
            "created_at": ctx.now.isoformat(), "progress": {}}
    session = ctx.yesterday if job == "DAILY" else ctx.today
    snap = begin(ctx, plan)
    if snap is not None:
        actions, touched, planned = [], set(), {}
        reconcile(ctx, snap, plan)
        coverage(ctx, snap, actions, touched)
        orphans(ctx, snap, actions)
        brakes = risk_state(ctx, snap, session)
        if job == "DAILY":
            stale = ctx.stale_coins(session)
            for coin, why in stale.items():
                plan["notes"].append(f"{coin}: {why}, no entries")
            ratchets(ctx, snap, actions, touched, session, planned)
            entries(ctx, snap, actions, plan, session, stale, planned, brakes)
            refresh(ctx, actions, touched)
        add_actions(ctx, plan, actions)
    plan["state"] = ctx.display_state(session)
    ctx.save()
    save_plan(ctx, plan)
    ctx.event("plan", job=job, actions=len(plan["actions"]), skipped=len(plan["skipped"]))
    return plan
