"""
strategy.py | Turtle-based crypto trend rules for the Robinhood agent

Single source of truth for every number the system trades on:
signals, stops, trailing, and position size. Pure functions only.
No network, no files, no clock. The live agent and the backtester
both import this file, so the backtest and live trading cannot drift.

Live parameter values come from config/risk.json via Params.from_dict.
"""
from __future__ import annotations

import math
import numbers
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Params:
    entry_lookback: int = 20       # enter when close beats the highest close of the prior 20 days
    exit_lookback: int = 10        # trail the stop at the lowest close of the last 10 days
    trend_sma: int = 50            # only buy coins closing above their 50-day average
    atr_period: int = 20           # N = 20-day Wilder average true range (Turtle "N")
    stop_atr_mult: float = 2.0     # initial stop = fill - 2N
    min_ratchet_n: float = 0.25    # raise a stop only when the new level is at least 0.25N higher
    risk_per_trade: float = 0.01   # 1% of sizing equity per trade (picked from drawdown tests)
    max_position_frac: float = 0.30
    max_positions: int = 3
    max_open_risk_frac: float = 0.03   # 3 positions x 1% risk
    min_stop_frac: float = 0.03    # skip trades whose stop is under 3% away (spread eats them)
    min_notional: float = 20.0
    cash_buffer_frac: float = 0.03
    dd_step: float = 0.10          # every 10% below peak equity...
    dd_cut: float = 0.20           # ...cuts sizing equity by 20% (original Turtle rule)

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass(frozen=True)
class Sizing:
    ok: bool
    reason: str
    qty: float = 0.0
    notional: float = 0.0
    risk: float = 0.0


def is_real(x) -> bool:
    """True for finite real numbers (numpy types included). Rejects bool, None, NaN, inf."""
    return isinstance(x, numbers.Real) and not isinstance(x, bool) and math.isfinite(float(x))


def true_range(df: pd.DataFrame) -> pd.Series:
    """Classic true range when high/low exist, absolute close change otherwise."""
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    if {"high", "low"}.issubset(df.columns):
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        parts = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
        return parts.max(axis=1, skipna=True)
    return (close - prev_close).abs()


def wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing seeded with a simple average. Gaps carry the last value forward."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float] = []
    seed: list[float] = []
    prev = float("nan")
    for v in series.astype(float).to_numpy():
        if not math.isfinite(v):
            out.append(prev)
            continue
        if not math.isfinite(prev):
            seed.append(v)
            if len(seed) == period:
                prev = sum(seed) / period
            out.append(prev)
            continue
        prev = (prev * (period - 1) + v) / period
        out.append(prev)
    return pd.Series(out, index=series.index, dtype=float)


def indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """Everything the rules need, computed from completed daily bars only."""
    if "close" not in df.columns:
        raise ValueError("price data needs a close column")
    out = pd.DataFrame(index=df.index)
    out["close"] = df["close"].astype(float)
    for col in ("open", "high", "low"):
        if col in df.columns:
            out[col] = df[col].astype(float)
    out["n"] = wilder(true_range(df), p.atr_period)
    out["sma"] = out["close"].rolling(p.trend_sma, min_periods=p.trend_sma).mean()
    prior = out["close"].shift(1)
    out["entry_level"] = prior.rolling(p.entry_lookback, min_periods=p.entry_lookback).max()
    out["exit_level"] = out["close"].rolling(p.exit_lookback, min_periods=p.exit_lookback).min()
    out["momentum"] = out["close"] / out["close"].shift(p.entry_lookback) - 1.0
    ready = out[["n", "sma", "entry_level", "exit_level"]].notna().all(axis=1)
    out["signal"] = ready & (out["close"] > out["entry_level"]) & (out["close"] > out["sma"])
    return out


def initial_stop(fill: float, n: float, p: Params) -> float:
    """Initial protective stop. NaN means no valid stop, and no valid stop means no trade."""
    if not (is_real(fill) and is_real(n)) or fill <= 0 or n <= 0:
        return float("nan")
    return float(fill) - p.stop_atr_mult * float(n)


def ratchet_stop(current: float, exit_level: float) -> float:
    """Stops only move up. A missing exit level keeps the current stop."""
    if not is_real(current):
        raise ValueError("current stop must be a finite number")
    if not is_real(exit_level):
        return float(current)
    return max(float(current), float(exit_level))


