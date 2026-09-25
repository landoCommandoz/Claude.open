"""BUILD.md section 4.9: parity.

Replay the last 400 days through the engine's plan and confirm logic against a simulated
broker that fills at the close plus cost. The trades must match backtest.py over the same
window exactly: same coins, same entry dates, same exit dates.
Candles are a frozen snapshot of real Coinbase data (tests/fixtures/candles), so the result
is the same on every machine.
"""
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from backtest import load_prices, simulate
from helpers import REPO, make_root
from sim import replay
from strategy import Params

CANDLES = Path(__file__).resolve().parent / "fixtures" / "candles"
WINDOW_DAYS = 400
CLOSED_TRADES = 31          # frozen candles + bid-triggered stops (CHANGELOG 1.3)


def run_both(tmp_path, cfg_edit=None):
    root = make_root(tmp_path, cfg_edit=cfg_edit)
    cfg = json.loads((root / "config" / "risk.json").read_text())
    frames = {c: load_prices(str(CANDLES), c, "ohlc") for c in cfg["universe"]}
    end = min(f.index.max() for f in frames.values())
    start = end - pd.Timedelta(days=WINDOW_DAYS - 1)
    a, ex = cfg["account"], cfg["execution"]
    bt = simulate(frames, Params.from_dict(cfg["strategy"]), start=str(start.date()), end=str(end.date()),
                  cost_side=ex["cost_per_side_assumed"], start_equity=a["starting_capital"],
                  floor=a["floor_equity"], daily_loss_frac=a["daily_loss_limit_frac"],
                  weekly_loss_frac=a["weekly_loss_limit_frac"], gap_frac=a["floor_gap_allowance_frac"])
    days = [d.date() for d in pd.date_range(start, end, freq="D")]
    sim = replay(root, frames, days, cfg)
    from state import Paths, read_journal, read_json
    journal = read_journal(Paths(root))
    st = read_json(Paths(root).state)
    live = sorted((r["coin"], r["trade_id"].split("-", 1)[1], r["exit_ts"][:10]) for r in journal)
    back = sorted((t.coin, t.entry_date, t.exit_date) for t in bt["trades"])
    live_open = sorted((c, p["trade_id"].split("-", 1)[1]) for c, p in st["positions"].items())
    return live, back, live_open, bt, st, sim


def diff(live, back):
    return {"only_live": sorted(set(live) - set(back)), "only_backtest": sorted(set(back) - set(live))}


def test_parity_last_400_days_matches_backtest_exactly(tmp_path):
    live, back, live_open, bt, st, sim = run_both(tmp_path)
    assert st["state"] not in ("PAUSED", "HALTED"), st["reason"]
    assert live == back, json.dumps(diff(live, back), indent=1)
    assert len(back) == CLOSED_TRADES                        # every closed trade in the window
    assert len(live_open) == bt["open_positions"]
    eq_live = sim.cash + sum(q * sim.close(c) for c, q in sim.hold.items())
    assert abs(eq_live - float(bt["curve"]["equity"].iloc[-1])) < 0.05
