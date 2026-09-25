"""
planner.py | engine.py plan, steps 1 to 5 (both jobs): tape, hook and account checks,
reconcile positions, stop coverage, orphans, risk state. Steps 6 to 9 live in entries.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from actions import cancel_action, exit_action, stop_action
from broker import StaleTape, TapeError
from deskconfig import READ_ROLES, increments, min_order_ok, tools_for
from state import iso, load_approvals, parse_ts, read_jsonl
from strategy import initial_stop

PRICE_TOL = 0.0005
HOOK_WINDOW_SEC = 600
LOOKBACK_DAYS = 7


@dataclass
class Snapshot:
    accounts: list
    account: object
    held: dict
    orders: list
    open: list
    quotes: dict
    meta: list


def read_snapshot(ctx) -> Snapshot:
    b, age = ctx.broker, ctx.ex["tape_max_age_sec"]
    orders = b.recent_orders(age)
    return Snapshot(accounts=b.accounts(age), account=b.account(age), held=b.positions(age),
                    orders=orders, open=[o for o in orders if o.is_live], quotes=b.quotes(age),
                    meta=b.read_meta(READ_ROLES, age))


def hook_check(ctx, snap: Snapshot) -> bool:
    """Every read on the tape must have a guard decision logged just before it."""
    guard = [e for e in read_jsonl(ctx.paths.events)
             if e.get("type") == "guard" and e.get("decision") == "allow" and not e.get("selftest")]
    for tool, ts in snap.meta:
        t = parse_ts(ts)
        if not any(e.get("tool") == tool and
                   -5 <= (t - parse_ts(e["ts"])).total_seconds() <= HOOK_WINDOW_SEC for e in guard):
            ctx.pause(f"guard hook not running: no guard decision for {tool.rsplit('__', 1)[-1]}")
            return False
    return True


def account_check(ctx, snap: Snapshot) -> bool:
    agentic = [a for a in snap.accounts if a.agentic_allowed]
    if len(agentic) != 1:
        ctx.pause(f"expected exactly one agentic account, saw {len(agentic)}")
        return False
    number = agentic[0].rhs_account_number
    if ctx.st["account"] is None:
        ctx.st["account"] = number
        ctx.event("account_recorded")
        ctx.say("Recorded the agentic account.")
    elif ctx.st["account"] != number:
        ctx.pause("agentic account changed")
        return False
    return True


def _order_ts(o) -> str:
    return o.updated_at or o.created_at


def _when(o):
    return parse_ts(_order_ts(o))


def _n_for(ctx, coin: str):
    for back in (1, 2):
        row = ctx.row(coin, ctx.today - timedelta(days=back))
        if row is not None and row["n"] == row["n"]:
            return float(row["n"])
    return None


def reconcile(ctx, snap: Snapshot, plan: dict) -> None:
    """Step 2: Robinhood is the source of truth for what is held."""
    st, dust = ctx.st, ctx.ex["dust_qty_frac"]
    for coin, pos in list(st["positions"].items()):
        rh = snap.held.get(coin, 0.0)
        if rh > 0:
            if abs(rh - pos["qty"]) <= dust * pos["qty"]:
                continue
            if rh > pos["qty"]:
                ctx.pause(f"{coin} position larger than the desk recorded")
            else:
                plan["notes"].append(f"{coin}: Robinhood holds {rh:g}, desk had {pos['qty']:g}. Resized.")
            pos["qty"] = rh
            continue
        fills = [o for o in snap.orders if o.coin == coin and o.side == "sell" and o.filled_qty > 0
                 and o.status in ("filled", "partially_filled") and _when(o) >= parse_ts(pos["entry_ts"])]
        fill = next((o for o in fills if o.id == pos.get("stop_order_id")), None) or \
            (max(fills, key=_when) if fills else None)
        if fill is None:
            st.setdefault("vanished", {})[coin] = pos
            del st["positions"][coin]
            ctx.pause(f"position vanished: {coin}")
            continue
        reason = "stop" if pos["stop"] <= pos["initial_stop"] else "trail"
        stop_ref = fill.stop_price if fill.is_stop else pos["stop"]
        ctx.close_trade(coin, pos["qty"], fill.avg_fill_price, iso(_when(fill)), reason, stop_ref)
        ctx.remember_order(fill.id, coin, "stop_fill")

    approvals = load_approvals(ctx.paths)
    for coin, qty in snap.held.items():
        if coin in st["positions"]:
            continue
        q = snap.quotes.get(coin)
        if coin in ctx.cfg["universe"] and q and not min_order_ok(ctx.tools, coin, qty, q.bid):
            plan["notes"].append(f"{coin}: dust {qty:g} below the minimum order. Ignored.")
            continue
        adopted = _adopt(ctx, snap, coin, qty, approvals)
        if adopted:
            continue
        ctx.pause(f"unknown position: {coin}")
        n = _n_for(ctx, coin)
        if q is None or n is None:
            plan["notes"].append(f"{coin}: cannot compute a stop (no quote or no N). Lando decides.")
            continue
        stop = initial_stop(q.bid, n, ctx.p)
        st["positions"][coin] = {"qty": qty, "entry_fill": q.bid, "entry_ts": iso(ctx.now),
                                 "n_at_entry": n, "initial_stop": stop, "stop": stop,
                                 "stop_order_id": None, "entry_order_id": None, "unknown": True,
                                 "entry_mid": q.mid, "trade_id": f"{coin}-unknown-{ctx.today}"}


def _adopt(ctx, snap: Snapshot, coin: str, qty: float, approvals: list) -> bool:
    since = ctx.now - timedelta(hours=24)
    used = [a for a in approvals if a.get("kind") == "BUY" and a.get("coin") == coin and a.get("used")
            and a.get("used_at") and parse_ts(a["used_at"]) >= since]
    buys = [o for o in snap.orders if o.coin == coin and o.side == "buy" and o.filled_qty > 0
            and any(_when(o) >= parse_ts(a["issued_at"]) for a in used)]
    n = _n_for(ctx, coin)
    if not buys or n is None:
        return False
    o = max(buys, key=_when)
    stop = initial_stop(o.avg_fill_price, n, ctx.p)
    ctx.st["positions"][coin] = {"qty": qty, "entry_fill": o.avg_fill_price, "entry_ts": iso(_when(o)),
                                 "n_at_entry": n, "initial_stop": stop, "stop": stop,
                                 "stop_order_id": None, "entry_order_id": o.id, "entry_mid": o.avg_fill_price,
                                 "trade_id": f"{coin}-{_order_ts(o)[:10]}"}
    ctx.remember_order(o.id, coin, "BUY")
    ctx.say(f"Adopted {coin} from approved buy {o.avg_fill_price:g} x {qty:g}.")
    return True


def _stop_matches(ctx, coin, o, pos) -> bool:
    tol = max(PRICE_TOL * pos["stop"], increments(ctx.tools, coin)[1])
    return (abs(o.qty - pos["qty"]) <= ctx.ex["dust_qty_frac"] * pos["qty"]
            and o.stop_price is not None and abs(o.stop_price - pos["stop"]) <= tol)


def coverage(ctx, snap: Snapshot, actions: list, touched: set) -> None:
    """Step 3: every position needs exactly one live stop, full quantity, at the state stop."""
    for coin, pos in ctx.st["positions"].items():
        stops = [o for o in snap.open if o.coin == coin and o.side == "sell" and o.is_stop]
        q = snap.quotes.get(coin)
        good = [o for o in stops if _stop_matches(ctx, coin, o, pos)]
        if good:
            keep = next((o for o in good if o.id == pos.get("stop_order_id")), good[0])
            pos["stop_order_id"] = keep.id
            pos.setdefault("stop_placed_at", keep.created_at)
            for o in stops:
                if o.id != keep.id:
                    actions.append(cancel_action(ctx, coin, o.id, f"extra stop for {coin}"))
                    touched.add(coin)
            continue
        touched.add(coin)
        if q is not None and q.bid <= pos["stop"]:
            follow = exit_action(ctx, coin, pos["qty"], q.bid, "emergency",
                                 f"bid {q.bid:g} at or below stop {pos['stop']:g}", stop_ref=pos["stop"])
        else:
            follow = stop_action(ctx, "PROTECT", coin, pos["qty"], pos["stop"],
                                 f"position without a matching live stop, stop {pos['stop']:g}")
        if not stops:
            actions.append(follow)
            continue
        for i, o in enumerate(stops):              # cancel the wrong ones, then place the right one
            last = i == len(stops) - 1
            actions.append(cancel_action(ctx, coin, o.id, f"wrong stop for {coin}",
                                         then=follow if last else None))


def orphans(ctx, snap: Snapshot, actions: list) -> None:
    """Step 4: cancel the desk's own stale orders; pause on anything the desk did not place."""
    since = ctx.now - timedelta(days=LOOKBACK_DAYS + 1)
    approvals = load_approvals(ctx.paths)
    refs = {a.get("ref_id") for a in approvals if a.get("ref_id")}
    explained = set(ctx.st.get("known_orders", {}))
    for pos in ctx.st["positions"].values():
        explained.update(x for x in (pos.get("stop_order_id"), pos.get("entry_order_id")) if x)
    explained.update(a.get("order_id") for a in approvals if a.get("order_id"))
    try:
        explained.update(o.id for ref, o, _ in ctx.broker.placements(since) if ref in refs)
    except TapeError as exc:
        ctx.say(f"NOTE: placements unreadable ({exc})")
    cutoff = ctx.now - timedelta(days=LOOKBACK_DAYS)
    timeout = ctx.ex["entry_fill_timeout_sec"]
    for o in snap.orders:
        recent_fill = o.status in ("filled", "partially_filled") and _when(o) >= cutoff
        if o.id not in explained:
            if o.is_live or recent_fill:
                ctx.pause(f"order not placed by desk: {o.coin} {o.side} {o.type}")
            continue
        if not o.is_live:
            continue
        age = (ctx.now - parse_ts(o.created_at)).total_seconds()
        if o.side == "buy" and age > timeout:
            actions.append(cancel_action(ctx, o.coin, o.id, "approved buy still open past the timeout",
                                         buy_order=True))
        elif o.is_stop and o.coin not in ctx.st["positions"]:
            actions.append(cancel_action(ctx, o.coin, o.id, "stop for a coin the desk no longer holds"))


def risk_state(ctx, snap: Snapshot, session) -> tuple[bool, bool]:
    """Step 5: floor and brakes. Returns (day brake, week brake) for the session day."""
    equity = snap.account.equity
    ctx.st["last_equity"] = equity
    if equity <= ctx.acct["floor_equity"]:
        ctx.halt(f"equity {equity:.2f} at or below floor {ctx.acct['floor_equity']}")
    return ctx.brakes(session, equity)


def begin(ctx, plan: dict) -> Snapshot | None:
    """Step 1. None means: no actions this run."""
    try:
        snap = read_snapshot(ctx)
    except StaleTape as exc:
        ctx.say(f"CALL READ TOOLS FIRST ({exc})")
        plan["notes"].append("CALL READ TOOLS FIRST")
        return None
    except TapeError as exc:
        ctx.incident(f"tape unreadable: {exc}")
        plan["notes"].append("tape unreadable")
        return None
    if not tools_for(ctx.tools, "place", "place"):
        return None
    if not hook_check(ctx, snap) or not account_check(ctx, snap):
        return None
    ctx.note_start(snap.account.equity)
    return snap
