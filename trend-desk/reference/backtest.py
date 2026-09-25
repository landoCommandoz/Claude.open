"""
backtest.py | Backtest engine for the Robinhood trend agent

Replays the exact rules in strategy.py over daily history and charges
Robinhood's spread on every buy and every sell. Stops trigger on Robinhood's
bid, modeled as price x (1 - cost): with full OHLC data a stop triggers when
the bid at the day's low reaches it and fills at the stop price (at the
open's bid on a gap). Close-only data uses the close for all three. Stops
rise only through strategy.should_ratchet, the same rule the live desk uses.

Usage (single run, for exploring; the go/no-go decision is gate.py):
  python3 backtest.py --data-dir data --format ohlc --coins BTC ETH SOL DOGE --config config/risk.json
  python3 backtest.py --data-dir data --format coinmetrics --coins btc eth doge --n-mult 1.4
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from dataclasses import dataclass, asdict

import pandas as pd

from strategy import (Params, indicators, initial_stop, ratchet_stop, should_ratchet, size_position,
                      stop_fill, worst_case_loss)


@dataclass
class Position:
    coin: str
    qty: float
    entry_date: pd.Timestamp
    entry_raw: float
    entry_fill: float
    initial_stop: float
    stop: float

    def initial_risk(self) -> float:
        return self.qty * (self.entry_fill - self.initial_stop)

    def open_risk(self) -> float:
        return self.qty * max(0.0, self.entry_fill - self.stop)


@dataclass
class Trade:
    coin: str
    entry_date: str
    exit_date: str
    entry_fill: float
    exit_fill: float
    qty: float
    pnl: float
    r: float
    cost: float
    days: int


def load_prices(data_dir: str, coin: str, fmt: str) -> pd.DataFrame:
    """Load one coin into a UTC-date-indexed frame with close (and open/high/low if present)."""
    path = os.path.join(data_dir, f"{coin}.csv")
    raw = pd.read_csv(path, low_memory=False)
    if fmt == "coinmetrics":
        if "PriceUSD" not in raw.columns:
            raise ValueError(f"{path}: no PriceUSD column")
        df = pd.DataFrame({"close": pd.to_numeric(raw["PriceUSD"], errors="coerce")})
        df.index = pd.to_datetime(raw["time"], utc=True).dt.tz_localize(None).dt.normalize()
    elif fmt == "ohlc":
        needed = {"date", "open", "high", "low", "close"}
        if not needed.issubset(raw.columns):
            raise ValueError(f"{path}: needs columns {sorted(needed)}")
        df = raw[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        df.index = pd.to_datetime(raw["date"], utc=True).dt.tz_localize(None).dt.normalize()
    else:
        raise ValueError(f"unknown format {fmt}")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df["close"] > 0].dropna(subset=["close"])
    if df.empty:
        raise ValueError(f"{path}: no usable prices")
    return df


def simulate(data: dict, p: Params, *, start: str, end: str | None, cost_side: float,
             start_equity: float, floor: float, halt_on_floor: bool = True, n_mult: float = 1.0,
             daily_loss_frac: float = 0.04, weekly_loss_frac: float = 0.06,
             gap_frac: float = 0.0) -> dict:
    ind = {}
    for coin, df in data.items():
        frame = indicators(df, p)
        frame["n"] = frame["n"] * n_mult
        ind[coin] = frame
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else max(f.index.max() for f in ind.values())
    dates = sorted({d for f in ind.values() for d in f.index if start_ts <= d <= end_ts})
    if not dates:
        raise ValueError("no dates in the requested window")

    cash, peak, prev_equity = start_equity, start_equity, start_equity
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    curve: list[tuple] = []
    skipped: Counter = Counter()
    last_close: dict[str, float] = {}
    halted_on = None
    cur_week = None
    week_start_equity = start_equity
    realized_week = 0.0

    for d in dates:
        day_start_equity = prev_equity
        realized_today = 0.0
        week = tuple(d.isocalendar())[:2]
        if week != cur_week:
            cur_week, week_start_equity, realized_week = week, prev_equity, 0.0
        exited_today: set[str] = set()

        # 1. Stops fire first, against today's price action.
        for coin in list(positions):
            frame = ind[coin]
            if d not in frame.index:
                continue
            row = frame.loc[d]
            pos = positions[coin]
            if "low" in frame.columns and pd.notna(row["low"]) and pd.notna(row["open"]):
                exit_fill = stop_fill(float(row["open"]), float(row["low"]), pos.stop, cost_side)
            else:
                exit_fill = stop_fill(float(row["close"]), float(row["close"]), pos.stop, cost_side)
            if exit_fill is None:
                continue
            raw_exit = exit_fill / (1.0 - cost_side)
            cash += pos.qty * exit_fill
            pnl = pos.qty * (exit_fill - pos.entry_fill)
            risk0 = pos.initial_risk()
            cost = pos.qty * (pos.entry_fill - pos.entry_raw) + pos.qty * (raw_exit - exit_fill)
            trades.append(Trade(coin, str(pos.entry_date.date()), str(d.date()), pos.entry_fill,
                                exit_fill, pos.qty, pnl, pnl / risk0 if risk0 > 0 else float("nan"),
                                cost, (d - pos.entry_date).days))
            realized_today += pnl
            realized_week += pnl
            exited_today.add(coin)
            del positions[coin]

        # 2. Mark to market at the close.
        for coin, frame in ind.items():
            if d in frame.index:
                last_close[coin] = float(frame.at[d, "close"])
        equity = cash + sum(pos.qty * last_close[c] for c, pos in positions.items())
        peak = max(peak, equity)
        curve.append((d, equity, cash, len(positions)))

        # 3. Floor check.
        if halt_on_floor and halted_on is None and equity <= floor:
            halted_on = d

        # 4. Ratchet surviving stops (never down).
        for coin, pos in positions.items():
            if d in ind[coin].index:
                new = ratchet_stop(pos.stop, ind[coin].at[d, "exit_level"])
                if should_ratchet(pos.stop, new, float(ind[coin].at[d, "n"]), p.min_ratchet_n):
                    pos.stop = new

        # 5. New entries at the close.
        blocked = (halted_on is not None
                   or realized_today <= -daily_loss_frac * day_start_equity
                   or realized_week <= -weekly_loss_frac * week_start_equity)
        if blocked:
            prev_equity = equity
            continue
        candidates = [c for c, f in ind.items()
                      if c not in positions and c not in exited_today
                      and d in f.index and bool(f.at[d, "signal"])]
        def strength(c: str) -> float:
            mom = ind[c].at[d, "momentum"]
            return float(mom) if pd.notna(mom) else -1e9
        candidates.sort(key=strength, reverse=True)
        worst = sum(worst_case_loss(pos.qty, last_close[c], pos.stop, gap_frac)
                    for c, pos in positions.items())
        for coin in candidates:
            row = ind[coin].loc[d]
            raw = float(row["close"])
            fill = raw * (1.0 + cost_side)
            stop = initial_stop(fill, float(row["n"]), p)
            if not math.isfinite(stop):
                skipped["no valid stop"] += 1
                continue
            open_risk = sum(pos.open_risk() for pos in positions.values())
            s = size_position(equity=equity, peak=peak, cash=cash, fill=fill, stop=stop,
                              open_risk=open_risk, open_positions=len(positions), p=p,
                              floor=floor if halt_on_floor else 0.0, gap_frac=gap_frac,
                              open_worst_case=worst)
            if not s.ok:
                skipped[s.reason] += 1
                continue
            cash -= s.notional
            worst += worst_case_loss(s.qty, fill, stop, gap_frac)
            positions[coin] = Position(coin, s.qty, d, raw, fill, stop, stop)
        prev_equity = equity

    curve_df = pd.DataFrame(curve, columns=["date", "equity", "cash", "positions"]).set_index("date")
    return {"curve": curve_df, "trades": trades, "skipped": dict(skipped),
            "halted_on": str(halted_on.date()) if halted_on is not None else None,
            "open_positions": len(positions)}


def longest_losing_streak(trades: list) -> int:
    best = run = 0
    for t in sorted(trades, key=lambda t: t.exit_date):
        run = run + 1 if t.pnl <= 0 else 0
        best = max(best, run)
    return best


def metrics(result: dict, start_equity: float) -> dict:
    eq = result["curve"]["equity"]
    trades = result["trades"]
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    final = float(eq.iloc[-1])
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    month_end = eq.resample("ME").last()
    monthly = month_end.pct_change()
    monthly.iloc[0] = month_end.iloc[0] / start_equity - 1
    year_end = eq.resample("YE").last()
    yearly = year_end.pct_change()
    yearly.iloc[0] = year_end.iloc[0] / start_equity - 1
    rs = [t.r for t in trades if math.isfinite(t.r)]
    return {
        "start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
        "final_equity": round(final, 2),
        "total_return_pct": round((final / start_equity - 1) * 100, 1),
        "cagr_pct": round(((final / start_equity) ** (1 / years) - 1) * 100, 1) if final > 0 else None,
        "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
        "trades": len(trades),
        "trades_per_month": round(len(trades) / (years * 12), 2),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_win_r": round(sum(t.r for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_r": round(sum(t.r for t in losses) / len(losses), 2) if losses else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "longest_losing_streak": longest_losing_streak(trades),
        "total_spread_cost": round(sum(t.cost for t in trades), 2),
        "positive_months_pct": round(float((monthly > 0).mean()) * 100, 1),
        "worst_month_pct": round(float(monthly.min()) * 100, 1),
        "best_month_pct": round(float(monthly.max()) * 100, 1),
        "days_in_market_pct": round(float((result["curve"]["positions"] > 0).mean()) * 100, 1),
        "yearly_return_pct": {str(k.year): round(float(v) * 100, 1) for k, v in yearly.items()},
        "halted_on": result["halted_on"],
        "skipped": result["skipped"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Single backtest run")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--format", choices=["ohlc", "coinmetrics"], default="ohlc")
    ap.add_argument("--coins", nargs="+", required=True)
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--cost", type=float, default=0.006, help="spread per side, 0.006 = 0.6%%")
    ap.add_argument("--equity", type=float, default=400.0)
    ap.add_argument("--floor", type=float, default=320.0)
    ap.add_argument("--config", default=None, help="config/risk.json to load strategy params from")
    ap.add_argument("--no-halt", action="store_true", help="ignore the floor to see the full path")
    ap.add_argument("--n-mult", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    p, limits = Params(), {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        p = Params.from_dict(cfg.get("strategy", {}))
        acct = cfg.get("account", {})
        limits = {k: acct[c] for k, c in (("daily_loss_frac", "daily_loss_limit_frac"),
                                          ("weekly_loss_frac", "weekly_loss_limit_frac"),
                                          ("gap_frac", "floor_gap_allowance_frac")) if c in acct}
    data = {c.upper(): load_prices(args.data_dir, c, args.format) for c in args.coins}
    result = simulate(data, p, start=args.start, end=args.end, cost_side=args.cost,
                      start_equity=args.equity, floor=args.floor,
                      halt_on_floor=not args.no_halt, n_mult=args.n_mult, **limits)
    m = metrics(result, args.equity)
    if args.json:
        print(json.dumps(m, indent=2))
        return
    for k, v in m.items():
        print(f"{k:>24}: {v}")


if __name__ == "__main__":
    main()
