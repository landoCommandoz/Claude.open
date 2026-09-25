"""
runner.py | Starts Claude Code headless for one job, with a lock, a timeout, and locked permissions.

Usage: runner.py DAILY|RECONCILE|WEEKLY     exit 0 success, 1 failure, 3 locked

Windows (setup/WINDOWS.md): claude is found with shutil.which (npm installs claude.cmd), the
prompt goes in on stdin (multi-line arguments break under cmd.exe), and a timeout kills the
whole process tree with taskkill /T /F.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from state import Paths, atomic_write_text, iso, log_event, parse_ts, read_json, read_jsonl  # noqa: E402

JOBS = ("DAILY", "RECONCILE", "WEEKLY")
LOCK_STALE_SEC = 45 * 60
TIMEOUT_SEC = 20 * 60
MAX_TURNS = 60
LOGIN_HINTS = ("unauthorized", "401", "login", "authenticate", "authentication", "expired token")


def claude_args(claude: str) -> list[str]:
    """Verified against claude --help in Phase 1. The prompt itself goes in on stdin."""
    return [claude, "-p", "--permission-mode", "dontAsk", "--max-turns", str(MAX_TURNS),
            "--output-format", "json"]


def lock_held(paths: Paths, now: datetime) -> bool:
    info = read_json(paths.runner_lock) if paths.runner_lock.exists() else None
    if info is None:
        return paths.runner_lock.exists()
    try:
        return (now - parse_ts(info["started"])).total_seconds() < LOCK_STALE_SEC
    except (KeyError, ValueError, TypeError):
        return False


def acquire_lock(paths: Paths, now: datetime, alert) -> bool:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    if paths.runner_lock.exists():
        if lock_held(paths, now):
            return False
        paths.runner_lock.unlink(missing_ok=True)
        alert("Trend Desk stale lock", "A run lock older than 45 minutes was broken.")
    try:
        fd = os.open(paths.runner_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "started": iso(now)}))
    return True


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


def parse_status(output: str) -> dict | None:
    """The final line of the agent's answer must be the engine's status JSON."""
    text = output
    try:
        obj = json.loads(output)
        if isinstance(obj, dict) and isinstance(obj.get("result"), str):
            text = obj["result"]
    except ValueError:
        pass
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        status = json.loads(lines[-1])
    except ValueError:
        return None
    return status if isinstance(status, dict) and "job" in status and "state" in status else None


def checks(root: Path) -> str | None:
    """Guard self-test and config validation. None means both pass."""
    from deskconfig import ConfigError, load_config, load_tools
    try:
        cfg = load_config(Paths(root).config)
        load_tools(Paths(root).tools, cfg["universe"])
    except ConfigError as exc:
        return f"config invalid: {exc}"[:200]
    try:
        g = subprocess.run([sys.executable, str(root / "scripts" / "guard.py"), "--selftest"],
                           cwd=root, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"guard self-test could not run: {exc}"
    if g.returncode != 0 or g.stdout.count("DENY ") != 2:
        return "guard self-test failed"
    return None


def pause(root: Path, reason: str, now: datetime) -> None:
    try:
        from desk import Ctx
        ctx = Ctx(root, now, load_tools_now=False)
        ctx.pause(reason)
        ctx.save()
    except Exception:
        pass                               # config unreadable: the alert below still goes out


def default_alert(root: Path):
    def send(title: str, message: str) -> None:
        try:
            from alert import send as push
            from deskconfig import load_config
            push(root, load_config(Paths(root).config), title, message, datetime.now(timezone.utc))
        except Exception:
            from alert import send as push
            push(root, {}, title, message, datetime.now(timezone.utc))
    return send


def run_job(root: Path, job: str, *, claude: str | None = None, timeout: float = TIMEOUT_SEC,
            alert=None, now_fn=lambda: datetime.now(timezone.utc)) -> dict:
    paths, start = Paths(root), now_fn()
    alert = alert or default_alert(root)
    if job not in JOBS:
        return {"ok": False, "reason": f"unknown job {job}"}
    if not acquire_lock(paths, start, alert):
        return {"ok": False, "reason": "locked"}
    try:
        problem = checks(root)
        if problem:
            pause(root, problem, start)
            alert("Trend Desk PAUSED", f"{job} not started: {problem}")
            return {"ok": False, "reason": problem}
        claude = claude or shutil.which("claude")
        if not claude:
            alert("Trend Desk run failed", f"{job}: claude executable not found")
            return {"ok": False, "reason": "claude not found"}
        prompt = (root / "prompts" / f"{job.lower()}.md").read_text(encoding="utf-8")
        env = {**os.environ, "TREND_DESK_RUNNER": "1"}
        extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})
        proc = subprocess.Popen(claude_args(claude), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, cwd=root, env=env, text=True,
                                encoding="utf-8", errors="replace", **extra)
        timed_out = False
        try:
            out, _ = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            try:
                out, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                out = ""
        end = now_fn()
        log_path = paths.logs / f"{start.strftime('%Y%m%dT%H%M%SZ')}_{job}.log"
        atomic_write_text(log_path, out or "")
        status = parse_status(out or "")
        closed = any(r.get("job") == job and r.get("ts", "") >= iso(start)
                     for r in read_jsonl(paths.runs))
        if timed_out:
            reason = f"timed out after {timeout:.0f}s, process tree killed"
        elif proc.returncode != 0:
            reason = f"exit code {proc.returncode}"
        elif status is None:
            reason = "final line is not the status JSON"
        elif not closed:
            reason = "engine close did not record this run"
        else:
            reason = "ok"
        ok = reason == "ok"
        log_event(paths, "run", end, job=job, ok=ok, reason=reason,
                  seconds=round((end - start).total_seconds(), 1))
        if not ok:
            alert("Trend Desk run failed", f"{job}: {reason}")
            if any(h in (out or "").lower() for h in LOGIN_HINTS):
                alert("Trend Desk login", "Robinhood login may have expired. Open Claude Code in the "
                                          "folder, run /mcp, log in again.")
        return {"ok": ok, "reason": reason, "status": status}
    finally:
        paths.runner_lock.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in JOBS:
        print("usage: runner.py DAILY|RECONCILE|WEEKLY")
        return 1
    result = run_job(ROOT, argv[0])
    print(json.dumps({k: v for k, v in result.items() if k != "status"}))
    if result["reason"] == "locked":
        return 3
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
