"""BUILD.md section 4.6: engine confirm, on broker's normalized objects."""
import json
from datetime import timedelta

import pytest

from confirmer import confirm
from entries import make_plan
from helpers import (NOW, SESSION, FakeBroker, breakout, ctx_for, flat, log_guard_reads, make_root, order,
                     quote)
from state import Paths, load_state, read_json, save_state
from strategy import initial_stop, round_down

COINS = ("BTC", "ETH", "SOL", "DOGE")


@pytest.fixture
def root(tmp_path):
    r = make_root(tmp_path)
    log_guard_reads(r)
    return r


def frames(**over):
    return {**{c: flat() for c in COINS}, **over}


def quotes(**mids):
    return {c: quote(mids.get(c, 100.0)) for c in COINS}


def step(root, broker, fr, action_id, now):
    ctx = ctx_for(root, broker, fr, now=now)
    return ctx, confirm(ctx, action_id)


def new_action(line):
    assert line.startswith("NEW ACTION "), line
    return json.loads(line[len("NEW ACTION "):])


def buy_plan(root, broker, fr):
    ctx = ctx_for(root, broker, fr)
    plan = make_plan(ctx, "DAILY")
    return ctx, plan["actions"][0]


def test_filled_buy_gives_protect_for_the_exact_filled_quantity(root):
    b = FakeBroker(quotes=quotes(BTC=110.0))
    fr = frames(BTC=breakout(110.0))
    ctx, buy = buy_plan(root, b, fr)
    qty = float(buy["args"]["quantity"])
    b.placed[buy["args"]["ref_id"]] = order("b1", side="buy", type_="limit", qty=qty, filled=qty,
                                            avg=110.3, limit=float(buy["args"]["limit_price"]), status="filled",
                                            updated="2026-10-02T00:07:30Z")
    ctx2, line = step(root, b, fr, buy["id"], NOW + timedelta(seconds=30))
    p = new_action(line)
    n = float(ctx.row("BTC", SESSION)["n"])
    assert p["kind"] == "PROTECT" and float(p["args"]["quantity"]) == qty
    assert float(p["args"]["stop_price"]) == round_down(initial_stop(110.3, n, ctx.p), 0.01)
    pos = ctx2.st["positions"]["BTC"]
    assert pos["qty"] == qty and pos["entry_fill"] == 110.3


def test_open_buy_inside_the_timeout_waits(root):
    b = FakeBroker(quotes=quotes(BTC=110.0))
    fr = frames(BTC=breakout(110.0))
    _, buy = buy_plan(root, b, fr)
    b.placed[buy["args"]["ref_id"]] = order("b1", side="buy", type_="limit", qty=0.1, status="open")
    assert step(root, b, fr, buy["id"], NOW + timedelta(seconds=30))[1] == "WAIT 20"


def test_partial_fill_past_the_timeout_gives_cancel_then_protect(root):
    b = FakeBroker(quotes=quotes(BTC=110.0))
    fr = frames(BTC=breakout(110.0))
    _, buy = buy_plan(root, b, fr)
    part = order("b1", side="buy", type_="limit", qty=0.2, filled=0.05, avg=110.2, status="partially_filled")
    b.placed[buy["args"]["ref_id"]] = part
    later = NOW + timedelta(seconds=130)
    _, line = step(root, b, fr, buy["id"], later)
    cancel = new_action(line)
    assert cancel["kind"] == "CANCEL" and cancel["args"]["order_id"] == "b1"
    b.statuses["b1"] = order("b1", side="buy", type_="limit", qty=0.2, filled=0.06, avg=110.2, status="canceled")
    ctx, line = step(root, b, fr, cancel["id"], later + timedelta(seconds=10))
    protect = new_action(line)
    assert protect["kind"] == "PROTECT" and float(protect["args"]["quantity"]) == 0.06
    assert ctx.st["positions"]["BTC"]["qty"] == 0.06


