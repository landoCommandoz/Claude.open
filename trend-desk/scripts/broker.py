"""
broker.py | The only code that reads Robinhood responses.

Reads the tape that capture.py records and returns clean objects. Tape older than
max_age raises StaleTape. A missing or unreadable field raises TapeError. No defaults.

PHASE 1 FILLS THE RAW MAPPINGS. Until real responses are captured, every entry in
PARSERS raises TapeError, so nothing downstream can act on a guessed field name.
Each parser takes one tool_response and returns the normalized value for its role:
  accounts  -> list[AgenticAccount]     account -> Account      positions -> {coin: qty}
  orders    -> list[Order]              quotes  -> {coin: Quote}
  place     -> Order (the placed order) cancel  -> Order or None
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from deskconfig import args_canonical, role_of, short_name, tools_for
from state import Paths, parse_ts

SIDES = ("buy", "sell")
TYPES = ("limit", "market", "stop_market", "stop_limit")
STATUSES = ("open", "filled", "partially_filled", "canceled", "rejected", "pending")
LIVE = ("open", "pending", "partially_filled")


class StaleTape(Exception):
    pass


class TapeError(Exception):
    pass


@dataclass(frozen=True)
class Order:
    id: str
    coin: str
    side: str
    type: str
    qty: float
    filled_qty: float
    avg_fill_price: float | None
    limit_price: float | None
    stop_price: float | None
    status: str
    created_at: str
    updated_at: str | None
    reject_reason: str | None

    @property
    def is_stop(self) -> bool:
        return self.type in ("stop_market", "stop_limit")

    @property
    def is_live(self) -> bool:
        return self.status in LIVE


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    ts: str


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    ts: str

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class AgenticAccount:
    rhs_account_number: str
    agentic_allowed: bool


def _num(fields: dict, key: str, *, nullable=False, positive=False) -> float | None:
    if key not in fields:
        raise TapeError(f"missing field {key}")
    v = fields[key]
    if v is None and nullable:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TapeError(f"unreadable number in {key}: {v!r}") from None
    if not math.isfinite(x) or x < 0 or (positive and x <= 0):
        raise TapeError(f"bad number in {key}: {v!r}")
    return x


def _text(fields: dict, key: str, *, nullable=False, choices=None) -> str | None:
    if key not in fields:
        raise TapeError(f"missing field {key}")
    v = fields[key]
    if v is None and nullable:
        return None
    if not isinstance(v, str) or not v:
        raise TapeError(f"unreadable text in {key}: {v!r}")
    if choices and v not in choices:
        raise TapeError(f"unknown {key} {v!r}")
    return v


def make_order(f: dict) -> Order:
    """Every key must be present. Values may be null only where null has a meaning."""
    order = Order(id=_text(f, "id"), coin=_text(f, "coin").upper(), side=_text(f, "side", choices=SIDES),
                  type=_text(f, "type", choices=TYPES), qty=_num(f, "qty", positive=True),
                  filled_qty=_num(f, "filled_qty"), avg_fill_price=_num(f, "avg_fill_price", nullable=True),
                  limit_price=_num(f, "limit_price", nullable=True),
                  stop_price=_num(f, "stop_price", nullable=True),
                  status=_text(f, "status", choices=STATUSES), created_at=_text(f, "created_at"),
                  updated_at=_text(f, "updated_at", nullable=True),
                  reject_reason=_text(f, "reject_reason", nullable=True))
    if order.filled_qty > 0 and order.avg_fill_price is None:
        raise TapeError(f"order {order.id} filled without an average fill price")
    if order.is_stop and order.stop_price is None:
        raise TapeError(f"stop order {order.id} has no stop price")
    return order


def make_account(f: dict) -> Account:
    return Account(equity=_num(f, "equity"), cash=_num(f, "cash"),
                   buying_power=_num(f, "buying_power"), ts=_text(f, "ts"))


def make_quote(f: dict) -> Quote:
    q = Quote(bid=_num(f, "bid", positive=True), ask=_num(f, "ask", positive=True), ts=_text(f, "ts"))
    if q.ask < q.bid:
        raise TapeError("quote ask below bid")
    return q


def make_agentic_account(f: dict) -> AgenticAccount:
    if not isinstance(f.get("agentic_allowed"), bool):
        raise TapeError("missing field agentic_allowed")
    return AgenticAccount(_text(f, "rhs_account_number"), f["agentic_allowed"])


def _unmapped(role: str):
    def parse(_response):
        raise TapeError(f"no raw mapping for role {role} yet: built in Phase 1 from real responses")
    return parse


PARSERS = {role: _unmapped(role) for role in
           ("accounts", "account", "positions", "orders", "quotes", "place", "cancel")}


def parse(role: str, response):
    try:
        return PARSERS[role](response)
    except TapeError:
        raise
    except Exception as exc:          # any shape surprise is a TapeError, never a guess
        raise TapeError(f"{role}: {type(exc).__name__}: {exc}") from exc


def order_ids_in(role: str, response) -> list[str]:
    """For capture.py: order ids in an order-bearing response. Empty when unknown."""
    try:
        if role == "orders":
            return [o.id for o in parse(role, response)]
        if role in ("place", "cancel"):
            o = parse(role, response)
            return [o.id] if o else []
    except TapeError:
        pass
    return []


class Broker:
    def __init__(self, root: Path, tools: dict, now: datetime):
        self.paths = Paths(root)
        self.tools = tools
        self.now = now

    # ----- tape access -----
    def _load(self, path: Path) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                entry = json.load(fh)
            parse_ts(entry["ts"])
            entry["tool_response"]
            return entry
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TapeError(f"unreadable tape {path.name}: {exc}") from exc

    def entries(self, role: str, max_age: float) -> list[dict]:
        names = tools_for(self.tools, role, "read")
        if not names:
            raise TapeError(f"no read tool for role {role}")
        out = []
        for name in names:
            path = self.paths.tape / f"latest_{short_name(name)}.json"
            if not path.exists():
                raise StaleTape(f"{short_name(name)} not on the tape")
            entry = self._load(path)
            age = (self.now - parse_ts(entry["ts"])).total_seconds()
            if age > max_age or age < -5:
                raise StaleTape(f"{short_name(name)} is {age:.0f}s old")
            out.append(entry)
        return out

    def read_meta(self, roles, max_age: float) -> list[tuple[str, str]]:
        return [(e["tool_name"], e["ts"]) for r in roles for e in self.entries(r, max_age)]

    # ----- normalized reads -----
    def accounts(self, max_age: float) -> list[AgenticAccount]:
        return [a for e in self.entries("accounts", max_age) for a in parse("accounts", e["tool_response"])]

    def account(self, max_age: float) -> Account:
        return parse("account", self.entries("account", max_age)[0]["tool_response"])

    def positions(self, max_age: float) -> dict[str, float]:
        held = {}
        for e in self.entries("positions", max_age):
            for coin, qty in parse("positions", e["tool_response"]).items():
                if qty > 0:
                    held[coin.upper()] = float(qty)
        return held

    def recent_orders(self, max_age: float) -> list[Order]:
        by_id: dict[str, Order] = {}
        for e in sorted(self.entries("orders", max_age), key=lambda e: e["ts"]):
            for o in parse("orders", e["tool_response"]):
                by_id[o.id] = o
        return list(by_id.values())

    def open_orders(self, max_age: float) -> list[Order]:
        return [o for o in self.recent_orders(max_age) if o.is_live]

    def quotes(self, max_age: float) -> dict[str, Quote]:
        out = {}
        for e in self.entries("quotes", max_age):
            out.update({c.upper(): q for c, q in parse("quotes", e["tool_response"]).items()})
        return out

    # ----- order tracking -----
    def order(self, order_id: str, after: datetime) -> Order | None:
        """The order as seen by the newest status or placement result recorded after `after`."""
        path = self.paths.tape / f"order_{order_id}.json"
        if not path.exists():
            return None
        entry = self._load(path)
        if parse_ts(entry["ts"]) < after:
            return None
        role = role_of(self.tools["tools"].get(entry["tool_name"], {}))
        found = parse(role, entry["tool_response"])
        for o in (found if isinstance(found, list) else [found]):
            if o is not None and o.id == order_id:
                return o
        return None

    def placements(self, since: datetime) -> list[tuple[str, Order, str]]:
        """(ref_id, placed order, ts) for every placement on the tape since `since`."""
        out = []
        names = {short_name(n): n for n in tools_for(self.tools, "place", "place")}
        if not self.paths.tape.exists():
            return out
        for path in sorted(self.paths.tape.glob("2*_*.json")):
            short = path.stem.split("_", 1)[1]
            if short not in names:
                continue
            entry = self._load(path)
            if parse_ts(entry["ts"]) < since:
                continue
            ref = args_canonical(self.tools, names[short], entry.get("tool_input") or {}).get("ref_id")
            try:
                placed = parse("place", entry["tool_response"])
            except TapeError:
                continue                   # a rejected or errored call placed nothing readable
            if ref and placed is not None:
                out.append((str(ref), placed, entry["ts"]))
        return out

    def placement(self, ref_id: str, since: datetime) -> Order | None:
        found = [o for r, o, _ in self.placements(since) if r == ref_id]
        return found[-1] if found else None
