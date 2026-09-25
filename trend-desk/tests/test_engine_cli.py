"""engine.py commands around the plan: preflight, test-approval, clear, close, incident; alert redaction."""
import io
import json

import alert
import engine
from closer import close
from helpers import ACCT, NOW, FakeBroker, ctx_for, flat, log_guard_reads, make_root, quote
from state import Paths, read_json, read_jsonl

COINS = ("BTC", "ETH", "SOL", "DOGE")


def test_preflight_lists_the_exact_read_calls_in_order(tmp_path, capsys):
    root = make_root(tmp_path)
    last = engine.preflight(root, NOW, "DAILY", skip_selftests=True, broker=FakeBroker())
    calls = [l for l in capsys.readouterr().out.splitlines() if l.startswith("CALL ")]
    assert last == "READY"
    tools = [c.split()[2] for c in calls]
    assert [t.rsplit("__", 1)[1] for t in tools] == ["get_accounts", "get_portfolio", "get_crypto_positions",
                                                     "get_crypto_orders", "get_crypto_quotes"]
    quotes_args = json.loads(calls[-1].split(" ", 3)[3])
    assert quotes_args == {"symbols": ["BTC-USD", "DOGE-USD", "ETH-USD", "SOL-USD"], "rhs_account_number": ACCT}
    assert engine.preflight(root, NOW, "WEEKLY", skip_selftests=True, broker=FakeBroker()) == "READY"


def test_preflight_records_the_account_from_a_fresh_get_accounts(tmp_path):
    root = make_root(tmp_path, account=None)
    assert engine.preflight(root, NOW, "DAILY", skip_selftests=True, broker=FakeBroker()) == "READY"
    assert read_json(Paths(root).state)["account"] == ACCT


def test_preflight_pauses_when_the_self_tests_fail(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(engine, "run_selftests", lambda r: "guard self-test failed")
    assert engine.preflight(root, NOW, "DAILY", broker=FakeBroker()) == "NOT_READY guard self-test failed"
    assert read_json(Paths(root).state)["state"] == "PAUSED"


def test_test_approval_issues_one_test_stamp_for_the_guard_test_order(tmp_path):
    root = make_root(tmp_path, mode="DRY")
    b = FakeBroker(quotes={"BTC": quote(60000.0)})
    too_close = engine.cmd_test_approval(root, NOW, "BTC", 0.0001, 40000.0, broker=b)
    assert too_close.startswith("REFUSED")                          # above 60% of the bid
    assert engine.cmd_test_approval(root, NOW, "BTC", 0.0002, 30000.0, broker=b).startswith("REFUSED")  # $6
    assert engine.cmd_test_approval(root, NOW, "BTC", 0.00006, 29000.0, broker=b) == "TEST APPROVAL ISSUED"
    approvals = read_json(Paths(root).approvals)
    assert [a["mode"] for a in approvals] == ["TEST"] and approvals[0]["kind"] == "TEST"
    assert engine.cmd_test_approval(root, NOW, "BTC", 0.00006, 29000.0, broker=b).startswith("REFUSED")


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_clear_is_human_only(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    st = read_json(Paths(root).state)
    st.update(state="PAUSED", reason="unknown position: ETH")
    Paths(root).state.write_text(json.dumps(st))
    assert engine.cmd_clear(root, NOW, "checked", stdin=io.StringIO("CLEAR DESK\n")).startswith("REFUSED")
    monkeypatch.setenv("TREND_DESK_RUNNER", "1")
    assert engine.cmd_clear(root, NOW, "checked", stdin=Tty("CLEAR DESK\n")).startswith("REFUSED")
    monkeypatch.delenv("TREND_DESK_RUNNER")
    assert engine.cmd_clear(root, NOW, "checked", stdin=Tty("clear\n")) == "NOT CLEARED"
    assert engine.cmd_clear(root, NOW, "checked", stdin=Tty("CLEAR DESK\n")) == "CLEARED PAUSED"
    assert read_json(Paths(root).state)["state"] == "SCANNING"
    assert read_jsonl(Paths(root).events)[-1]["reason"] == "checked"


def test_close_writes_status_line_status_md_report_and_runs(tmp_path):
    root = make_root(tmp_path)
    log_guard_reads(root)
    sink = []
    ctx = ctx_for(root, FakeBroker(quotes={c: quote(100.0) for c in COINS}),
                  {c: flat() for c in COINS}, alerts=sink)
    line = close(ctx, "DAILY")
    assert set(line) == {"job", "state", "mode", "equity", "day_pnl", "week_pnl", "open_positions",
                         "open_risk", "floor", "trades_today", "incidents_today", "next_daily_utc"}
    assert line["next_daily_utc"] == "2026-10-03T00:07:00Z"
    assert "Trend Desk SCANNING" in [t for t, _ in sink]
    assert (root / "STATUS.md").exists() and (root / "reports" / "daily" / "2026-10-02.md").exists()
    assert read_jsonl(Paths(root).runs)[-1]["job"] == "DAILY"
    assert read_json(Paths(root).state)["last_success"]["DAILY"] == "2026-10-02T00:07:00Z"


def test_incident_alerts_on_keywords_only(tmp_path):
    root = make_root(tmp_path)
    sink = []
    ctx = ctx_for(root, FakeBroker(), {}, alerts=sink)
    ctx.incident("tool timed out twice")
    ctx.incident("guard blocked an order")
    assert [m for _, m in sink] == ["guard blocked an order"]
    assert len(read_jsonl(Paths(root).incidents)) == 2


def test_alerts_redact_ids_and_dedupe_for_an_hour(tmp_path):
    root = make_root(tmp_path)
    posts = []
    cfg = {"alerts": {"ntfy_topic": "abc123"}}
    msg = "order 3f2b8c1e-1111-2222-3333-444455556666 on account 123456789 token AbC123xYz789QwE456rTy0"
    post = lambda url, **kw: posts.append((url, kw)) or type("R", (), {"status_code": 200})()
    assert alert.send(root, cfg, "Trend Desk", msg, NOW, post=post) is True
    assert alert.send(root, cfg, "Trend Desk", msg, NOW, post=post) is False
    sent = posts[0][1]["data"].decode()
    assert "3f2b8c1e" not in sent and "123456789" not in sent and "AbC123" not in sent
    assert posts[0][0] == "https://ntfy.sh/abc123" and len(posts) == 1
