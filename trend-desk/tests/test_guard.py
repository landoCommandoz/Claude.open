"""BUILD.md section 4.2: the guard."""
import json
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest

import guard
from helpers import ACCT, CANCEL, NOW, PLACE, SERVER, make_root
from state import Paths, iso, read_json, read_jsonl


def approval(kind="BUY", coin="BTC", side="buy", otype="limit", qty=0.01, limit=100.0, stop=None,
             order_id=None, mode="LIVE", ttl=300, used=False, ref=None):
    return {"id": str(uuid.uuid4()), "action_id": "A1", "kind": kind, "coin": coin, "side": side,
            "order_type": otype, "qty": qty, "notional": qty * (limit or stop or 0) if qty else None,
            "limit_price": limit, "stop_price": stop, "order_id": order_id, "ref_id": ref or str(uuid.uuid4()),
            "issued_at": iso(NOW), "expires_at": iso(NOW + timedelta(seconds=ttl)), "used": used,
            "used_at": None, "mode": mode}


def place_input(a, **over):
    args = {"rhs_account_number": ACCT, "symbol": f"{a['coin']}-USD", "side": a["side"],
            "type": {"stop_market": "stop_loss"}.get(a["order_type"], a["order_type"]),
            "quantity": a["qty"], "ref_id": a["ref_id"]}
    if a["limit_price"] is not None:
        args["limit_price"] = a["limit_price"]
    if a["stop_price"] is not None:
        args["stop_price"] = a["stop_price"]
        args["time_in_force"] = "gtc"
    args.update(over)
    return {k: v for k, v in args.items() if v is not None}


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


def run(root, tool, tool_input, approvals=None, now=NOW):
    if approvals is not None:
        Paths(root).approvals.parent.mkdir(parents=True, exist_ok=True)
        (Paths(root).approvals).write_text(json.dumps(approvals))
    return guard.decide_safe({"tool_name": tool, "tool_input": tool_input}, root, now)


def test_unapproved_place_is_denied(root):
    a = approval()
    assert run(root, PLACE, place_input(a), []) == (False, "no matching approval")


def test_matching_approval_is_allowed_and_marked_used_then_reuse_denied(root):
    a = approval()
    allow, _ = run(root, PLACE, place_input(a), [a])
    assert allow
    saved = read_json(Paths(root).approvals)
    assert saved[0]["used"] is True and saved[0]["used_at"] == iso(NOW)
    assert run(root, PLACE, place_input(a)) == (False, "approval already used")


def test_expired_is_denied(root):
    a = approval(ttl=300)
    assert run(root, PLACE, place_input(a), [a], now=NOW + timedelta(seconds=301)) == (False, "approval expired")


def test_quantity_off_by_one_percent_is_denied(root):
    a = approval(qty=0.01)
    assert run(root, PLACE, place_input(a, quantity=0.0101), [a]) == (False, "quantity mismatch")
    assert run(root, PLACE, place_input(a, quantity=0.010005), [a])[0] is True   # inside 0.1%


def test_price_off_by_point_two_percent_is_denied(root):
    a = approval(limit=100.0)
    assert run(root, PLACE, place_input(a, limit_price=100.2), [a]) == (False, "limit price mismatch")
    s = approval(kind="PROTECT", side="sell", otype="stop_market", limit=None, stop=90.0)
    assert run(root, PLACE, place_input(s, stop_price=90.18), [s]) == (False, "stop price mismatch")


def test_unknown_and_forbidden_tools_are_denied(root):
    assert run(root, SERVER + "withdraw_everything", {}, []) == (False, "tool not in map")
    assert run(root, SERVER + "transfer_funds", {}, []) == (False, "forbidden tool")


def test_reads_and_previews_are_allowed(root):
    assert run(root, SERVER + "get_crypto_quotes", {}, [])[0] is True
    assert run(root, SERVER + "preview_crypto_order", {}, [])[0] is True


