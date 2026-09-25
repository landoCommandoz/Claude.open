"""
guard.py | PreToolUse hook: nothing trades without an approval.

read, preview      allowed (no output, normal permissions apply)
place, cancel      allowed only with a matching, unused, unexpired approval stamped LIVE or TEST
forbidden          always denied
not in the map     always denied

The guard never reads the mode in config. It trusts only the stamp the engine wrote on
the approval. A deny is printed as the PreToolUse JSON decision (never exit code 2).
Any exception anywhere: deny, reason "guard error".
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

CANCEL_KINDS = ("CANCEL", "RATCHET_CANCEL", "TEST_CANCEL")
BUY_KINDS = ("BUY", "TEST")
GOOD_STAMPS = ("LIVE", "TEST")
STOP_TYPES = ("stop_market", "stop_limit")
QTY_TOL = Decimal("0.001")      # 0.1 percent
PRICE_TOL = Decimal("0.0005")    # 0.05 percent


def deny_json(reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}},
                      separators=(",", ":"))


def _dec(x) -> Decimal | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _close(value, target, tol) -> bool:
    v, t = _dec(value), _dec(target)
    return v is not None and t is not None and t > 0 and abs(v - t) <= tol * t


def _price_ok(approved, sent) -> bool:
    if approved is None:
        return sent is None
    return sent is not None and _close(sent, approved, PRICE_TOL)


def match(a: dict, klass: str, args: dict, takes_ref: bool, now: datetime,
          stop_file: bool) -> tuple[bool, str, bool]:
    """(matched, reason, about_this_order). about_this_order marks a specific failure reason."""
    from state import parse_ts
    kind = a.get("kind")
    if klass == "cancel":
        if kind not in CANCEL_KINDS:
            return False, "no matching approval", False
        if str(args.get("order_id")) != str(a.get("order_id")):
            return False, "order id mismatch", False
    else:
        if kind in CANCEL_KINDS:
            return False, "no matching approval", False
        if (args.get("coin"), args.get("side"), args.get("order_type")) != \
                (a.get("coin"), a.get("side"), a.get("order_type")):
            return False, "no matching approval", False
    if a.get("used"):
        return False, "approval already used", True
    if a.get("mode") not in GOOD_STAMPS:
        return False, f"approval stamped {a.get('mode')}", True
    if parse_ts(a["expires_at"]) <= now:
        return False, "approval expired", True
    if takes_ref and str(args.get("ref_id") or "") != str(a.get("ref_id") or "-"):
        return False, "ref_id mismatch", True
    if klass == "cancel":
        return True, "ok", True
    if stop_file and kind in BUY_KINDS:
        return False, "STOP file blocks buys", True
    if "qty" in args and "notional" in args:
        return False, "both quantity and dollar amount", True
    if "qty" in args:
        if not _close(args["qty"], a.get("qty"), QTY_TOL):
            return False, "quantity mismatch", True
    elif "notional" in args:
        if not _close(args["notional"], a.get("notional"), QTY_TOL):
            return False, "notional mismatch", True
    else:
        return False, "no quantity", True
    if not _price_ok(a.get("limit_price"), args.get("limit_price")):
        return False, "limit price mismatch", True
    if not _price_ok(a.get("stop_price"), args.get("stop_price")):
        return False, "stop price mismatch", True
    if a.get("order_type") in STOP_TYPES and args.get("time_in_force") != "gtc":
        return False, "stop without time_in_force gtc", True
    return True, "ok", True


def decide(payload: dict, root: Path, now: datetime) -> tuple[bool, str]:
    from deskconfig import args_canonical, args_map, validate_tools
    from state import Paths, approvals_locked, iso, read_json

    paths = Paths(root)
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return False, "bad hook input"
    tools = read_json(paths.tools)
    if tools is None or validate_tools(tools, []):
        return False, "tools.json missing or invalid"
    info = tools["tools"].get(tool_name)
    if info is None:
        return False, "tool not in map"
    klass = info.get("class")
    if klass in ("read", "preview"):
        return True, klass
    if klass == "forbidden":
        return False, "forbidden tool"
    if klass not in ("place", "cancel"):
        return False, "unknown tool class"
    st = read_json(paths.state) or {}
    if not st.get("account"):
        return False, "no agentic account recorded"
    args = args_canonical(tools, tool_name, tool_input)
    if str(args.get("account")) != str(st["account"]):
        return False, "wrong account"
    takes_ref = "ref_id" in args_map(tools, tool_name)
    stop_file = paths.stop_file.exists()
    reason = "no matching approval"
    with approvals_locked(paths) as approvals:
        for a in approvals:
            ok, why, specific = match(a, klass, args, takes_ref, now, stop_file)
            if ok:
                a["used"], a["used_at"] = True, iso(now)
                return True, f"approval {a.get('id')}"
            if specific:
                reason = why
    return False, reason


def decide_safe(payload, root: Path, now: datetime) -> tuple[bool, str]:
    try:
        return decide(payload, root, now)
    except Exception:
        return False, "guard error"


def record(root: Path, now: datetime, tool, allow: bool, reason: str, **extra) -> None:
    from state import Paths, log_event
    log_event(Paths(root), "guard", now, decision="allow" if allow else "deny",
              tool=str(tool), reason=reason, **extra)


def selftest(root: Path, now: datetime) -> int:
    from state import Paths, read_json
    tools = read_json(Paths(root).tools, {}) or {}
    names = tools.get("tools", {}) if isinstance(tools.get("tools"), dict) else {}
    server = tools.get("server", "robinhood-trading")
    place = next((n for n, i in names.items() if i.get("class") == "place"),
                 f"mcp__{server}__place_crypto_order")
    forbidden = next((n for n, i in names.items() if i.get("class") == "forbidden"),
                     f"mcp__{server}__transfer_funds")
    fake = {"symbol": "BTC-USD", "side": "buy", "type": "limit", "quantity": 1, "limit_price": 1,
            "ref_id": f"selftest-{uuid.uuid4()}", "rhs_account_number": "selftest"}
    denied = 0
    for tool in (place, forbidden):
        allow, reason = decide_safe({"tool_name": tool, "tool_input": fake}, root, now)
        try:
            record(root, now, tool, allow, reason, selftest=True)
        except Exception:
            pass
        print(("ALLOW " if allow else "DENY ") + reason)
        denied += not allow
    print("SELFTEST: PASS" if denied == 2 else "SELFTEST: FAIL")
    return 0 if denied == 2 else 1


def main(argv: list[str]) -> int:
    now = datetime.now(timezone.utc)
    if "--selftest" in argv:
        try:
            return selftest(ROOT, now)
        except Exception:
            print("SELFTEST: FAIL")
            return 1
    tool = "?"
    try:
        payload = json.loads(sys.stdin.read())
        tool = payload.get("tool_name", "?") if isinstance(payload, dict) else "?"
        allow, reason = decide_safe(payload, ROOT, now) if isinstance(payload, dict) \
            else (False, "bad hook input")
    except Exception:
        allow, reason = False, "guard error"
    try:
        record(ROOT, now, tool, allow, reason)
    except Exception:
        allow, reason = False, "guard error"
    if not allow:
        print(deny_json(reason))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
