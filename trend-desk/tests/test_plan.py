"""BUILD.md section 4.5: engine plan scenarios, on broker's normalized objects."""
import json

import pytest

from entries import make_plan
from helpers import (NOW, SESSION, FakeBroker, breakout, candles, ctx_for, flat, log_guard_reads,
                     make_root, order, quote)
from state import Paths, append_journal, load_state, read_json, save_state
from strategy import initial_stop, ratchet_stop, round_down, size_position, worst_case_loss

COINS = ("BTC", "ETH", "SOL", "DOGE")


def frames(**over):
    out = {c: flat() for c in COINS}
    out.update(over)
    return out


def quotes(**mids):
    return {c: quote(mids.get(c, 100.0)) for c in COINS}


@pytest.fixture
def root(tmp_path):
    r = make_root(tmp_path)
    log_guard_reads(r)
    return r


def add_position(root, coin, qty, entry, stop, stop_id="s1", **extra):
    paths = Paths(root)
    cfg = json.loads(paths.config.read_text())
    st = load_state(paths, cfg)
    st["positions"][coin] = {"qty": qty, "entry_fill": entry, "entry_ts": "2026-09-20T00:08:00Z",
                             "n_at_entry": 5.0, "initial_stop": stop, "stop": stop, "stop_order_id": stop_id,
                             "entry_order_id": "b1", "stop_placed_at": "2026-09-20T00:09:00Z", **extra}
    st["known_orders"] = {"b1": {"coin": coin, "kind": "BUY", "ts": "2026-09-20T00:08:00Z"}}
    save_state(paths, st)


def plan_for(root, broker, fr, **kw):
    ctx = ctx_for(root, broker, fr, **kw)
    return ctx, make_plan(ctx, "DAILY")


def kinds(plan):
    return [a["kind"] for a in plan["actions"]]


def test_no_signal_gives_no_actions(root):
    ctx, plan = plan_for(root, FakeBroker(quotes=quotes()), frames())
    assert plan["actions"] == [] and plan["state"] == "SCANNING"


def test_clean_signal_gives_one_buy_with_exact_quantity_and_limit(root):
    b = FakeBroker(quotes=quotes(BTC=110.0))
    ctx, plan = plan_for(root, b, frames(BTC=breakout(110.0)))
    assert kinds(plan) == ["BUY"]
    a = plan["actions"][0]
    n = float(ctx.row("BTC", SESSION)["n"])
    limit = round_down(b.quotes_["BTC"].ask * 1.005, 0.01)
    stop = initial_stop(limit, n, ctx.p)
    s = size_position(equity=400, peak=400, cash=400, fill=limit, stop=stop, open_risk=0.0,
                      open_positions=0, p=ctx.p, floor=320, gap_frac=0.05, open_worst_case=0.0)
    assert float(a["args"]["quantity"]) == round_down(s.qty, 1e-8) and float(a["args"]["limit_price"]) == limit
    assert a["args"]["rhs_account_number"] == "5QA00001" and a["args"]["symbol"] == "BTC-USD"
    approvals = read_json(Paths(root).approvals)
    assert len(approvals) == 1 and approvals[0]["mode"] == "LIVE" and approvals[0]["ref_id"] == a["args"]["ref_id"]


def test_wide_spread_skips_with_the_reason(root):
    q = quotes()
    q["BTC"] = quote(110.0, spread=0.03)
    ctx, plan = plan_for(root, FakeBroker(quotes=q), frames(BTC=breakout(110.0)))
    assert plan["actions"] == []
    assert plan["skipped"] == [{"coin": "BTC", "reason": "spread 3.00% above 2.50%"}]


def test_near_the_floor_the_worst_case_floor_sizes_down_or_skips(root, tmp_path):
    add_position(root, "SOL", 0.5, 100.0, 99.0)
    live = [order("s1", coin="SOL", qty=0.5, stop=99.0)]
    b = FakeBroker(equity=327.0, cash=300.0, quotes=quotes(BTC=110.0), held={"SOL": 0.5}, orders=live)
    st = load_state(Paths(root), json.loads(Paths(root).config.read_text()))
    st["peak_equity"] = 327.0
    save_state(Paths(root), st)
    ctx, plan = plan_for(root, b, frames(BTC=breakout(110.0)))
    n = float(ctx.row("BTC", SESSION)["n"])
    limit = round_down(b.quotes_["BTC"].ask * 1.005, 0.01)
    stop = initial_stop(limit, n, ctx.p)
    worst = worst_case_loss(0.5, b.quotes_["SOL"].bid, 99.0, 0.05)
    common = dict(equity=327.0, peak=327.0, cash=300.0, fill=limit, stop=stop, open_risk=0.5,
                  open_positions=1, p=ctx.p, gap_frac=0.05, open_worst_case=worst)
    floored, free = size_position(floor=320, **common), size_position(floor=0.0, **common)
    assert floored.ok and floored.notional < free.notional       # the floor is what binds here
    assert kinds(plan) == ["BUY"] and float(plan["actions"][0]["args"]["quantity"]) == round_down(floored.qty, 1e-8)

    root2 = make_root(tmp_path / "b")
    log_guard_reads(root2)
    add_position(root2, "SOL", 0.5, 100.0, 99.0)
    b2 = FakeBroker(equity=322.0, cash=300.0, quotes=quotes(BTC=110.0), held={"SOL": 0.5}, orders=live)
    _, plan2 = plan_for(root2, b2, frames(BTC=breakout(110.0)))
    assert plan2["actions"] == [] and {"coin": "BTC", "reason": "floor buffer reached"} in plan2["skipped"]