def protect_setup(root):
    paths = Paths(root)
    st = load_state(paths, json.loads(paths.config.read_text()))
    st["positions"]["SOL"] = {"qty": 0.5, "entry_fill": 100.0, "entry_ts": "2026-09-20T00:08:00Z",
                              "n_at_entry": 4.0, "initial_stop": 92.0, "stop": 92.0, "stop_order_id": None,
                              "entry_order_id": "b1"}
    save_state(paths, st)
    b = FakeBroker(quotes=quotes(), held={"SOL": 0.5})
    plan = make_plan(ctx_for(root, b, frames()), "DAILY")
    return b, plan["actions"][0]


def test_stop_rejected_twice_gives_exit_and_paused(root):
    b, protect = protect_setup(root)
    assert protect["kind"] == "PROTECT"
    rejected = order("p1", coin="SOL", qty=0.5, stop=92.0, status="rejected", reason="bad price")
    b.placed[protect["args"]["ref_id"]] = rejected
    ctx, line = step(root, b, frames(), protect["id"], NOW + timedelta(seconds=5))
    assert line == "RETRY" and ctx.st["state"] != "PAUSED"
    approvals = read_json(Paths(root).approvals)
    same_ref = [a for a in approvals if a["ref_id"] == protect["args"]["ref_id"]]
    assert len(same_ref) == 2 and not same_ref[-1]["used"]          # fresh approval, SAME ref_id
    ctx, line = step(root, b, frames(), protect["id"], NOW + timedelta(seconds=15))
    exit_ = new_action(line)
    assert exit_["kind"] == "EXIT" and exit_["args"]["side"] == "sell" and float(exit_["args"]["quantity"]) == 0.5
    assert ctx.st["state"] == "PAUSED" and "stop would not place" in ctx.st["reason"]


def test_live_matching_stop_confirms_and_records_the_order(root):
    b, protect = protect_setup(root)
    b.placed[protect["args"]["ref_id"]] = order("p1", coin="SOL", qty=0.5, stop=92.0, status="open")
    ctx, line = step(root, b, frames(), protect["id"], NOW + timedelta(seconds=5))
    assert line == "NEXT" and ctx.st["positions"]["SOL"]["stop_order_id"] == "p1"


def test_failed_ratchet_puts_the_old_stop_back_then_exits_if_that_fails(root):
    paths = Paths(root)
    st = load_state(paths, json.loads(paths.config.read_text()))
    st["positions"]["BTC"] = {"qty": 0.1, "entry_fill": 100.0, "entry_ts": "2026-09-20T00:08:00Z",
                              "n_at_entry": 4.0, "initial_stop": 90.0, "stop": 90.0, "stop_order_id": "s1",
                              "entry_order_id": "b1", "stop_placed_at": "2026-09-20T00:09:00Z"}
    st["known_orders"] = {"b1": {"coin": "BTC", "kind": "BUY", "ts": "2026-09-20T00:08:00Z"}}
    save_state(paths, st)
    from helpers import candles
    fr = frames(BTC=candles([100.0 + 0.5 * i for i in range(80)]))
    b = FakeBroker(quotes=quotes(BTC=139.5), held={"BTC": 0.1}, orders=[order("s1", qty=0.1, stop=90.0)])
    plan = make_plan(ctx_for(root, b, fr), "DAILY")
    rc = plan["actions"][0]
    assert rc["kind"] == "RATCHET_CANCEL"
    t = NOW + timedelta(seconds=5)
    b.statuses["s1"] = order("s1", qty=0.1, stop=90.0, status="canceled")
    place = new_action(step(root, b, fr, rc["id"], t)[1])
    assert place["kind"] == "RATCHET_PLACE" and float(place["args"]["stop_price"]) == rc["new_stop"]
    b.placed[place["args"]["ref_id"]] = order("r1", qty=0.1, stop=rc["new_stop"], status="rejected")
    assert step(root, b, fr, place["id"], t)[1] == "RETRY"
    ctx, line = step(root, b, fr, place["id"], t)
    back = new_action(line)
    assert back["kind"] == "PROTECT" and float(back["args"]["stop_price"]) == 90.0     # the old stop goes back
    assert ctx.st["positions"]["BTC"]["stop"] == 90.0
    b.placed[back["args"]["ref_id"]] = order("r2", qty=0.1, stop=90.0, status="rejected")
    assert step(root, b, fr, back["id"], t)[1] == "RETRY"
    ctx, line = step(root, b, fr, back["id"], t)
    assert new_action(line)["kind"] == "EXIT" and ctx.st["state"] == "PAUSED"


