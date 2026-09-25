"""
confirmer.py | engine.py confirm --action ID

Reads the order status captured after the action was issued and prints exactly one of:
NEXT, WAIT <seconds>, NEW ACTION <json>, RETRY, STOP.
"""
from __future__ import annotations

import json
from datetime import timedelta

from actions import CANCEL_KINDS, add_actions, cancel_action, exit_action, reissue, save_plan, stop_action
from broker import TapeError
from state import iso, parse_ts, read_json
from strategy import initial_stop

UNKNOWN_LIMIT = 3
CANCEL_WAIT_LIMIT = 6
LAST_EXIT_DISCOUNT = 0.05


def load_plan(ctx) -> dict | None:
    return read_json(ctx.paths.plans / "latest.json")


def _resolve(ctx, action: dict):
    after = parse_ts(action["issued_at"]) - timedelta(seconds=5)
    if action["kind"] in CANCEL_KINDS:
        return ctx.broker.order(action["order"]["order_id"], after)
    placed = ctx.broker.placement(action["order"]["ref_id"], after)
    if placed is None:
        return None
    return ctx.broker.order(placed.id, after) or placed


def confirm(ctx, action_id: str) -> str:
    plan = load_plan(ctx)
    action = next((a for a in (plan or {}).get("actions", []) if a["id"] == action_id), None)
    if action is None:
        ctx.incident(f"confirm: no action {action_id} in the latest plan")
        return "STOP"
    if action["kind"] == "PREVIEW":
        return "NEXT"
    prog = plan["progress"].setdefault(action_id, {"unknown": 0, "retries": 0, "waits": 0})
    try:
        order = _resolve(ctx, action)
    except TapeError as exc:
        ctx.incident(f"confirm {action_id}: tape unreadable ({exc})")
        order = None
    if order is None:
        prog["unknown"] += 1
        line = "WAIT 10"
        if prog["unknown"] >= UNKNOWN_LIMIT:
            ctx.incident(f"order status unknown three times for {action['kind']} {action['coin']}",
                         force_alert=True)
            line = "STOP"
    else:
        prog["unknown"] = 0
        ctx.remember_order(order.id, action["coin"], action["kind"])
        handler = {"BUY": _buy, "PROTECT": _stop, "RATCHET_PLACE": _stop, "EXIT": _exit}.get(
            action["kind"], _cancel)
        line = handler(ctx, plan, action, order, prog)
    ctx.save()
    save_plan(ctx, plan)
    ctx.event("confirm", action=action_id, kind=action["kind"], result=line.split(" ", 1)[0])
    return line


def _new(ctx, plan: dict, action: dict) -> str:
    added = add_actions(ctx, plan, [action])[0]
    slim = {k: v for k, v in added.items() if k in ("id", "kind", "coin", "tool", "args",
                                                    "status_tool", "status_args", "why")}
    return "NEW ACTION " + json.dumps(slim, separators=(",", ":"))


def _age(ctx, action: dict) -> float:
    return (ctx.now - parse_ts(action["issued_at"])).total_seconds()


def _open_position(ctx, action: dict, order, qty: float) -> None:
    coin, n = action["coin"], action["n"]
    fill = order.avg_fill_price
    stop = initial_stop(fill, n, ctx.p)
    pos = ctx.st["positions"].get(coin)
    if pos is None:
        ctx.st["positions"][coin] = pos = {
            "entry_fill": fill, "entry_ts": iso(parse_ts(order.updated_at)) if order.updated_at else iso(ctx.now),
            "n_at_entry": n,
            "initial_stop": stop, "stop": stop, "stop_order_id": None, "entry_order_id": order.id,
            "entry_mid": action.get("mid", fill), "signal_date": action.get("signal_date"),
            "trade_id": f"{coin}-{action.get('signal_date')}"}
        ctx.event("entry", coin=coin, fill=fill, planned=action["order"]["limit_price"], qty=qty)
    pos["qty"] = qty
    ctx.say(f"BUY {coin} filled {qty:g} at {fill:g}. Stop {stop:g}.")


def _ts(ctx, o) -> str:
    return iso(parse_ts(o.updated_at)) if o.updated_at else iso(ctx.now)