def test_position_missing_its_stop_gives_protect_first(root):
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    b = FakeBroker(quotes=quotes(BTC=110.0), held={"SOL": 0.5})
    _, plan = plan_for(root, b, frames(BTC=breakout(110.0)))
    assert kinds(plan) == ["PROTECT", "BUY"]
    p = plan["actions"][0]
    assert float(p["args"]["stop_price"]) == 92.0 and float(p["args"]["quantity"]) == 0.5
    assert p["args"]["time_in_force"] == "gtc" and p["args"]["type"] == "stop_loss"


def test_position_gone_with_a_stop_fill_records_the_exit_and_no_actions(root):
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    fill = order("s1", coin="SOL", qty=0.5, filled=0.5, avg=91.8, stop=92.0, status="filled",
                 updated="2026-10-01T13:00:00Z")
    ctx, plan = plan_for(root, FakeBroker(quotes=quotes(), orders=[fill]), frames())
    assert plan["actions"] == [] and ctx.st["positions"] == {} and ctx.st["state"] != "PAUSED"
    row = ctx.journal()[-1]
    assert row["coin"] == "SOL" and row["exit_reason"] == "stop" and float(row["pnl_usd"]) == pytest.approx(-4.1)
    assert ctx.st["exited"]["SOL"] == "2026-10-01"


def test_a_stop_that_filled_six_percent_below_its_price_sets_paused(root):
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    fill = order("s1", coin="SOL", qty=0.5, filled=0.5, avg=92.0 * 0.94, stop=92.0, status="filled",
                 updated="2026-10-01T13:00:00Z")
    ctx, plan = plan_for(root, FakeBroker(quotes=quotes(), orders=[fill]), frames())
    assert ctx.st["state"] == "PAUSED" and "stop slipped past allowance" in ctx.st["reason"]


def test_unknown_filled_order_sets_paused_and_still_protects(root):
    stray = order("x1", coin="ETH", side="buy", type_="limit", qty=0.5, filled=0.5, avg=100.0,
                  limit=100.0, status="filled", created="2026-10-01T15:00:00Z")
    b = FakeBroker(quotes=quotes(), held={"ETH": 0.5}, orders=[stray])
    ctx, plan = plan_for(root, b, frames())
    assert ctx.st["state"] == "PAUSED" and "unknown position" in ctx.st["reason"]
    assert "order not placed by desk" in ctx.st["reason"]
    assert kinds(plan) == ["PROTECT"] and plan["actions"][0]["coin"] == "ETH"


def test_equity_at_the_floor_halts_no_entries_protection_still_planned(root):
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    b = FakeBroker(equity=320.0, cash=270.0, quotes=quotes(BTC=110.0), held={"SOL": 0.5})
    ctx, plan = plan_for(root, b, frames(BTC=breakout(110.0)))
    assert ctx.st["state"] == "HALTED" and plan["state"] == "HALTED"
    assert kinds(plan) == ["PROTECT"]


def test_daily_loss_limit_sets_cooldown_day_and_no_entries(root):
    paths = Paths(root)
    append_journal(paths, {"trade_id": "t", "coin": "ETH", "entry_ts": "2026-09-28T00:08:00Z",
                           "entry_fill": 100, "qty": 1, "initial_stop": 90, "exit_ts": "2026-10-01T09:00:00Z",
                           "exit_fill": 87, "exit_reason": "stop", "pnl_usd": -13.0, "r_multiple": -1.3,
                           "spread_paid_usd": 1.8, "days_held": 3})
    st = load_state(paths, json.loads(paths.config.read_text()))
    st["starts"] = {"2026-10-01": 400.0}
    save_state(paths, st)
    _, plan = plan_for(root, FakeBroker(equity=387.0, cash=387.0, quotes=quotes(BTC=110.0)),
                       frames(BTC=breakout(110.0)))
    assert plan["actions"] == [] and plan["state"] == "COOLDOWN_DAY"
    assert any("daily brake" in n for n in plan["notes"])


def rising():
    return candles([100.0 + 0.5 * i for i in range(80)])