def test_exit_filled_writes_the_journal_and_checks_slip(root):
    b, protect = protect_setup(root)
    b.placed[protect["args"]["ref_id"]] = order("p1", coin="SOL", qty=0.5, stop=92.0, status="rejected")
    step(root, b, frames(), protect["id"], NOW)
    exit_ = new_action(step(root, b, frames(), protect["id"], NOW)[1])
    b.placed[exit_["args"]["ref_id"]] = order("e1", coin="SOL", type_="limit", qty=0.5, filled=0.5, avg=99.4,
                                              limit=float(exit_["args"]["limit_price"]), status="filled",
                                              updated="2026-10-02T00:08:00Z")
    ctx, line = step(root, b, frames(), exit_["id"], NOW + timedelta(seconds=20))
    assert line == "NEXT" and "SOL" not in ctx.st["positions"]
    assert ctx.journal()[-1]["exit_reason"] == "emergency"


def test_unknown_status_three_times_stops(root):
    b = FakeBroker(quotes=quotes(BTC=110.0))
    fr = frames(BTC=breakout(110.0))
    _, buy = buy_plan(root, b, fr)
    lines = [step(root, b, fr, buy["id"], NOW + timedelta(seconds=i))[1] for i in range(3)]
    assert lines == ["WAIT 10", "WAIT 10", "STOP"]


def test_last_exit_attempt_unfilled_puts_the_stop_back_before_stopping(root):
    b, protect = protect_setup(root)
    b.placed[protect["args"]["ref_id"]] = order("p1", coin="SOL", qty=0.5, stop=92.0, status="rejected")
    step(root, b, frames(), protect["id"], NOW)
    exit_ = new_action(step(root, b, frames(), protect["id"], NOW)[1])
    plan_path = Paths(root).plans / "latest.json"
    plan = json.loads(plan_path.read_text())
    for a in plan["actions"]:
        if a["id"] == exit_["id"]:
            a["attempt"] = 4                                    # this was the last attempt (3 reprices + 1)
    plan_path.write_text(json.dumps(plan))
    b.placed[exit_["args"]["ref_id"]] = order("e9", coin="SOL", type_="limit", qty=0.5, limit=90.0, status="open")
    later = NOW + timedelta(seconds=200)
    cancel = new_action(step(root, b, frames(), exit_["id"], later)[1])
    assert cancel["kind"] == "CANCEL" and cancel["args"]["order_id"] == "e9"
    b.statuses["e9"] = order("e9", coin="SOL", type_="limit", qty=0.5, limit=90.0, status="canceled")
    back = new_action(step(root, b, frames(), cancel["id"], later)[1])
    assert back["kind"] == "PROTECT" and float(back["args"]["stop_price"]) == 92.0
    assert float(back["args"]["quantity"]) == 0.5
    b.placed[back["args"]["ref_id"]] = order("p9", coin="SOL", qty=0.5, stop=92.0, status="open")
    ctx, line = step(root, b, frames(), back["id"], later)
    assert line == "STOP" and ctx.st["positions"]["SOL"]["stop_order_id"] == "p9"


def test_put_back_stop_rejected_twice_pauses_instead_of_looping(root):
    b, protect = protect_setup(root)
    plan_path = Paths(root).plans / "latest.json"
    plan = json.loads(plan_path.read_text())
    plan["actions"][0]["stop_run"] = True
    plan_path.write_text(json.dumps(plan))
    b.placed[protect["args"]["ref_id"]] = order("p1", coin="SOL", qty=0.5, stop=92.0, status="rejected")
    assert step(root, b, frames(), protect["id"], NOW)[1] == "RETRY"
    ctx, line = step(root, b, frames(), protect["id"], NOW)
    assert line == "STOP" and ctx.st["state"] == "PAUSED" and "has no stop" in ctx.st["reason"]
