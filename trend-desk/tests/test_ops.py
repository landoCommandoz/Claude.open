"""BUILD.md section 4.7 (watcher) and 4.8 (runner)."""
import json
import os
import stat
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

import runner
import watcher
from helpers import make_root
from state import Paths, atomic_write_json, iso, read_json, read_jsonl

DAY = datetime(2026, 10, 2, tzinfo=timezone.utc)          # a Friday


def at(h, m=0, day=DAY):
    return day.replace(hour=h, minute=m)


# ---------- watcher ----------

class Runs:
    """Stands in for runner.run_job. A successful DAILY records last_success like engine close does."""

    def __init__(self, root, ok=True):
        self.root, self.ok, self.jobs, self.clock = root, ok, [], None

    def __call__(self, job):
        self.jobs.append(job)
        if self.ok:
            st = read_json(Paths(self.root).state)
            st["last_success"][job] = iso(self.clock)
            atomic_write_json(Paths(self.root).state, st)
        return {"ok": self.ok, "reason": "ok" if self.ok else "exit code 1"}


def tick(root, runs, now, price=lambda c: None, alerts=None):
    runs.clock = now
    return watcher.tick(root, now, run=runs, price=price,
                        alert=(lambda t, m: alerts.append(t)) if alerts is not None else (lambda t, m: None))


def test_one_daily_per_day(tmp_path):
    root = make_root(tmp_path)
    runs = Runs(root)
    assert tick(root, runs, at(0, 2)) == []                       # before 00:07
    assert tick(root, runs, at(0, 10)) == ["DAILY ok"]
    assert tick(root, runs, at(0, 15)) == [] and tick(root, runs, at(12)) == []
    assert tick(root, runs, at(0, 10, DAY + timedelta(days=1))) == ["DAILY ok"]
    assert runs.jobs == ["DAILY", "DAILY"]
    assert read_json(Paths(root).heartbeat)["ts"] == iso(at(0, 10, DAY + timedelta(days=1)))


def test_failed_daily_retries_every_30_minutes_three_times_then_alerts(tmp_path):
    root = make_root(tmp_path)
    runs, alerts = Runs(root, ok=False), []
    times = [at(0, 10), at(0, 20), at(0, 41), at(1, 12), at(1, 43), at(2, 14), at(3, 0)]
    for t in times:
        tick(root, runs, t, alerts=alerts)
    assert runs.jobs == ["DAILY"] * 4
    assert "Trend Desk DAILY failed" in alerts and "Trend Desk missed run" in alerts


def test_reconcile_on_a_breach_with_a_30_minute_cooldown(tmp_path):
    root = make_root(tmp_path)
    st = read_json(Paths(root).state)
    st["positions"]["BTC"] = {"qty": 0.1, "entry_fill": 110, "entry_ts": "x", "n_at_entry": 5,
                              "initial_stop": 100.0, "stop": 100.0}
    st["last_success"]["DAILY"] = iso(at(0, 9))
    atomic_write_json(Paths(root).state, st)
    runs = Runs(root)
    below = lambda c: 99.0                                        # more than 0.5% under the stop
    assert tick(root, runs, at(9), price=below) == ["RECONCILE ok"]
    assert tick(root, runs, at(9, 20), price=below) == []
    assert tick(root, runs, at(9, 31), price=below) == ["RECONCILE ok"]
    assert tick(root, runs, at(10, 5), price=lambda c: 99.7) == []  # within 0.5%: no breach
    assert runs.jobs == ["RECONCILE", "RECONCILE"]


def test_watcher_respects_the_runner_lock(tmp_path):
    root = make_root(tmp_path)
    Paths(root).runner_lock.write_text(json.dumps({"pid": 1, "started": iso(at(0, 5))}))
    runs = Runs(root)
    assert tick(root, runs, at(0, 10)) == ["locked"] and runs.jobs == []


def test_weekly_runs_once_on_the_review_day_after_daily(tmp_path):
    root = make_root(tmp_path)
    runs = Runs(root)
    sunday = DAY + timedelta(days=2)
    assert tick(root, runs, at(0, 10, sunday)) == ["DAILY ok", "WEEKLY ok"]
    assert tick(root, runs, at(0, 40, sunday)) == []


def test_stop_file_does_not_stop_the_watcher(tmp_path):
    root = make_root(tmp_path)
    (root / "STOP").write_text("")
    assert tick(root, Runs(root), at(0, 10)) == ["DAILY ok"]


# ---------- runner ----------

