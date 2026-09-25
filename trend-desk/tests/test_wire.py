"""Sept 25 Robinhood schema facts that change how the desk sends and parses data.
Raw response mappings still wait for real Phase 1 fixtures."""
import json
from datetime import timedelta

import pytest

import guard
from actions import dec_str
from broker import TapeError, make_agentic_account, make_order, make_quote
from deskconfig import coin_from_symbol
from entries import make_plan
from helpers import ACCT, NOW, PLACE, FakeBroker, breakout, ctx_for, flat, log_guard_reads, make_root, quote
from state import Paths, iso


@pytest.mark.parametrize("raw", ["BTC", "btc", "BTC-USD", "BTC/USD", "BTCUSD", "btcusd"])
def test_every_symbol_shape_means_the_coin(raw):
    assert coin_from_symbol(raw) == "BTC"


def test_doge_and_usd_like_names_survive():
    assert coin_from_symbol("DOGEUSD") == "DOGE" and coin_from_symbol("DOGE-USD") == "DOGE"


def test_decimal_strings_are_cut_to_the_increment_never_rounded_up():
    assert dec_str(0.000734849, 1e-8) == "0.00073484"
    assert dec_str(138.19, 0.1) == "138.1"
    assert dec_str(111.0525, 0.01) == "111.05"
    assert dec_str(0.2439569, 1e-5) == "0.24395"
    assert "e" not in dec_str(0.00000001, 1e-8).lower()


def test_order_args_go_out_as_strings(tmp_path):
    root = make_root(tmp_path)
    log_guard_reads(root)
    q = {c: quote(100.0) for c in ("ETH", "SOL", "DOGE")}
    q["BTC"] = quote(110.0)
    frames = {"BTC": breakout(110.0), "ETH": flat(), "SOL": flat(), "DOGE": flat()}
    plan = make_plan(ctx_for(root, FakeBroker(quotes=q), frames), "DAILY")
    args = plan["actions"][0]["args"]
    for key in ("quantity", "limit_price", "ref_id", "symbol", "side", "type", "rhs_account_number"):
        assert isinstance(args[key], str), key
    assert "dollar_amount" not in args


def test_guard_denies_quantity_and_dollar_amount_together(tmp_path):
    root = make_root(tmp_path)
    ref = "r-1"
    a = {"id": "x", "action_id": "A1", "kind": "BUY", "coin": "BTC", "side": "buy", "order_type": "limit",
         "qty": 0.001, "notional": 100.0, "limit_price": 100000.0, "stop_price": None, "order_id": None,
         "ref_id": ref, "issued_at": iso(NOW), "expires_at": iso(NOW + timedelta(minutes=5)), "used": False,
         "used_at": None, "mode": "LIVE"}
    Paths(root).approvals.write_text(json.dumps([a]))
    base = {"rhs_account_number": ACCT, "symbol": "BTC-USD", "side": "buy", "type": "limit",
            "limit_price": "100000.00", "ref_id": ref}
    both = guard.decide_safe({"tool_name": PLACE, "tool_input": {**base, "quantity": "0.001",
                                                                 "dollar_amount": "100"}}, root, NOW)
    assert both == (False, "both quantity and dollar amount")
    bad = guard.decide_safe({"tool_name": PLACE, "tool_input": {**base, "quantity": "0.001x"}}, root, NOW)
    assert bad == (False, "quantity mismatch")
    assert guard.decide_safe({"tool_name": PLACE, "tool_input": {**base, "quantity": "0.00100000"}},
                             root, NOW)[0] is True


def test_broker_parses_decimal_strings_exactly():
    q = make_quote({"bid": "64123.12345678", "ask": "65337.87654321", "ts": "t"})
    assert q.bid == 64123.12345678 and q.ask == 65337.87654321
    o = make_order({"id": "o", "coin": "BTC", "side": "buy", "type": "limit", "qty": "0.00000001",
                    "filled_qty": "0", "avg_fill_price": None, "limit_price": "1.00000000", "stop_price": None,
                    "status": "open", "created_at": "t", "updated_at": None, "reject_reason": None})
    assert o.qty == 1e-8
    for bad in ("NaN", "Infinity", True, [1]):
        with pytest.raises(TapeError):
            make_quote({"bid": bad, "ask": "1", "ts": "t"})


def test_agentic_account_needs_both_account_numbers():
    a = make_agentic_account({"account_number": "A1", "rhs_account_number": "R1", "agentic_allowed": True})
    assert (a.account_number, a.rhs_account_number) == ("A1", "R1")
    with pytest.raises(TapeError):
        make_agentic_account({"rhs_account_number": "R1", "agentic_allowed": True})


def test_get_portfolio_is_called_with_account_number(tmp_path, capsys):
    import engine
    root = make_root(tmp_path, account=None)
    assert engine.preflight(root, NOW, "DAILY", skip_selftests=True, broker=FakeBroker()) == "READY"
    call = next(l for l in capsys.readouterr().out.splitlines() if "get_portfolio" in l)
    assert json.loads(call.split(" ", 3)[3]) == {"account_number": "5QA00001-M"}