def _protect(ctx, coin: str, why: str, **extra) -> dict:
    pos = ctx.st["positions"][coin]
    return stop_action(ctx, "PROTECT", coin, pos["qty"], pos["stop"], why, **extra)


def _buy(ctx, plan, action, o, prog) -> str:
    timeout = ctx.ex["entry_fill_timeout_sec"]
    if o.status == "filled" or (o.filled_qty > 0 and not o.is_live):
        _open_position(ctx, action, o, o.filled_qty)
        return _new(ctx, plan, _protect(ctx, action["coin"], "protect the fill"))
    if o.is_live and _age(ctx, action) < timeout:
        return "WAIT 20"
    if o.is_live:
        if o.filled_qty > 0:
            _open_position(ctx, action, o, o.filled_qty)
        return _new(ctx, plan, cancel_action(ctx, action["coin"], o.id, "buy not filled in time",
                                             buy_order=True, n=action["n"], mid=action.get("mid"),
                                             signal_date=action.get("signal_date"),
                                             planned_limit=action["order"]["limit_price"]))
    ctx.incident(f"BUY {action['coin']} rejected: {o.reject_reason or o.status}")
    return "NEXT"


def _stop(ctx, plan, action, o, prog) -> str:
    coin, want = action["coin"], action["order"]
    if o.status == "filled":
        if coin in ctx.st["positions"]:
            ctx.close_trade(coin, o.filled_qty, o.avg_fill_price, _ts(ctx, o),
                            "trail" if action["kind"] == "RATCHET_PLACE" else "stop", want["stop_price"])
        return "NEXT"
    if o.is_live and abs(o.qty - want["qty"]) <= ctx.ex["dust_qty_frac"] * want["qty"] \
            and o.stop_price is not None and abs(o.stop_price - want["stop_price"]) <= 1e-9 + 0.0005 * want["stop_price"]:
        pos = ctx.st["positions"].get(coin)
        if pos is not None:
            pos.update(stop=want["stop_price"], stop_order_id=o.id, stop_placed_at=iso(ctx.now))
        ctx.event("stop_live", coin=coin, seconds=_age(ctx, action))
        return "NEXT"
    if o.is_live:
        ctx.incident(f"{action['kind']} {coin}: live stop does not match the plan")
        return _new(ctx, plan, cancel_action(ctx, coin, o.id, "stop does not match the plan",
                                             then=_protect(ctx, coin, "replace the wrong stop")))
    ctx.incident(f"{action['kind']} {coin} rejected: {o.reject_reason or o.status}")
    prog["retries"] += 1                       # counts rejections: the second one ends the retries
    if action["kind"] == "RATCHET_PLACE":
        if prog["retries"] < 2:
            reissue(ctx, action)
            return "RETRY"
        pos = ctx.st["positions"][coin]
        pos["stop"] = action["old_stop"]
        return _new(ctx, plan, _protect(ctx, coin, "raised stop failed: put the old stop back"))
    if prog["retries"] < ctx.ex["stop_retry_limit"]:
        reissue(ctx, action)
        return "RETRY"
    ctx.pause(f"stop would not place for {coin}")
    return _new(ctx, plan, _emergency_exit(ctx, coin, "stop would not place"))


def _emergency_exit(ctx, coin: str, why: str) -> dict:
    pos = ctx.st["positions"][coin]
    bid = _fresh_bid(ctx, coin)
    price = bid if bid is not None else pos["stop"] * (1 - LAST_EXIT_DISCOUNT)
    return exit_action(ctx, coin, pos["qty"], price, "emergency", why, stop_ref=None)


def _fresh_bid(ctx, coin: str):
    try:
        q = ctx.broker.quotes(ctx.ex["tape_max_age_sec"]).get(coin)
        return q.bid if q else None
    except Exception:
        return None


