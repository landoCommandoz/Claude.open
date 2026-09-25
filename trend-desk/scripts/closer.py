"""
closer.py | engine.py close, status, and weekly.

close: peak equity, day and week records, reports/daily/{date}.md on DAILY, STATUS.md,
the daily summary alert, state/runs.jsonl, and the status line as the last line printed.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

from state import atomic_write_text, append_jsonl, iso, parse_ts, read_jsonl
from desk import denver

DRIFT_MIN_TRADES = 30


def next_daily(ctx) -> str:
    hh, mm = map(int, ctx.cfg["schedule"]["daily_run_utc"].split(":"))
    t = ctx.now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return iso(t if t > ctx.now else t + timedelta(days=1))


def status_line(ctx, job: str) -> dict:
    today = ctx.today.isoformat()
    monday = ctx.today - timedelta(days=ctx.today.weekday())
    week_days = {(monday + timedelta(days=i)).isoformat() for i in range(7)}
    events = [e for e in read_jsonl(ctx.paths.events) if e.get("ts", "")[:10] == today]
    incidents = [i for i in read_jsonl(ctx.paths.incidents) if i.get("ts", "")[:10] == today]
    return {"job": job, "state": ctx.display_state(), "mode": ctx.mode,
            "equity": round(ctx.st["last_equity"], 2),
            "day_pnl": round(ctx.realized_on({today}), 2),
            "week_pnl": round(ctx.realized_on(week_days), 2),
            "open_positions": len(ctx.st["positions"]), "open_risk": round(ctx.open_risk(), 2),
            "floor": ctx.acct["floor_equity"],
            "trades_today": sum(e.get("type") in ("entry", "exit") for e in events),
            "incidents_today": len(incidents), "next_daily_utc": next_daily(ctx)}


def write_status_md(ctx, line: dict) -> None:
    st = ctx.st
    rows = ["| Coin | Qty | Entry | Stop | Stop order |", "|---|---|---|---|---|"]
    for coin, p in sorted(st["positions"].items()):
        rows.append(f"| {coin} | {p['qty']:g} | {p['entry_fill']:g} | {p['stop']:g} | "
                    f"{'live' if p.get('stop_order_id') else 'MISSING'} |")
    if len(rows) == 2:
        rows.append("| none | | | | |")
    text = "\n".join([
        "# TREND DESK STATUS", "",
        f"Updated {iso(ctx.now)} ({denver(ctx.now)})", "",
        f"- State: **{line['state']}**{' (' + st['reason'] + ')' if st['reason'] else ''}",
        f"- Mode: {ctx.mode}",
        f"- Equity: ${line['equity']:.2f} | floor ${line['floor']} | peak ${st['peak_equity']:.2f}",
        f"- Today P&L: ${line['day_pnl']:+.2f} | week ${line['week_pnl']:+.2f}",
        f"- Open risk: ${line['open_risk']:.2f} | incidents today: {line['incidents_today']}",
        f"- STOP file: {'present, no new buys' if ctx.stop_file() else 'none'}",
        f"- Next daily run: {line['next_daily_utc']} ({denver(parse_ts(line['next_daily_utc']))})",
        "", "## Positions", "", *rows, "",
        "Clear PAUSED or HALTED only from your own terminal: python3 scripts/engine.py clear --reason \"why\"",
        ""])
    atomic_write_text(ctx.paths.status_md, text)


def write_daily_report(ctx, line: dict) -> None:
    plan = json.loads((ctx.paths.plans / "latest.json").read_text(encoding="utf-8")) \
        if (ctx.paths.plans / "latest.json").exists() else {}
    lines = [f"# Daily report {ctx.yesterday.isoformat()} session", "",
             f"Run at {iso(ctx.now)} ({denver(ctx.now)}). State {line['state']}, mode {ctx.mode}.",
             f"Equity ${line['equity']:.2f}. Day P&L ${line['day_pnl']:+.2f}. Week ${line['week_pnl']:+.2f}.", "",
             "## Actions"]
    lines += [f"- {a['id']} {a['kind']} {a['coin']}: {a['why']}" for a in plan.get("actions", [])] or ["- none"]
    lines += ["", "## Skipped"]
    lines += [f"- {s['coin']}: {s['reason']}" for s in plan.get("skipped", [])] or ["- none"]
    lines += ["", "## Notes"] + ([f"- {n}" for n in plan.get("notes", [])] or ["- none"]) + [""]
    atomic_write_text(ctx.paths.reports / "daily" / f"{ctx.today.isoformat()}.md", "\n".join(lines))


def close(ctx, job: str) -> dict:
    try:
        ctx.st["last_equity"] = ctx.broker.account(1200).equity
    except Exception:
        pass                                 # no fresh account read: keep the plan's equity
    eq = ctx.st["last_equity"]
    ctx.st["peak_equity"] = max(ctx.st["peak_equity"], eq)
    ctx.note_start(eq)
    today = ctx.today.isoformat()
    monday = (ctx.today - timedelta(days=ctx.today.weekday()))
    week_days = {(monday + timedelta(days=i)).isoformat() for i in range(7)}
    ctx.st["day"] = {"date": today, "start_equity": ctx.st["starts"].get(today, eq),
                     "realized": round(ctx.realized_on({today}), 6)}
    ctx.st["week"] = {"iso": f"{ctx.today.isocalendar()[0]}-W{ctx.today.isocalendar()[1]:02d}",
                      "start_equity": next((ctx.st["starts"][d] for d in sorted(week_days)
                                            if d in ctx.st["starts"]), eq),
                      "realized": round(ctx.realized_on(week_days), 6)}
    if eq <= ctx.acct["floor_equity"]:
        ctx.halt(f"equity {eq:.2f} at or below floor {ctx.acct['floor_equity']}")
    ctx.st["last_success"][job] = iso(ctx.now)
    line = status_line(ctx, job)
    ctx.save()
    write_status_md(ctx, line)
    if job == "DAILY":
        write_daily_report(ctx, line)
        if ctx.cfg["alerts"]["daily_summary"]:
            ctx.alert(f"Trend Desk {line['state']}",
                      f"Equity ${line['equity']:.2f}, day {line['day_pnl']:+.2f}, week {line['week_pnl']:+.2f}, "
                      f"{line['open_positions']} open, {line['incidents_today']} incidents")
    append_jsonl(ctx.paths.runs, {"ts": iso(ctx.now), **line})
    return line


def weekly(ctx, backtest_metrics=None) -> dict:
    rows = ctx.journal()
    week_ago = iso(ctx.now - timedelta(days=7))
    week = [r for r in rows if r["exit_ts"] >= week_ago]
    events = read_jsonl(ctx.paths.events)
    recent = [e for e in events if e.get("ts", "") >= week_ago]
    runs = read_jsonl(ctx.paths.runs)

    def stats(trades):
        rs = [float(r["r_multiple"]) for r in trades if r["r_multiple"] not in ("", None)]
        wins = [t for t in trades if float(t["pnl_usd"]) > 0]
        return {"trades": len(trades),
                "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else None,
                "avg_r": round(statistics.mean(rs), 3) if rs else None,
                "expectancy_r": round(statistics.mean(rs), 3) if rs else None,
                "spread_paid_usd": round(sum(float(t["spread_paid_usd"]) for t in trades), 2),
                "pnl_usd": round(sum(float(t["pnl_usd"]) for t in trades), 2)}

    entries = [e for e in recent if e.get("type") == "entry" and e.get("planned")]
    stops = [e["seconds"] for e in recent if e.get("type") == "stop_live"]
    equities = [(r["ts"], r["equity"]) for r in runs if "equity" in r]
    peak, maxdd = 0.0, 0.0
    for _, e in equities:
        peak = max(peak, e)
        maxdd = min(maxdd, e / peak - 1 if peak else 0.0)
    week_eq = [e for t, e in equities if t >= week_ago]
    out = {"week": stats(week), "since_inception": stats(rows),
           "slippage_vs_plan_pct": round(statistics.mean(
               (e["fill"] / e["planned"] - 1) * 100 for e in entries), 3) if entries else None,
           "stop_confirm_sec_avg": round(statistics.mean(stops), 1) if stops else None,
           "incidents_week": sum(1 for i in read_jsonl(ctx.paths.incidents) if i.get("ts", "") >= week_ago),
           "guard_blocks_week": sum(1 for e in recent if e.get("type") == "guard"
                                    and e.get("decision") == "deny" and not e.get("selftest")),
           "equity_change_week": round(week_eq[-1] - week_eq[0], 2) if len(week_eq) > 1 else 0.0,
           "max_drawdown_pct": round(maxdd * 100, 1),
           "backtest": backtest_metrics}
    live = out["since_inception"]
    if live["trades"] >= DRIFT_MIN_TRADES and (live["expectancy_r"] or 0) < 0:
        ctx.pause(f"DRIFT: live expectancy {live['expectancy_r']}R after {live['trades']} trades")
        ctx.save()
    out["drift_alarm"] = ctx.st["state"] == "PAUSED" and "DRIFT" in ctx.st["reason"]
    return out


def phase0_metrics(ctx) -> dict | None:
    """The Phase 0 backtest numbers on today's data, for the live-versus-backtest comparison."""
    try:
        from backtest import metrics, simulate
        data = {c: ctx.frame(c) for c in ctx.cfg["universe"]}
        if any(v is None for v in data.values()):
            return None
        g, a = ctx.cfg["gate"], ctx.acct
        res = simulate(data, ctx.p, start=g["full_start"], end=None,
                       cost_side=ctx.ex["cost_per_side_assumed"], start_equity=a["starting_capital"],
                       floor=a["floor_equity"], daily_loss_frac=a["daily_loss_limit_frac"],
                       weekly_loss_frac=a["weekly_loss_limit_frac"], gap_frac=a["floor_gap_allowance_frac"])
        m = metrics(res, a["starting_capital"])
        return {k: m[k] for k in ("trades", "trades_per_month", "win_rate_pct", "avg_win_r", "avg_loss_r",
                                  "expectancy_r", "profit_factor", "max_drawdown_pct", "total_spread_cost")}
    except Exception:
        return None
