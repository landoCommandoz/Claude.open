"""Simulated Robinhood for the parity test: fills at the close plus cost, stops on the daily low.

Every order goes through the real guard first, exactly as the PreToolUse hook would run it.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta, timezone

import pandas as pd

import guard
from broker import Account, AgenticAccount, Order, Quote
from deskconfig import READ_ROLES, args_canonical, tools_for
from helpers import ACCT, ACCT_MAIN, TOOLS, log_guard_reads
from state import iso
from strategy import stop_fill

HISTORY_DAYS = 8


def _f(x):
    return None if x is None else float(x)          # the wire carries decimal strings


class SimBroker:
    def __init__(self, frames: dict, cost: float, cash: float):
        self.frames, self.cost, self.cash = frames, cost, cash
        self.hold: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.day = None
        self.now = None

    # ----- the market -----
    def close(self, coin):
        return float(self.frames[coin].at[pd.Timestamp(self.day), "close"])

    def set_session(self, day, now):
        self.day, self.now = day, now

    def intraday_stops(self, day):
        """Resting stops trigger on the modeled bid, with the same strategy.stop_fill as backtest.py."""
        for o in self.orders.values():
            if o["status"] != "open" or o["type"] != "stop_market":
                continue
            row = self.frames[o["coin"]].loc[pd.Timestamp(day)]
            fill = stop_fill(float(row["open"]), float(row["low"]), o["stop_price"], self.cost)
            if fill is not None:
                self._fill(o, fill, datetime.combine(day, time(12), timezone.utc))

    def _fill(self, o, price, when):
        o.update(status="filled", filled_qty=o["qty"], avg_fill_price=price, updated_at=iso(when))
        sign = 1 if o["side"] == "buy" else -1
        self.cash -= sign * o["qty"] * price
        self.hold[o["coin"]] = self.hold.get(o["coin"], 0.0) + sign * o["qty"]
        if self.hold[o["coin"]] <= 1e-12:
            del self.hold[o["coin"]]

    # ----- what the agent does with an action -----
    def execute(self, root, action) -> None:
        allow, reason = guard.decide({"tool_name": action["tool"], "tool_input": action["args"]},
                                     root, self.now)
        assert allow, f"guard denied a planned {action['kind']}: {reason}"
        a = args_canonical(TOOLS, action["tool"], action["args"])
        if TOOLS["tools"][action["tool"]]["class"] == "cancel":
            o = self.orders[a["order_id"]]
            if o["status"] == "open":
                o.update(status="canceled", updated_at=iso(self.now))
            return
        if a["ref_id"] in self.refs:                      # idempotent ref_id: no duplicate order
            return
        oid = f"o{len(self.orders) + 1}"
        self.refs[a["ref_id"]] = oid
        o = {"id": oid, "coin": a["coin"], "side": a["side"], "type": a["order_type"], "qty": float(a["qty"]),
             "filled_qty": 0.0, "avg_fill_price": None, "limit_price": _f(a.get("limit_price")),
             "stop_price": _f(a.get("stop_price")), "status": "open", "created_at": iso(self.now),
             "updated_at": None, "reject_reason": None}
        self.orders[oid] = o
        if o["side"] == "sell":
            reserved = sum(x["qty"] for x in self.orders.values() if x["coin"] == o["coin"]
                           and x["side"] == "sell" and x["status"] == "open" and x["id"] != oid)
            if reserved + o["qty"] > self.hold.get(o["coin"], 0.0) * (1 + 1e-9):
                o.update(status="rejected", reject_reason="insufficient holdings")
                return
        c = self.close(o["coin"])
        if o["type"] == "limit" and o["side"] == "buy" and o["limit_price"] >= c * (1 + self.cost):
            self._fill(o, c * (1 + self.cost), self.now)
        elif o["type"] == "limit" and o["side"] == "sell" and o["limit_price"] <= c * (1 - self.cost):
            self._fill(o, c * (1 - self.cost), self.now)

    # ----- broker.Broker's interface -----
    def _order(self, o) -> Order:
        return Order(**o)

    def accounts(self, max_age):
        return [AgenticAccount(ACCT, True, ACCT_MAIN)]

    def account(self, max_age):
        eq = self.cash + sum(q * self.close(c) for c, q in self.hold.items())
        return Account(eq, self.cash, self.cash, iso(self.now))

    def positions(self, max_age):
        return dict(self.hold)

    def recent_orders(self, max_age):
        cutoff = iso(self.now - timedelta(days=HISTORY_DAYS))
        return [self._order(o) for o in self.orders.values()
                if o["status"] == "open" or (o["updated_at"] or o["created_at"]) >= cutoff]

    def open_orders(self, max_age):
        return [o for o in self.recent_orders(max_age) if o.is_live]

    def quotes(self, max_age):
        return {c: Quote(self.close(c) * (1 - self.cost), self.close(c) * (1 + self.cost), iso(self.now))
                for c in self.frames}

    def read_meta(self, roles, max_age):
        return [(t, iso(self.now)) for r in roles for t in tools_for(TOOLS, r, "read")]

    def order(self, order_id, after):
        o = self.orders.get(order_id)
        return self._order(o) if o else None

    def placement(self, ref_id, since):
        oid = self.refs.get(ref_id)
        return self._order(self.orders[oid]) if oid else None

    def placements(self, since):
        return [(r, self._order(self.orders[o]), iso(self.now)) for r, o in self.refs.items()]


def drive(root, sim, make_ctx, action, depth=0) -> bool:
    """Execute one action the way prompts/daily.md tells the agent to. False means STOP."""
    from confirmer import confirm
    assert depth < 8, "runaway NEW ACTION chain"
    sim.execute(root, action)
    for _ in range(6):
        line = confirm(make_ctx(), action["id"])
        if line == "NEXT":
            return True
        if line == "STOP":
            return False
        if line == "RETRY":
            sim.execute(root, action)
        elif line.startswith("NEW ACTION "):
            return drive(root, sim, make_ctx, json.loads(line[len("NEW ACTION "):]), depth + 1)
    raise AssertionError(f"confirm loop did not settle for {action['kind']} {action['coin']}")


def replay(root, frames: dict, days: list, cfg: dict) -> SimBroker:
    from closer import close
    from desk import Ctx
    from entries import make_plan
    sim = SimBroker(frames, cfg["execution"]["cost_per_side_assumed"], cfg["account"]["starting_capital"])
    for day in days:
        sim.set_session(day, None)
        sim.intraday_stops(day)
        now = datetime.combine(day + timedelta(days=1), time(0, 7), timezone.utc)
        sim.set_session(day, now)
        log_guard_reads(root, now)
        cut = {c: f.loc[:pd.Timestamp(day)] for c, f in frames.items()}

        def make_ctx():
            return Ctx(root, now, broker=sim, frames=cut, public_price=lambda c: sim.close(c),
                       alert=lambda t, m: None)
        plan = make_plan(make_ctx(), "DAILY")
        for action in plan["actions"]:
            if not drive(root, sim, make_ctx, action):
                break
        close(make_ctx(), "DAILY")
    return sim
