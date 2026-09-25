"""BUILD.md section 4.3 (capture) and 4.4 (broker, normalized layer).

Real Robinhood response fixtures arrive in Phase 1. Until then the broker is tested on its
normalized objects, its errors, and a SYNTHETIC response shape registered only inside these
tests. The shipped PARSERS stay unmapped, so production code cannot parse a guessed shape.
"""
import json
import subprocess
import sys
from datetime import timedelta

import pytest

import broker
import capture
from broker import Broker, StaleTape, TapeError, make_account, make_order, make_quote
from helpers import ACCT, NOW, ORDERS, PLACE, SERVER, TOOLS, make_root
from state import Paths, read_jsonl

GOOD_ORDER = {"id": "o1", "coin": "btc", "side": "sell", "type": "stop_market", "qty": "0.1",
              "filled_qty": "0", "avg_fill_price": None, "limit_price": None, "stop_price": "90.5",
              "status": "open", "created_at": "2026-10-01T00:08:00Z", "updated_at": None,
              "reject_reason": None}


def hook(root, tool, response, tool_input=None, now=NOW):
    capture.capture(json.dumps({"tool_name": tool, "tool_input": tool_input or {},
                                "tool_response": response, "session_id": "x"}), root, now)


# ---------- capture ----------

def test_capture_writes_tape_latest_keys_and_event(tmp_path):
    root = make_root(tmp_path)
    hook(root, SERVER + "get_crypto_quotes", {"anything": 1}, {"symbols": ["BTC-USD"]})
    tape = Paths(root).tape
    stamped = [p for p in tape.glob("2*_get_crypto_quotes.json")]
    latest = json.loads((tape / "latest_get_crypto_quotes.json").read_text())
    assert len(stamped) == 1 and latest["tool_response"] == {"anything": 1}
    assert latest["tool_input"] == {"symbols": ["BTC-USD"]} and latest["ts"] == "2026-10-02T00:07:00Z"
    assert json.loads((tape / "_keys.json").read_text()) == ["session_id", "tool_input", "tool_name",
                                                             "tool_response"]
    assert read_jsonl(Paths(root).events)[-1]["type"] == "tape"


@pytest.mark.parametrize("stdin", ["", "garbage{", "[1,2]", '{"no_tool_name": true}'])
def test_capture_process_survives_garbage_and_never_prints(tmp_path, stdin):
    root = make_root(tmp_path, copy_scripts=True)
    out = subprocess.run([sys.executable, str(root / "scripts" / "capture.py")], input=stdin,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout == ""
    assert (root / "state" / "capture_errors.log").exists()


def test_capture_process_records_a_real_call(tmp_path):
    root = make_root(tmp_path, copy_scripts=True)
    payload = json.dumps({"tool_name": ORDERS, "tool_input": {}, "tool_response": [{"x": 1}]})
    out = subprocess.run([sys.executable, str(root / "scripts" / "capture.py")], input=payload,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout == ""
    assert (root / "state" / "tape" / "latest_get_crypto_orders.json").exists()


# ---------- broker: normalized objects ----------

def test_normalized_objects_parse():
    o = make_order(GOOD_ORDER)
    assert (o.coin, o.qty, o.stop_price, o.is_stop, o.is_live) == ("BTC", 0.1, 90.5, True, True)
    assert make_account({"equity": "400.1", "cash": 350, "buying_power": 350, "ts": "t"}).equity == 400.1
    assert make_quote({"bid": 99, "ask": 101, "ts": "t"}).mid == 100


@pytest.mark.parametrize("drop", sorted(GOOD_ORDER))
def test_a_missing_order_field_raises(drop):
    with pytest.raises(TapeError):
        make_order({k: v for k, v in GOOD_ORDER.items() if k != drop})


@pytest.mark.parametrize("bad", [{"qty": "abc"}, {"side": "short"}, {"status": "weird"},
                                 {"filled_qty": "0.1", "avg_fill_price": None}, {"qty": "-1"}])
def test_an_unreadable_order_field_raises(bad):
    with pytest.raises(TapeError):
        make_order({**GOOD_ORDER, **bad})


def test_missing_account_and_quote_fields_raise():
    with pytest.raises(TapeError):
        make_account({"equity": 1, "cash": 1, "ts": "t"})
    with pytest.raises(TapeError):
        make_quote({"bid": 101, "ask": 99, "ts": "t"})


def test_unmapped_raw_responses_fail_closed():
    for role in broker.PARSERS:
        with pytest.raises(TapeError, match="Phase 1"):
            broker.parse(role, {"looks": "plausible"})


# ---------- broker: tape pipeline with a synthetic shape ----------

@pytest.fixture
def synthetic(monkeypatch):
    monkeypatch.setitem(broker.PARSERS, "orders", lambda r: [make_order(x) for x in r["orders"]])
    monkeypatch.setitem(broker.PARSERS, "place", lambda r: make_order(r["order"]))
    monkeypatch.setitem(broker.PARSERS, "quotes", lambda r: {k: make_quote(v) for k, v in r.items()})


def test_stale_and_missing_tape_raise(tmp_path, synthetic):
    root = make_root(tmp_path)
    b = Broker(root, TOOLS, NOW)
    with pytest.raises(StaleTape, match="not on the tape"):
        b.recent_orders(180)
    hook(root, ORDERS, {"orders": [GOOD_ORDER]}, now=NOW - timedelta(seconds=181))
    with pytest.raises(StaleTape, match="181s old"):
        b.recent_orders(180)
    hook(root, ORDERS, {"orders": [GOOD_ORDER]}, now=NOW - timedelta(seconds=10))
    assert [o.id for o in b.open_orders(180)] == ["o1"]


def test_tape_with_a_missing_field_raises(tmp_path, synthetic):
    root = make_root(tmp_path)
    broken = {k: v for k, v in GOOD_ORDER.items() if k != "status"}
    hook(root, ORDERS, {"orders": [broken]})
    with pytest.raises(TapeError, match="missing field status"):
        Broker(root, TOOLS, NOW).recent_orders(180)


def test_order_files_and_placements_by_ref_id(tmp_path, synthetic):
    root = make_root(tmp_path)
    placed = {**GOOD_ORDER, "id": "o9"}
    hook(root, PLACE, {"order": placed}, {"ref_id": "ref-9", "rhs_account_number": ACCT, "symbol": "BTC-USD"},
         now=NOW - timedelta(seconds=30))
    filled = {**placed, "status": "filled", "filled_qty": "0.1", "avg_fill_price": "90.4"}
    hook(root, ORDERS, {"orders": [filled]}, now=NOW - timedelta(seconds=5))
    b = Broker(root, TOOLS, NOW)
    assert b.placement("ref-9", NOW - timedelta(minutes=5)).id == "o9"
    assert b.order("o9", NOW - timedelta(seconds=20)).status == "filled"
    assert b.order("o9", NOW) is None                      # nothing recorded after that time
