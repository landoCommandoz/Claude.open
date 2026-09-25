"""
engine.py | The brain. Every command prints human-readable lines, then one final line.
The agent only ever acts on what engine.py prints.

  preflight --job DAILY|RECONCILE|WEEKLY     plan --job DAILY|RECONCILE
  confirm --action ID                         close --job DAILY|RECONCILE|WEEKLY
  incident --text "..."   weekly   status   wait <seconds>   clear --reason "..."
  test-approval --coin BTC --qty Q --limit P  |  test-approval --cancel ORDER_ID
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskconfig import ConfigError, args_raw, tool_for, tools_for  # noqa: E402
from state import STICKY, StateError, iso, load_approvals, utcnow  # noqa: E402

JOBS = ("DAILY", "RECONCILE", "WEEKLY")
TEST_MAX_USD = 5.0
TEST_MAX_BID_FRAC = 0.60


def make_ctx(root: Path, now, **kw):
    from desk import Ctx
    return Ctx(root, now, **kw)


def run_selftests(root: Path) -> str | None:
    """None when both pass, else the reason."""
    py = sys.executable
    try:
        t = subprocess.run([py, "-m", "pytest", "-q", "tests/test_strategy.py"], cwd=root,
                           capture_output=True, text=True, timeout=600)
        if t.returncode != 0:
            return "strategy tests failing"
        g = subprocess.run([py, str(root / "scripts" / "guard.py"), "--selftest"], cwd=root,
                           capture_output=True, text=True, timeout=120)
        if g.returncode != 0 or g.stdout.count("DENY ") != 2:
            return "guard self-test failed"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"self-test could not run: {exc}"
    return None


def read_calls(ctx) -> list[tuple[str, dict]]:
    acct = ctx.st["account"]
    symbols = sorted(set(ctx.cfg["universe"]) | set(ctx.st["positions"]))
    since = iso(ctx.now - timedelta(days=7))
    values = {"account": acct, "account_number": ctx.st.get("account_number"), "since": since, "symbols": [ctx.tools.get("symbol_format", "{coin}-USD")
                                                          .format(coin=c) for c in symbols]}
    calls = []
    for role in ("accounts", "account", "positions", "orders", "quotes"):
        for tool in tools_for(ctx.tools, role, "read"):
            calls.append((tool, args_raw(ctx.tools, tool, values)))
    return calls


def preflight(root: Path, now, job: str, skip_selftests: bool = False, **kw) -> str:
    try:
        ctx = make_ctx(root, now, **kw)
    except (ConfigError, StateError, ValueError, KeyError) as exc:
        print(f"config or state invalid: {exc}")
        return "NOT_READY config or state invalid"
    problem = None if skip_selftests else run_selftests(root)
    if problem:
        ctx.pause(problem)
        ctx.save()
        return f"NOT_READY {problem}"
    if job == "WEEKLY":
        return "READY"
    if ctx.st["account"] is None:
        try:
            agentic = [a for a in ctx.broker.accounts(ctx.ex["tape_max_age_sec"]) if a.agentic_allowed]
        except Exception:
            agentic = []
        if len(agentic) != 1:
            print(f"CALL 1: {tool_for(ctx.tools, 'accounts')} {{}}")
            return "NOT_READY account not recorded: call get_accounts, then run preflight again"
        ctx.st["account"] = agentic[0].rhs_account_number
        ctx.st["account_number"] = agentic[0].account_number
        ctx.event("account_recorded")
        ctx.save()
    if ctx.st["state"] in STICKY:
        print(f"State {ctx.st['state']}: protection only. {ctx.st['reason']}")
    if ctx.stop_file():
        print("STOP file present: no new buys. Protection keeps running.")
    for i, (tool, args) in enumerate(read_calls(ctx), 1):
        print(f"CALL {i}: {tool} {json.dumps(args, separators=(',', ':'))}")
    return "READY"


def cmd_plan(root, now, job, **kw) -> str:
    from actions import printable
    from entries import make_plan
    if job not in ("DAILY", "RECONCILE"):
        return "PLAN_ERROR plan runs for DAILY or RECONCILE only"
    ctx = make_ctx(root, now, **kw)
    return printable(make_plan(ctx, job))


def cmd_confirm(root, now, action_id, **kw) -> str:
    from confirmer import confirm
    return confirm(make_ctx(root, now, **kw), action_id)


def cmd_close(root, now, job, **kw) -> str:
    from closer import close
    return json.dumps(close(make_ctx(root, now, **kw), job), separators=(",", ":"))


def cmd_status(root, now, **kw) -> str:
    from closer import status_line, write_status_md
    ctx = make_ctx(root, now, **kw)
    line = status_line(ctx, "STATUS")
    write_status_md(ctx, line)
    return json.dumps(line, separators=(",", ":"))


def cmd_weekly(root, now, **kw) -> str:
    from closer import phase0_metrics, weekly
    ctx = make_ctx(root, now, **kw)
    out = weekly(ctx, phase0_metrics(ctx))
    for key, value in out.items():
        print(f"{key}: {json.dumps(value)}")
    return "WEEKLY: DRIFT ALARM" if out["drift_alarm"] else "WEEKLY: OK"


def cmd_clear(root, now, reason: str, stdin=None) -> str:
    stdin = stdin or sys.stdin
    if os.environ.get("TREND_DESK_RUNNER") or not stdin.isatty():
        return "REFUSED clear is for Lando at his own terminal"
    ctx = make_ctx(root, now, load_tools_now=False)
    if ctx.st["state"] not in STICKY:
        return f"NOTHING TO CLEAR state is {ctx.st['state']}"
    print(f"State {ctx.st['state']}: {ctx.st['reason']}")
    print("Type CLEAR DESK to clear it: ", end="", flush=True)
    if stdin.readline().strip() != "CLEAR DESK":
        return "NOT CLEARED"
    was = ctx.st["state"]
    ctx.st["state"], ctx.st["reason"] = "SCANNING", ""
    ctx.event("cleared", was=was, who=getpass.getuser(), reason=reason)
    ctx.save()
    return f"CLEARED {was}"


def cmd_test_approval(root, now, coin=None, qty=None, limit=None, cancel=None, **kw) -> str:
    from actions import approval_for, cancel_action, order_action, write_approvals
    ctx = make_ctx(root, now, **kw)
    if ctx.mode == "LIVE":
        return "REFUSED test-approval is Phase 1 only and mode is LIVE"
    if not ctx.st["account"]:
        return "REFUSED no agentic account recorded"
    tests = [a for a in load_approvals(ctx.paths) if a.get("mode") == "TEST"]
    if cancel:
        placed = [a for a in tests if a["kind"] == "TEST" and a.get("used")]
        refs = {a["ref_id"] for a in placed}
        ours = [o for r, o, _ in ctx.broker.placements(now - timedelta(days=1)) if r in refs]
        if not any(o.id == cancel for o in ours):
            return "REFUSED that order id is not the TEST order"
        if any(a["kind"] == "TEST_CANCEL" for a in tests):
            return "REFUSED the TEST cancel approval was already issued"
        action = cancel_action(ctx, ours[0].coin, cancel, "Phase 1 guard test cancel", kind="TEST_CANCEL")
    else:
        if any(a["kind"] == "TEST" for a in tests):
            return "REFUSED the one TEST approval was already issued"
        coin = (coin or "").upper()
        if coin not in ctx.cfg["universe"] or not qty or not limit or qty <= 0 or limit <= 0:
            return "REFUSED need --coin in the universe, --qty and --limit above zero"
        q = ctx.broker.quotes(ctx.ex["tape_max_age_sec"]).get(coin)
        if q is None:
            return "REFUSED no fresh quote for that coin on the tape"
        if limit > TEST_MAX_BID_FRAC * q.bid or qty * limit > TEST_MAX_USD:
            return "REFUSED the test order must be a limit buy of at most $5 at or below 60% of the bid"
        action = order_action(ctx, "TEST", coin, "buy", "limit", qty, limit=limit,
                              why="Phase 1 guard test, cannot fill")
    action["id"] = f"T{uuid.uuid4().hex[:6]}"
    action["issued_at"] = iso(now)
    write_approvals(ctx, [approval_for(ctx, action, "TEST")])
    print(f"TOOL: {action['tool']}")
    print(f"ARGS: {json.dumps(action['args'], separators=(',', ':'))}")
    return "TEST APPROVAL ISSUED"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trend Desk engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "plan", "close"):
        sub.add_parser(name).add_argument("--job", required=True, choices=JOBS)
    sub.add_parser("confirm").add_argument("--action", required=True)
    sub.add_parser("incident").add_argument("--text", required=True)
    sub.add_parser("weekly")
    sub.add_parser("status")
    sub.add_parser("wait").add_argument("seconds", type=float)
    sub.add_parser("clear").add_argument("--reason", required=True)
    ta = sub.add_parser("test-approval")
    ta.add_argument("--coin")
    ta.add_argument("--qty", type=float)
    ta.add_argument("--limit", type=float)
    ta.add_argument("--cancel")
    args = ap.parse_args(argv)
    now = utcnow()
    try:
        if args.cmd == "preflight":
            last = preflight(ROOT, now, args.job)
        elif args.cmd == "plan":
            last = cmd_plan(ROOT, now, args.job)
        elif args.cmd == "confirm":
            last = cmd_confirm(ROOT, now, args.action)
        elif args.cmd == "close":
            last = cmd_close(ROOT, now, args.job)
        elif args.cmd == "incident":
            make_ctx(ROOT, now, load_tools_now=False).incident(args.text[:500])
            last = "INCIDENT RECORDED"
        elif args.cmd == "weekly":
            last = cmd_weekly(ROOT, now)
        elif args.cmd == "status":
            last = cmd_status(ROOT, now, load_tools_now=False)
        elif args.cmd == "wait":
            time.sleep(max(0.0, min(args.seconds, 60.0)))
            last = "WAITED"
        elif args.cmd == "clear":
            last = cmd_clear(ROOT, now, args.reason)
        else:
            last = cmd_test_approval(ROOT, now, args.coin, args.qty, args.limit, args.cancel)
    except Exception as exc:              # fail closed: an engine error moves no money
        print(f"ENGINE ERROR: {type(exc).__name__}: {exc}")
        last = "ENGINE_ERROR"
        try:
            make_ctx(ROOT, now, load_tools_now=False).incident(f"engine error in {args.cmd}: {exc}"[:300])
        except Exception:
            pass
    print(last)
    return 0 if not last.startswith(("ENGINE_ERROR", "NOT_READY", "REFUSED")) else 1


if __name__ == "__main__":
    sys.exit(main())