def test_ratchet_above_quarter_n_gives_cancel_then_place_below_gives_nothing(root, tmp_path):
    add_position(root, "BTC", 0.1, 100.0, 90.0)
    b = FakeBroker(quotes=quotes(BTC=139.5), held={"BTC": 0.1}, orders=[order("s1", qty=0.1, stop=90.0)])
    ctx, plan = plan_for(root, b, frames(BTC=rising()))
    row = ctx.row("BTC", SESSION)
    new = round_down(ratchet_stop(90.0, row["exit_level"]), 0.01)
    assert kinds(plan) == ["RATCHET_CANCEL"]
    assert plan["actions"][0]["new_stop"] == new and plan["actions"][0]["args"]["order_id"] == "s1"

    root2 = make_root(tmp_path / "b")
    log_guard_reads(root2)
    near = round(float(row["exit_level"]) - 0.1 * float(row["n"]), 2)
    add_position(root2, "BTC", 0.1, 100.0, near)
    b2 = FakeBroker(quotes=quotes(BTC=139.5), held={"BTC": 0.1}, orders=[order("s1", qty=0.1, stop=near)])
    _, plan2 = plan_for(root2, b2, frames(BTC=rising()))
    assert plan2["actions"] == []


def test_bid_below_the_new_trail_level_gives_exit(root):
    add_position(root, "BTC", 0.1, 100.0, 90.0)
    b = FakeBroker(quotes=quotes(BTC=130.0), held={"BTC": 0.1}, orders=[order("s1", qty=0.1, stop=90.0)])
    _, plan = plan_for(root, b, frames(BTC=rising()))
    assert kinds(plan) == ["CANCEL"]
    then = plan["actions"][0]["then"]
    assert then["kind"] == "EXIT" and then["exit_reason"] == "trail" and then["order"]["side"] == "sell"


def test_stale_tape_gives_no_actions(root):
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    _, plan = plan_for(root, FakeBroker(stale=True, quotes=quotes(BTC=110.0)), frames(BTC=breakout(110.0)))
    assert plan["actions"] == [] and "CALL READ TOOLS FIRST" in plan["notes"]


def test_a_stale_coin_gets_no_entry_while_exits_still_work(root):
    from state import atomic_write_json
    atomic_write_json(Paths(root).data / "status.json",
                      {"coins": {"BTC": {"status": "STALE"}, "ETH": {"status": "OK"},
                                 "SOL": {"status": "OK"}, "DOGE": {"status": "OK"}}})
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    q = quotes(BTC=110.0, SOL=91.0)
    _, plan = plan_for(root, FakeBroker(quotes=q, held={"SOL": 0.5}), frames(BTC=breakout(110.0)))
    assert kinds(plan) == ["EXIT"] and plan["actions"][0]["coin"] == "SOL"
    assert {"coin": "BTC", "reason": "data STALE"} in plan["skipped"]


def test_stop_file_blocks_entries_while_protection_still_works(root):
    (root / "STOP").write_text("")
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    _, plan = plan_for(root, FakeBroker(quotes=quotes(BTC=110.0), held={"SOL": 0.5}),
                       frames(BTC=breakout(110.0)))
    assert kinds(plan) == ["PROTECT"] and any("STOP file" in n for n in plan["notes"])


def test_two_signals_never_exceed_cash_open_risk_or_max_positions(root):
    fr = {"BTC": breakout(110.0), "ETH": breakout(112.0), "SOL": breakout(115.0), "DOGE": breakout(111.0)}
    b = FakeBroker(equity=400.0, cash=150.0, quotes=quotes(BTC=110.0, ETH=112.0, SOL=115.0, DOGE=111.0))
    ctx, plan = plan_for(root, b, fr)
    buys = [a for a in plan["actions"] if a["kind"] == "BUY"]
    assert 2 <= len(buys) <= ctx.p.max_positions
    notional = sum(float(a["args"]["quantity"]) * float(a["args"]["limit_price"]) for a in buys)
    risk = sum(float(a["args"]["quantity"]) * (float(a["args"]["limit_price"]) - a["planned_stop"]) for a in buys)
    assert notional <= 150.0 - ctx.p.cash_buffer_frac * 400.0 + 1e-9
    assert risk <= ctx.p.max_open_risk_frac * 400.0 + 1e-9
    assert all(float(a["args"]["quantity"]) * float(a["args"]["limit_price"]) <= 0.3 * 400 + 1e-9 for a in buys)


def test_dry_mode_turns_orders_into_previews_and_writes_no_approvals(tmp_path):
    root = make_root(tmp_path, mode="DRY")
    log_guard_reads(root)
    _, plan = plan_for(root, FakeBroker(quotes=quotes(BTC=110.0)), frames(BTC=breakout(110.0)))
    assert kinds(plan) == ["PREVIEW"] and plan["actions"][0]["tool"].endswith("preview_crypto_order")
    assert read_json(Paths(root).approvals) is None and plan["state"] == "DRY"


def test_missing_guard_decisions_pause_with_no_orders(tmp_path):
    root = make_root(tmp_path)                 # no guard events logged: the hook is not running
    add_position(root, "SOL", 0.5, 100.0, 92.0)
    ctx, plan = plan_for(root, FakeBroker(quotes=quotes(), held={"SOL": 0.5}), frames())
    assert plan["actions"] == [] and "guard hook not running" in ctx.st["reason"]