def test_forced_exception_is_denied(root, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(guard, "decide", boom)
    assert guard.decide_safe({"tool_name": PLACE, "tool_input": {}}, root, NOW) == (False, "guard error")


def test_stop_file_denies_a_buy_and_allows_a_protective_stop(root):
    (root / "STOP").write_text("")
    buy = approval()
    stop = approval(kind="PROTECT", side="sell", otype="stop_market", limit=None, stop=90.0)
    assert run(root, PLACE, place_input(buy), [buy, stop]) == (False, "STOP file blocks buys")
    assert run(root, PLACE, place_input(stop))[0] is True


def test_dry_stamp_is_denied(root):
    a = approval(mode="DRY")
    assert run(root, PLACE, place_input(a), [a]) == (False, "approval stamped DRY")


def test_test_approval_passes_only_for_its_exact_order(root):
    a = approval(kind="TEST", qty=0.00004, limit=50000.0, mode="TEST")
    assert run(root, PLACE, place_input(a, limit_price=50100.0), [a]) == (False, "limit price mismatch")
    assert run(root, PLACE, place_input(a, symbol="ETH-USD"))[0] is False
    assert run(root, PLACE, place_input(a))[0] is True


def test_test_approval_is_issued_only_while_config_mode_is_dry(tmp_path):
    """The guard never reads config mode; the engine refuses to issue TEST approvals once LIVE."""
    import engine
    root = make_root(tmp_path, mode="LIVE")
    assert engine.cmd_test_approval(root, NOW, "BTC", 0.00004, 30000.0).startswith("REFUSED")


def test_stop_without_gtc_is_denied(root):
    s = approval(kind="PROTECT", side="sell", otype="stop_market", limit=None, stop=90.0)
    assert run(root, PLACE, place_input(s, time_in_force="gfd"), [s]) == (False, "stop without time_in_force gtc")
    assert run(root, PLACE, {k: v for k, v in place_input(s).items() if k != "time_in_force"})[0] is False


def test_wrong_ref_id_or_account_is_denied(root):
    a = approval()
    assert run(root, PLACE, place_input(a, ref_id="other"), [a]) == (False, "ref_id mismatch")
    assert run(root, PLACE, place_input(a, rhs_account_number="9ZZ"), [a]) == (False, "wrong account")


def test_cancel_needs_the_exact_order_id(root):
    c = approval(kind="CANCEL", side=None, otype=None, qty=None, limit=None, order_id="ord-1")
    base = {"rhs_account_number": ACCT}
    assert run(root, CANCEL, {**base, "order_id": "ord-2"}, [c])[0] is False
    assert run(root, CANCEL, {**base, "order_id": "ord-1"})[0] is True


def test_hook_process_prints_exact_deny_json_and_logs(tmp_path):
    root = make_root(tmp_path, copy_scripts=True)
    payload = json.dumps({"tool_name": PLACE, "tool_input": {"symbol": "BTC-USD"}})
    out = subprocess.run([sys.executable, str(root / "scripts" / "guard.py")], input=payload,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    decision = json.loads(out.stdout)
    assert decision == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": "wrong account"}}
    events = read_jsonl(Paths(root).events)
    assert events[-1]["type"] == "guard" and events[-1]["decision"] == "deny"
    garbage = subprocess.run([sys.executable, str(root / "scripts" / "guard.py")], input="not json{",
                             capture_output=True, text=True, timeout=60)
    assert garbage.returncode == 0 and json.loads(garbage.stdout)["hookSpecificOutput"][
        "permissionDecisionReason"] == "guard error"


def test_selftest_prints_deny_twice(tmp_path):
    root = make_root(tmp_path, copy_scripts=True)
    out = subprocess.run([sys.executable, str(root / "scripts" / "guard.py"), "--selftest"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.count("DENY ") == 2 and "SELFTEST: PASS" in out.stdout
