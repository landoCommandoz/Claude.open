"""
desk.py | Shared context for engine commands: config, state, broker, market data, incidents.

Session day: the DAILY run at 00:07 UTC acts on yesterday's completed candle, the same bar the
backtest trades at its close. Realized P&L is attributed to the UTC date of each exit fill
(from the journal), and the daily and weekly brakes for a DAILY run are measured on that
session day, exactly as backtest.py does.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from alert import send as send_alert
from backtest import load_prices
from broker import Broker
from deskconfig import load_config, load_tools
from state import (STICKY, Paths, append_journal, append_jsonl, iso, load_state, log_event,
                   parse_ts, read_journal, read_json, save_state)
from strategy import Params, indicators

ALERT_WORDS = ("guard", "stop", "unknown", "reject", "pause")
KEEP_STARTS_DAYS = 21


def denver(dt: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Denver")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:                      # no tz database: US Mountain rule by hand
        y = dt.year
        start = datetime(y, 3, 8 + (6 - date(y, 3, 8).weekday()) % 7, 9, tzinfo=dt.tzinfo)
        end = datetime(y, 11, 1 + (6 - date(y, 11, 1).weekday()) % 7, 8, tzinfo=dt.tzinfo)
        off, name = (6, "MDT") if start <= dt < end else (7, "MST")
        return (dt - timedelta(hours=off)).strftime(f"%Y-%m-%d %H:%M {name}")


class Ctx:
    def __init__(self, root: Path, now: datetime, *, cfg=None, tools=None, broker=None,
                 frames=None, public_price=None, alert=None, load_tools_now=True):
        self.root, self.now = Path(root), now
        self.paths = Paths(root)
        self.cfg = cfg or load_config(self.paths.config)
        self.mode = self.cfg["mode"]
        self.p = Params.from_dict(self.cfg["strategy"])
        self.acct, self.ex = self.cfg["account"], self.cfg["execution"]
        self.tools = tools if tools is not None else (
            load_tools(self.paths.tools, self.cfg["universe"]) if load_tools_now else None)
        self._broker, self._frames, self._ind = broker, dict(frames or {}), {}
        self._injected = frames is not None
        self._public, self._alert = public_price, alert
        self.st = load_state(self.paths, self.cfg)
        self.messages: list[str] = []

    # ----- dependencies -----
    @property
    def broker(self):
        if self._broker is None:
            self._broker = Broker(self.root, self.tools, self.now)
        return self._broker

    def say(self, line: str) -> None:
        self.messages.append(line)
        print(line)

    def alert(self, title: str, message: str) -> None:
        if self._alert is not None:
            self._alert(title, message)
        else:
            send_alert(self.root, self.cfg, title, message, self.now)

    def event(self, type_: str, **fields) -> None:
        log_event(self.paths, type_, self.now, **fields)

    def incident(self, text: str, force_alert: bool = False) -> None:
        append_jsonl(self.paths.incidents, {"ts": iso(self.now), "text": text})
        self.event("incident", text=text)
        self.say(f"INCIDENT: {text}")
        if force_alert or any(w in text.lower() for w in ALERT_WORDS):
            self.alert("Trend Desk incident", text)

    def pause(self, reason: str) -> None:
        if self.st["state"] != "PAUSED":
            self.st["state"], self.st["reason"] = "PAUSED", reason
        elif reason not in self.st["reason"]:
            self.st["reason"] = f"{self.st['reason']}; {reason}"[:300]
        self.incident(f"PAUSED: {reason}", force_alert=True)

    def halt(self, reason: str) -> None:
        if self.st["state"] == "PAUSED":
            self.say(f"NOTE: floor reached while PAUSED ({reason})")
            self.alert("Trend Desk at floor", reason)
            return
        if self.st["state"] != "HALTED":
            self.st["state"], self.st["reason"] = "HALTED", reason
            self.event("halted", reason=reason)
            self.alert("Trend Desk HALTED", reason)

    def save(self) -> None:
        save_state(self.paths, self.st)

    def stop_file(self) -> bool:
        return self.paths.stop_file.exists()

    # ----- dates -----
    @property
    def today(self) -> date:
        return self.now.date()

    @property
    def yesterday(self) -> date:
        return self.today - timedelta(days=1)

    # ----- market data -----
    def frame(self, coin: str) -> pd.DataFrame | None:
        if coin not in self._frames:
            try:
                self._frames[coin] = load_prices(str(self.paths.data), coin, "ohlc")
            except (OSError, ValueError):
                self._frames[coin] = None
        return self._frames[coin]

    def row(self, coin: str, day: date):
        """Indicator row for one completed candle, or None."""
        if coin not in self._ind:
            f = self.frame(coin)
            self._ind[coin] = indicators(f, self.p) if f is not None else None
        ind = self._ind[coin]
        ts = pd.Timestamp(day)
        if ind is None or ts not in ind.index:
            return None
        return ind.loc[ts]

    def stale_coins(self, session: date) -> dict[str, str]:
        status = (read_json(self.paths.data / "status.json", {}) or {}).get("coins", {})
        out = {}
        for coin in self.cfg["universe"]:
            s = status.get(coin, {})
            f = self.frame(coin)
            if self._injected and not status:
                s = {"status": "OK"}                 # injected frames (tests, parity) carry no status file
            if s.get("status") != "OK":
                out[coin] = f"data {s.get('status', 'missing')}"
            elif f is None or f.index.max().date() != session:
                out[coin] = "last candle is not yesterday"
        return out

    def public(self, coin: str) -> float | None:
        try:
            if self._public is not None:
                return self._public(coin)
            from data import BASE_URL, get_json
            return float(get_json(f"{BASE_URL}/{coin}-USD/ticker")["price"])
        except Exception:
            return None

    # ----- money records -----
    def journal(self) -> list[dict]:
        return read_journal(self.paths)

    def realized_on(self, days: set[str]) -> float:
        return sum(float(r["pnl_usd"]) for r in self.journal() if r["exit_ts"][:10] in days)

    def note_start(self, equity: float) -> None:
        starts = self.st["starts"]
        starts.setdefault(self.today.isoformat(), float(equity))
        cutoff = (self.today - timedelta(days=KEEP_STARTS_DAYS)).isoformat()
        self.st["starts"] = {d: v for d, v in starts.items() if d >= cutoff}

    def brakes(self, session: date, equity: float) -> tuple[bool, bool]:
        """(daily brake on, weekly brake on) for entries on the session day, as backtest.py measures."""
        starts = self.st["starts"]
        day_start = starts.get(session.isoformat(), equity)
        day_loss = self.realized_on({session.isoformat()})
        monday = session - timedelta(days=session.weekday())
        week_days = [(monday + timedelta(days=i)).isoformat() for i in range((session - monday).days + 1)]
        week_start = next((starts[d] for d in week_days if d in starts), equity)
        week_loss = self.realized_on(set(week_days))
        return (day_loss <= -self.acct["daily_loss_limit_frac"] * day_start,
                week_loss <= -self.acct["weekly_loss_limit_frac"] * week_start)

    def display_state(self, session: date | None = None) -> str:
        if self.st["state"] in STICKY:
            return self.st["state"]
        day_brake, week_brake = self.brakes(session or self.today, self.st["last_equity"])
        if day_brake:
            return "COOLDOWN_DAY"
        if week_brake:
            return "COOLDOWN_WEEK"
        if self.mode == "DRY":
            return "DRY"
        return "IN_TRADE" if self.st["positions"] else "SCANNING"

    def open_risk(self) -> float:
        return sum(p["qty"] * max(0.0, p["entry_fill"] - p["stop"]) for p in self.st["positions"].values())

    def close_trade(self, coin: str, qty: float, exit_fill: float, exit_ts: str, reason: str,
                    stop_ref: float | None) -> None:
        """Journal an exit (whole or partial), update the position, and check the slip allowance."""
        pos = self.st["positions"][coin]
        q = min(float(qty), pos["qty"])
        entry, cost = pos["entry_fill"], self.ex["cost_per_side_assumed"]
        pnl = q * (exit_fill - entry)
        risk0 = q * (entry - pos["initial_stop"])
        spread = q * max(0.0, entry - pos.get("entry_mid", entry)) + q * exit_fill * cost / (1 - cost)
        entry_dt, exit_dt = parse_ts(pos["entry_ts"]), parse_ts(exit_ts)
        append_journal(self.paths, {
            "trade_id": pos.get("trade_id") or f"{coin}-{pos['entry_ts'][:10]}", "coin": coin,
            "entry_ts": pos["entry_ts"], "entry_fill": round(entry, 10), "qty": q,
            "initial_stop": pos["initial_stop"], "exit_ts": iso(exit_dt), "exit_fill": round(exit_fill, 10),
            "exit_reason": reason, "pnl_usd": round(pnl, 6),
            "r_multiple": round(pnl / risk0, 4) if risk0 > 0 else "",
            "spread_paid_usd": round(spread, 6), "days_held": (exit_dt.date() - entry_dt.date()).days})
        self.event("exit", coin=coin, qty=q, fill=exit_fill, reason=reason, stop_ref=stop_ref)
        if q >= pos["qty"] * (1 - self.ex["dust_qty_frac"]):
            del self.st["positions"][coin]
            self.st["exited"][coin] = exit_dt.date().isoformat()
        else:
            pos["qty"] -= q
        self.say(f"EXIT {coin} qty {q:g} at {exit_fill:g} ({reason}), P&L {pnl:+.2f}")
        slip = self.acct["stop_slip_pause_frac"]
        if stop_ref and math.isfinite(stop_ref) and exit_fill < stop_ref * (1 - slip):
            self.pause(f"stop slipped past allowance: {coin} filled {exit_fill:g}, "
                       f"{(1 - exit_fill / stop_ref):.1%} below stop {stop_ref:g}")

    def remember_order(self, order_id: str, coin: str, kind: str) -> None:
        known = self.st.setdefault("known_orders", {})
        known[order_id] = {"coin": coin, "kind": kind, "ts": iso(self.now)}
        cutoff = iso(self.now - timedelta(days=30))
        self.st["known_orders"] = {k: v for k, v in known.items() if v["ts"] >= cutoff}