FAKE = r'''
import json, os, subprocess, sys, time
from datetime import datetime, timezone
mode = os.environ.get("FAKE_MODE", "ok")
prompt = sys.stdin.read()
root = os.getcwd()
if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.path.join(root, "pids.json"), "w").write(json.dumps([os.getpid(), child.pid]))
    time.sleep(60)
if mode == "fail":
    print("401 unauthorized: login expired"); sys.exit(1)
status = {"job": "DAILY", "state": "SCANNING", "mode": "DRY", "equity": 400.0}
if mode != "noclose":
    with open(os.path.join(root, "state", "runs.jsonl"), "a") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), **status}) + "\n")
final = "all done, no status" if mode == "badline" else json.dumps(status)
print(json.dumps({"type": "result", "result": "JOB: " + prompt.splitlines()[0] + "\n" + final}))
'''


@pytest.fixture
def desk(tmp_path, monkeypatch):
    root = make_root(tmp_path, copy_scripts=True, mode="DRY")
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE}")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    alerts = []
    monkeypatch.setenv("FAKE_MODE", "ok")
    return root, str(fake), alerts


def job(desk, **kw):
    root, fake, alerts = desk
    return runner.run_job(root, "DAILY", claude=fake, alert=lambda t, m: alerts.append((t, m)), **kw)


def test_runner_success_detection_and_prompt_on_stdin(desk):
    root, _, alerts = desk
    result = job(desk)
    assert result["ok"] and result["reason"] == "ok" and alerts == []
    log = next(Paths(root).logs.glob("*_DAILY.log")).read_text()
    assert "JOB: JOB: DAILY" in log                     # the prompt file reached claude on stdin
    assert not Paths(root).runner_lock.exists()
    assert read_jsonl(Paths(root).events)[-1]["type"] == "run"


@pytest.mark.parametrize("mode,reason", [("noclose", "engine close did not record this run"),
                                         ("badline", "final line is not the status JSON"),
                                         ("fail", "exit code 1")])
def test_runner_failure_detection(desk, monkeypatch, mode, reason):
    monkeypatch.setenv("FAKE_MODE", mode)
    result = job(desk)
    assert not result["ok"] and result["reason"] == reason
    titles = [t for t, _ in desk[2]]
    assert "Trend Desk run failed" in titles
    assert ("Trend Desk login" in titles) == (mode == "fail")


def test_runner_lock_blocks_a_second_run(desk):
    root, _, _ = desk
    Paths(root).runner_lock.write_text(json.dumps({"pid": 1, "started": iso(datetime.now(timezone.utc))}))
    assert job(desk) == {"ok": False, "reason": "locked"}
    assert Paths(root).runner_lock.exists()             # someone else's lock is left alone


def test_runner_breaks_a_stale_lock_and_alerts(desk):
    root, _, alerts = desk
    old = datetime.now(timezone.utc) - timedelta(minutes=46)
    Paths(root).runner_lock.write_text(json.dumps({"pid": 1, "started": iso(old)}))
    assert job(desk)["ok"]
    assert ("Trend Desk stale lock", "A run lock older than 45 minutes was broken.") in alerts


def test_runner_timeout_kills_the_whole_process_tree(desk, monkeypatch):
    root, _, _ = desk
    monkeypatch.setenv("FAKE_MODE", "sleep")
    started = time.monotonic()
    result = job(desk, timeout=3)
    assert not result["ok"] and result["reason"].startswith("timed out")
    assert time.monotonic() - started < 30
    pids = json.loads((root / "pids.json").read_text())
    time.sleep(0.5)
    assert not any(alive(pid) for pid in pids)          # both the agent and its child are gone


def alive(pid: int) -> bool:
    """A killed process the container's init has not reaped yet (zombie, Z) is dead."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def test_runner_refuses_to_start_when_the_guard_self_test_fails(desk):
    root, _, alerts = desk
    (root / "scripts" / "guard.py").write_text("import sys\nprint('ALLOW everything')\nsys.exit(0)\n")
    result = job(desk)
    assert not result["ok"] and result["reason"] == "guard self-test failed"
    assert read_json(Paths(root).state)["state"] == "PAUSED"
    assert not list(Paths(root).logs.glob("*.log")) if Paths(root).logs.exists() else True


def test_parse_status_reads_the_last_line():
    line = {"job": "DAILY", "state": "SCANNING"}
    assert runner.parse_status(json.dumps({"result": "text\n" + json.dumps(line)})) == line
    assert runner.parse_status("plain\n" + json.dumps(line)) == line
    assert runner.parse_status("nothing useful") is None
