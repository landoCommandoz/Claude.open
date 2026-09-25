"""
actions.py | Builds plan actions and the approvals that let the guard pass them.

Every order action carries the agentic account's rhs_account_number and a fresh UUID ref_id,
and every stop carries time_in_force "gtc" (Robinhood defaults stops to good-for-day).
Protective actions always come first: PROTECT and EXIT, then cancels and ratchets, then BUYs.
In DRY mode order actions become PREVIEW actions on the preview tool and no approvals exist.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from deskconfig import args_map, args_raw, increments, tool_for, tools_for
from state import approvals_locked, atomic_write_json, iso
from strategy import round_down

PRIORITY = {"PROTECT": 0, "EXIT": 0, "CANCEL": 1, "RATCHET_CANCEL": 1, "RATCHET_PLACE": 1, "BUY": 2}
CANCEL_KINDS = ("CANCEL", "RATCHET_CANCEL", "TEST_CANCEL")
STOP_LIMIT_OFFSET = 0.03       # BUILD.md Phase 1: if only stop-limit works, limit = stop - 3%
KEEP_APPROVALS_DAYS = 14


def price_round(ctx, coin: str, price: float) -> float:
    return round_down(price, increments(ctx.tools, coin)[1])


def _status(ctx) -> tuple[str, dict]:
    tool = tools_for(ctx.tools, "orders", "read")[0]
    return tool, args_raw(ctx.tools, tool, {"account": ctx.st["account"]})


def order_action(ctx, kind: str, coin: str, side: str, order_type: str, qty: float, *,
                 limit=None, stop=None, why: str = "", ref_id: str | None = None, **extra) -> dict:
    tool = tool_for(ctx.tools, "place")
    order = {"coin": coin, "side": side, "order_type": order_type, "qty": qty,
             "limit_price": limit, "stop_price": stop,
             "time_in_force": "gtc" if order_type in ("stop_market", "stop_limit") else None,
             "ref_id": ref_id or str(uuid.uuid4())}
    status_tool, status_args = _status(ctx)
    return {"id": None, "kind": kind, "coin": coin, "tool": tool,
            "args": args_raw(ctx.tools, tool, {"account": ctx.st["account"], **order}),
            "status_tool": status_tool, "status_args": status_args, "why": why, "order": order, **extra}


def stop_action(ctx, kind: str, coin: str, qty: float, stop: float, why: str, **extra) -> dict:
    stop = price_round(ctx, coin, stop)
    otype = ctx.tools["stop_order_type"]
    limit = price_round(ctx, coin, stop * (1 - STOP_LIMIT_OFFSET)) if otype == "stop_limit" else None
    return order_action(ctx, kind, coin, "sell", otype, qty, limit=limit, stop=stop, why=why, **extra)


def exit_action(ctx, coin: str, qty: float, price: float, reason: str, why: str, *,
                stop_ref=None, attempt: int = 0) -> dict:
    return order_action(ctx, "EXIT", coin, "sell", "limit", qty, limit=price_round(ctx, coin, price),
                        why=why, exit_reason=reason, stop_ref=stop_ref, attempt=attempt)


def cancel_action(ctx, coin: str, order_id: str, why: str, *, kind: str = "CANCEL",
                  then: dict | None = None, **extra) -> dict:
    tool = tool_for(ctx.tools, "cancel")
    ref = str(uuid.uuid4()) if "ref_id" in args_map(ctx.tools, tool) else None
    order = {"order_id": order_id, "ref_id": ref}
    status_tool, status_args = _status(ctx)
    return {"id": None, "kind": kind, "coin": coin, "tool": tool,
            "args": args_raw(ctx.tools, tool, {"account": ctx.st["account"], **order}),
            "status_tool": status_tool, "status_args": status_args, "why": why, "order": order,
            "then": then, **extra}


def approval_for(ctx, action: dict, mode: str) -> dict:
    o = action["order"]
    qty = o.get("qty")
    price = o.get("limit_price") or o.get("stop_price")
    return {"id": str(uuid.uuid4()), "action_id": action["id"], "kind": action["kind"],
            "coin": action["coin"], "side": o.get("side"), "order_type": o.get("order_type"),
            "qty": qty, "notional": qty * price if qty and price else None,
            "limit_price": o.get("limit_price"), "stop_price": o.get("stop_price"),
            "order_id": o.get("order_id"), "ref_id": o.get("ref_id"),
            "issued_at": iso(ctx.now), "expires_at": iso(ctx.now + timedelta(seconds=ctx.ex["approval_ttl_sec"])),
            "used": False, "used_at": None, "mode": mode}


def write_approvals(ctx, new: list[dict]) -> None:
    cutoff = iso(ctx.now - timedelta(days=KEEP_APPROVALS_DAYS))
    with approvals_locked(ctx.paths) as items:
        items[:] = [a for a in items if a.get("issued_at", "") >= cutoff] + new


def to_preview(ctx, action: dict) -> dict | None:
    if action["kind"] in CANCEL_KINDS:
        return None
    tool = tools_for(ctx.tools, "preview")[0]
    return {**action, "kind": "PREVIEW", "of": action["kind"], "tool": tool,
            "args": args_raw(ctx.tools, tool, {"account": ctx.st["account"], **action["order"]})}


def next_id(plan: dict) -> str:
    return f"A{len(plan['actions']) + 1}"


def add_actions(ctx, plan: dict, actions: list[dict]) -> list[dict]:
    """Number, order, convert for DRY, and write approvals. Returns the actions as printed."""
    out, approvals = [], []
    for action in sorted(actions, key=lambda a: PRIORITY.get(a["kind"], 3)):
        if ctx.mode == "DRY":
            preview = to_preview(ctx, action)
            if preview is None:
                plan["notes"].append(f"DRY: would {action['kind']} {action['coin']}: {action['why']}")
                continue
            action = preview
        action["id"] = next_id(plan)
        action["issued_at"] = iso(ctx.now)
        plan["actions"].append(action)
        out.append(action)
        if action["kind"] != "PREVIEW":
            approvals.append(approval_for(ctx, action, ctx.mode))
    if approvals:
        write_approvals(ctx, approvals)
    return out


def reissue(ctx, action: dict) -> None:
    """RETRY: a fresh approval for the same action with the SAME ref_id."""
    action["issued_at"] = iso(ctx.now)
    write_approvals(ctx, [approval_for(ctx, action, ctx.mode)])


def save_plan(ctx, plan: dict) -> None:
    stamp = ctx.now.strftime("%Y%m%dT%H%M%SZ")
    plan.setdefault("file", f"{stamp}_{plan['job']}.json")
    atomic_write_json(ctx.paths.plans / plan["file"], plan)
    atomic_write_json(ctx.paths.plans / "latest.json", plan)


def printable(plan: dict) -> str:
    keep = ("job", "mode", "state", "actions", "skipped", "notes")
    slim = {k: plan[k] for k in keep}
    slim["actions"] = [{k: v for k, v in a.items() if k in ("id", "kind", "coin", "tool", "args",
                                                             "status_tool", "status_args", "why")}
                       for a in plan["actions"]]
    return json.dumps(slim, separators=(",", ":"))
