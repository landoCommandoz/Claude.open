"""
gate.py | Phase 0 go/no-go for the Robinhood trend agent

Runs the exact live rules across history with Robinhood's spread charged.
Four checks, all must pass:
  A. Full history: enough trades, profit factor, expectancy, drawdown.
  B. Recent regime (2022 on): still profitable after costs.
  C. Stress costs (execution.cost_per_side_stress in config): still profitable.
  D. Rolling first-year test: starting on any quarter, the floor almost never hits.

Exit code 0 = PASS, allowed to go live. Exit code 1 = FAIL, do not trade.

Usage:
  python3 gate.py --data-dir data --format ohlc --config config/risk.json
  python3 gate.py --data-dir data --format coinmetrics --coins btc eth doge \
                  --n-mult 1.4 --config ../config/risk.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys

import pandas as pd

from backtest import load_prices, metrics, simulate
from strategy import Params


def run(data, p, acct, start, end, cost, n_mult):
    res = simulate(data, p, start=start, end=end, cost_side=cost,
                   start_equity=acct["starting_capital"], floor=acct["floor_equity"],
                   halt_on_floor=True, n_mult=n_mult,
                   daily_loss_frac=acct["daily_loss_limit_frac"],
                   weekly_loss_frac=acct["weekly_loss_limit_frac"],
                   gap_frac=acct["floor_gap_allowance_frac"])
    return metrics(res, acct["starting_capital"])


def rolling(data, p, acct, start, last_date, cost, n_mult):
    rows = []
    for s in pd.date_range(start, last_date - pd.DateOffset(months=12), freq="QS"):
        e = s + pd.DateOffset(months=12) - pd.Timedelta(days=1)
        m = run(data, p, acct, str(s.date()), str(e.date()), cost, n_mult)
        rows.append({"start": str(s.date()), "final": m["final_equity"],
                     "floor_hit": m["halted_on"] is not None, "trades": m["trades"]})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 go/no-go gate")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--format", choices=["ohlc", "coinmetrics"], default="ohlc")
    ap.add_argument("--config", required=True)
    ap.add_argument("--coins", nargs="+", default=None, help="defaults to the config universe")
    ap.add_argument("--n-mult", type=float, default=1.0,
                    help="close-only data only: widen N toward true range")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    p, acct, g, ex = Params.from_dict(cfg["strategy"]), cfg["account"], cfg["gate"], cfg["execution"]
    coins = args.coins or cfg["universe"]
    data = {c.upper(): load_prices(args.data_dir, c, args.format) for c in coins}
    last_date = min(df.index.max() for df in data.values())
    base, stress = ex["cost_per_side_assumed"], ex["cost_per_side_stress"]

    full = run(data, p, acct, g["full_start"], None, base, args.n_mult)
    recent = run(data, p, acct, g["recent_start"], None, base, args.n_mult)
    hard = run(data, p, acct, g["full_start"], None, stress, args.n_mult)
    rows = rolling(data, p, acct, g["full_start"], last_date, base, args.n_mult)
    floor_hits = sum(r["floor_hit"] for r in rows)
    median_final = statistics.median(r["final"] for r in rows) if rows else 0.0

    def pf(m):
        return m["profit_factor"] or 0

    def er(m):
        return m["expectancy_r"] if m["expectancy_r"] is not None else -1

    start_cap = acct["starting_capital"]
    checks = [
        ("A full: trades", full["trades"] >= g["min_trades"], full["trades"]),
        ("A full: profit factor", pf(full) >= g["min_profit_factor"], pf(full)),
        ("A full: expectancy R", er(full) >= g["min_expectancy_r"], er(full)),
        ("A full: max drawdown %", full["max_drawdown_pct"] >= g["max_drawdown_pct"],
         full["max_drawdown_pct"]),
        ("B recent: profit factor", pf(recent) >= g["recent_min_profit_factor"], pf(recent)),
        ("B recent: expectancy R", er(recent) > 0, er(recent)),
        ("C stress: profit factor", pf(hard) >= g["stress_min_profit_factor"], pf(hard)),
        ("D rolling: floor hits", floor_hits <= g["rolling_max_floor_hits"],
         f"{floor_hits}/{len(rows)}"),
        ("D rolling: median 1-yr equity", median_final >= start_cap, round(median_final, 2)),
    ]
    print(f"Coins: {', '.join(data)} | data through {last_date.date()} "
          f"| cost {base:.2%}/side (stress {stress:.2%})")
    print(f"Full {full['start']}..{full['end']}: ${start_cap} -> ${full['final_equity']} "
          f"| CAGR {full['cagr_pct']}% | max DD {full['max_drawdown_pct']}% "
          f"| {full['trades_per_month']} trades/mo | win {full['win_rate_pct']}% "
          f"| positive months {full['positive_months_pct']}%")
    print(f"Recent {recent['start']}..{recent['end']}: -> ${recent['final_equity']} "
          f"| CAGR {recent['cagr_pct']}% | max DD {recent['max_drawdown_pct']}%")
    print(f"Yearly: {full['yearly_return_pct']}")
    finals = [r["final"] for r in rows]
    print(f"First-year outcomes over {len(rows)} start dates: worst ${min(finals)} "
          f"| median ${median_final:.2f} | best ${max(finals)} "
          f"| ended below start {sum(f < start_cap for f in finals)}/{len(rows)}")
    for name, ok, value in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {value}")
    passed = all(ok for _, ok, _ in checks)
    print("GATE: PASS" if passed else "GATE: FAIL. Do not trade live.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
