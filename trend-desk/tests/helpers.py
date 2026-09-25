"""Shared test setup: a throwaway desk folder, a fake broker of normalized objects, candles."""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from broker import Account, AgenticAccount, Order, Quote, StaleTape
from deskconfig import READ_ROLES, tools_for
from state import Paths, atomic_write_json, iso, log_event

REPO = Path(__file__).resolve().parent.parent
TOOLS = json.loads((REPO / "tests" / "fixtures" / "tools.test.json").read_text())
ACCT = "5QA00001"
ACCT_MAIN = "5QA00001-M"
SERVER = "mcp__robinhood-trading__"
PLACE, CANCEL, ORDERS = SERVER + "place_crypto_order", SERVER + "cancel_crypto_order", SERVER + "get_crypto_orders"
NOW = datetime(2026, 10, 2, 0, 7, tzinfo=timezone.utc)
SESSION = date(2026, 10, 1)


def make_root(tmp: Path, *, copy_scripts=False, mode="LIVE", cfg_edit=None, account=ACCT) -> Path:
    root = Path(tmp) / "desk"
    (root / "config").mkdir(parents=True)
    cfg = json.loads((REPO / "config" / "risk.json").read_text())
    cfg["mode"] = mode
    if cfg_edit:
        cfg_edit(cfg)
    atomic_write_json(root / "config" / "risk.json", cfg)
    atomic_write_json(root / "config" / "tools.json", TOOLS)
    if copy_scripts:
        shutil.copytree(REPO / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "prompts", root / "prompts")
    if account:
        from state import default_state, save_state
        st = default_state(cfg)
        st["account"] = account
        save_state(Paths(root), st)
    return root


def order(id_, coin="BTC", side="sell", type_="stop_market", qty=0.1, filled=0.0, avg=None, limit=None,
          stop=None, status="open", created="2026-09-20T00:10:00Z", updated=None, reason=None) -> Order:
    return Order(id_, coin, side, type_, qty, filled, avg, limit, stop, status, created, updated, reason)


class FakeBroker:
    """Implements broker.Broker's interface with normalized objects only."""

    def __init__(self, *, equity=400.0, cash=400.0, held=None, orders=None, quotes=None,
                 accounts=None, stale=False, placements=None, statuses=None, now=NOW):
        self.acct = Account(equity, cash, cash, iso(now))
        self.held, self.orders_ = dict(held or {}), list(orders or [])
        self.quotes_ = dict(quotes or {})
        self.accounts_ = accounts if accounts is not None else [AgenticAccount(ACCT, True, ACCT_MAIN)]
        self.stale, self.now = stale, now
        self.placed = dict(placements or {})        # ref_id -> Order
        self.statuses = dict(statuses or {})        # order_id -> Order

    def _check(self):
        if self.stale:
            raise StaleTape("get_crypto_positions is 900s old")

    def accounts(self, max_age):
        self._check()
        return self.accounts_

    def account(self, max_age):
        self._check()
        return self.acct

    def positions(self, max_age):
        self._check()
        return dict(self.held)

    def recent_orders(self, max_age):
        self._check()
        return list(self.orders_)

    def open_orders(self, max_age):
        return [o for o in self.recent_orders(max_age) if o.is_live]

    def quotes(self, max_age):
        self._check()
        return dict(self.quotes_)

    def read_meta(self, roles, max_age):
        self._check()
        return [(t, iso(self.now)) for r in roles for t in tools_for(TOOLS, r, "read")]

    def order(self, order_id, after):
        return self.statuses.get(order_id)

    def placement(self, ref_id, since):
        return self.placed.get(ref_id)

    def placements(self, since):
        return [(r, o, iso(self.now)) for r, o in self.placed.items()]


def log_guard_reads(root: Path, now=NOW) -> None:
    """What the guard hook writes before each read tool runs."""
    for role in READ_ROLES:
        for tool in tools_for(TOOLS, role, "read"):
            log_event(Paths(root), "guard", now - timedelta(seconds=2), decision="allow", tool=tool, reason="read")


def quote(mid: float, spread: float = 0.01) -> Quote:
    return Quote(round(mid * (1 - spread / 2), 2), round(mid * (1 + spread / 2), 2), iso(NOW))


def candles(closes: list[float], end: date = SESSION) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(end), periods=len(closes), freq="D")
    c = pd.Series([float(x) for x in closes], index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"open": o, "high": pd.concat([o, c], axis=1).max(axis=1) * 1.005,
                         "low": pd.concat([o, c], axis=1).min(axis=1) * 0.995, "close": c})


def breakout(last: float = 110.0, days: int = 80) -> pd.DataFrame:
    """Chop between 97 and 103, then a close above the 20-day high and the 50-day average."""
    return candles([97.0 if i % 2 == 0 else 103.0 for i in range(days - 1)] + [last])


def flat(days: int = 80) -> pd.DataFrame:
    return candles([97.0 if i % 2 == 0 else 103.0 for i in range(days)])


def ctx_for(root: Path, broker: FakeBroker, frames: dict, public=None, now=NOW, alerts=None):
    from desk import Ctx
    sink = alerts if alerts is not None else []
    return Ctx(root, now, broker=broker, frames=frames,
               public_price=public or (lambda c: broker.quotes_[c].mid if c in broker.quotes_ else None),
               alert=lambda t, m: sink.append((t, m)))