def should_ratchet(current_stop: float, new_level: float, n: float, min_ratchet_n: float) -> bool:
    """Raise a stop only when the new level is at least min_ratchet_n x N above it.

    Small raises are skipped: every raise is a cancel and a replace at Robinhood, and the
    live desk and the backtest must skip the same ones.
    """
    if not all(is_real(x) for x in (current_stop, new_level, n, min_ratchet_n)):
        return False
    if n <= 0 or min_ratchet_n < 0:
        return False
    return new_level > current_stop and new_level - current_stop >= min_ratchet_n * n


def model_bid(price: float, cost_side: float) -> float:
    """Robinhood's bid, modeled as the market price less one side of the spread."""
    return float(price) * (1.0 - float(cost_side))


def stop_fill(open_: float, low: float, stop: float, cost_side: float) -> float | None:
    """Where a resting sell stop fills during one daily bar, or None if it does not trigger.

    The live desk's stops trigger on Robinhood's bid, so the bar triggers the stop when the
    bid at its low reaches the stop. It fills at the stop price, or at the bid at the open
    when the bar opens with the bid already at or below the stop (a gap).
    """
    if not all(is_real(x) for x in (open_, low, stop, cost_side)) or stop <= 0:
        return None
    if model_bid(low, cost_side) > stop:
        return None
    open_bid = model_bid(open_, cost_side)
    return open_bid if open_bid <= stop else float(stop)


def drawdown_multiplier(equity: float, peak: float, p: Params) -> float:
    """Turtle rule: each full 10% below peak cuts sizing equity by 20%."""
    if not (is_real(equity) and is_real(peak)) or peak <= 0 or equity <= 0:
        return 0.0
    dd = max(0.0, 1.0 - float(equity) / float(peak))
    steps = math.floor(dd / p.dd_step + 1e-12)
    return max(0.0, 1.0 - p.dd_cut * steps)


def round_down(qty: float, increment: float) -> float:
    """Round a quantity down to the broker's increment. Never rounds up."""
    if not (is_real(qty) and is_real(increment)) or qty <= 0 or increment <= 0:
        return 0.0
    steps = math.floor(float(qty) / float(increment) + 1e-9)
    return round(steps * float(increment), 12)


def worst_case_loss(qty: float, price: float, stop: float, gap_frac: float) -> float:
    """Loss from `price` if the stop fills gap_frac below its trigger (a crash through the stop)."""
    if not all(is_real(x) for x in (qty, price, stop, gap_frac)) or qty <= 0:
        return 0.0
    return float(qty) * max(0.0, float(price) - float(stop) * (1.0 - float(gap_frac)))


def size_position(*, equity: float, peak: float, cash: float, fill: float, stop: float,
                  open_risk: float, open_positions: int, p: Params,
                  floor: float = 0.0, gap_frac: float = 0.0, open_worst_case: float = 0.0) -> Sizing:
    """Position size for one new trade, or the exact reason it is refused.

    Worst-case floor: when floor > 0, the trade is sized so that even if every open stop
    and this new one filled gap_frac below its trigger, equity would stay at or above floor.
    """
    for name, value in (("equity", equity), ("peak", peak), ("cash", cash),
                        ("fill", fill), ("stop", stop), ("open_risk", open_risk),
                        ("floor", floor), ("gap_frac", gap_frac), ("open_worst_case", open_worst_case)):
        if not is_real(value):
            return Sizing(False, f"invalid {name}")
    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        return Sizing(False, "invalid open_positions")
    if equity <= 0 or fill <= 0:
        return Sizing(False, "non-positive equity or price")
    if open_positions >= p.max_positions:
        return Sizing(False, "max positions reached")
    stop_dist = fill - stop
    if stop_dist <= 0:
        return Sizing(False, "stop at or above entry")
    if stop_dist / fill < p.min_stop_frac:
        return Sizing(False, "stop too tight for spread costs")
    mult = drawdown_multiplier(equity, peak, p)
    if mult <= 0:
        return Sizing(False, "drawdown multiplier is zero")
    risk_budget = min(p.risk_per_trade * equity * mult, p.max_open_risk_frac * equity - open_risk)
    if risk_budget <= 0:
        return Sizing(False, "open risk limit reached")
    notional = min(risk_budget / stop_dist * fill,
                   p.max_position_frac * equity,
                   cash - p.cash_buffer_frac * equity)
    if not 0.0 <= gap_frac < 0.5:
        return Sizing(False, "invalid gap_frac")
    if floor > 0:
        room = equity - floor - open_worst_case
        if room <= 0:
            return Sizing(False, "floor buffer reached")
        notional = min(notional, room / (fill - stop * (1.0 - gap_frac)) * fill)
    if notional < p.min_notional:
        return Sizing(False, "position below minimum size")
    qty = notional / fill
    return Sizing(True, "ok", qty=qty, notional=notional, risk=qty * stop_dist)