def _exit(ctx, plan, action, o, prog) -> str:
    coin = action["coin"]
    if o.status == "filled":
        if coin in ctx.st["positions"]:
            ctx.close_trade(coin, o.filled_qty, o.avg_fill_price, _ts(ctx, o),
                            action["exit_reason"], action.get("stop_ref"))
        return "NEXT"
    if o.is_live and _age(ctx, action) < ctx.ex["entry_fill_timeout_sec"]:
        return "WAIT 20"
    attempt = action.get("attempt", 0)
    final = ctx.ex["exit_reprice_attempts"] + 1
    if attempt >= final:
        ctx.incident(f"EXIT {coin} did not fill after the last attempt", force_alert=True)
        return "STOP"
    if o.filled_qty > 0 and coin in ctx.st["positions"]:
        ctx.close_trade(coin, o.filled_qty, o.avg_fill_price, _ts(ctx, o),
                        action["exit_reason"], action.get("stop_ref"))
    if coin not in ctx.st["positions"]:
        return "NEXT"
    qty = ctx.st["positions"][coin]["qty"]
    if attempt + 1 < final:
        price = action["order"]["limit_price"] * (1 - ctx.ex["exit_reprice_step_frac"])
    else:
        bid = _fresh_bid(ctx, coin) or action["order"]["limit_price"]
        price = bid * (1 - LAST_EXIT_DISCOUNT)
        ctx.incident(f"EXIT {coin} last attempt at bid minus 5 percent", force_alert=True)
    follow = exit_action(ctx, coin, qty, price, action["exit_reason"], f"reprice exit {attempt + 1}",
                         stop_ref=action.get("stop_ref"), attempt=attempt + 1)
    if o.is_live:
        return _new(ctx, plan, cancel_action(ctx, coin, o.id, "exit not filled in time", then=follow))
    return _new(ctx, plan, follow)


def _cancel(ctx, plan, action, o, prog) -> str:
    coin = action["coin"]
    if o.is_live:
        prog["waits"] += 1
        if prog["waits"] > CANCEL_WAIT_LIMIT:
            ctx.incident(f"cancel for {coin} never confirmed", force_alert=True)
            return "STOP"
        return "WAIT 5"
    if o.status == "rejected":
        ctx.incident(f"cancel for {coin} rejected: {o.reject_reason or 'no reason'}")
        return "NEXT"
    if o.side == "sell" and o.filled_qty > 0:          # the order filled before the cancel landed
        if coin in ctx.st["positions"]:
            reason = "trail" if action["kind"] == "RATCHET_CANCEL" else "stop"
            ctx.close_trade(coin, o.filled_qty, o.avg_fill_price, _ts(ctx, o),
                            reason, o.stop_price)
        if coin not in ctx.st["positions"]:
            return "NEXT"
    if action.get("buy_order"):
        if o.filled_qty > 0 and action.get("n"):
            _open_position(ctx, {**action, "order": {"limit_price": action.get("planned_limit")}}, o,
                           o.filled_qty)
        if coin in ctx.st["positions"] and o.filled_qty > 0:
            return _new(ctx, plan, _protect(ctx, coin, "protect the partial fill"))
        return "NEXT"
    if action["kind"] == "RATCHET_CANCEL" and coin in ctx.st["positions"]:
        pos = ctx.st["positions"][coin]
        pos["stop_order_id"] = None
        return _new(ctx, plan, stop_action(ctx, "RATCHET_PLACE", coin, pos["qty"], action["new_stop"],
                                           f"raised stop {action['new_stop']:g}", old_stop=action["old_stop"]))
    then = action.get("then")
    if then and coin in ctx.st["positions"]:
        if then["kind"] in ("PROTECT", "EXIT"):
            then = {**then, "order": {**then["order"], "qty": ctx.st["positions"][coin]["qty"]}}
            then = _rebuild(ctx, then)
        return _new(ctx, plan, then)
    return "NEXT"


def _rebuild(ctx, a: dict) -> dict:
    """Rebuild a queued follow-up so its quantity matches what is held now, with a fresh ref_id."""
    o = a["order"]
    if a["kind"] == "PROTECT":
        return stop_action(ctx, "PROTECT", a["coin"], o["qty"], o["stop_price"], a["why"])
    return exit_action(ctx, a["coin"], o["qty"], o["limit_price"], a["exit_reason"], a["why"],
                       stop_ref=a.get("stop_ref"), attempt=a.get("attempt", 0))
