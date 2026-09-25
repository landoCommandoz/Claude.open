"""
alert.py | Push alerts to Lando's phone through ntfy.sh.

Anyone with the topic can read it, so messages carry only state, equity, P&L, coin names,
and a short reason. redact() strips anything shaped like an id, account number, or token
before a message leaves the machine. Every alert also goes to state/alerts.log.
The same text sends at most once per hour. Alerts never raise: a failed alert must not
stop protection.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from state import Paths, append_line, atomic_write_json, iso, read_json

NTFY_URL = "https://ntfy.sh/{topic}"
DEDUPE_SEC = 3600
TIMEOUT_SEC = 10
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_LONG_DIGITS = re.compile(r"\b\d{7,}\b")
_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,}\b")


def redact(text: str) -> str:
    text = _UUID.sub("[id]", str(text))
    text = _TOKEN.sub("[id]", text)
    return _LONG_DIGITS.sub("[number]", text)[:400]


def send(root: Path, cfg: dict, title: str, message: str, now: datetime,
         post=None) -> bool:
    """Returns True when pushed to ntfy. Logs either way. Never raises."""
    try:
        paths = Paths(root)
        title, message = redact(title)[:80], redact(message)
        append_line(paths.state_dir / "alerts.log", f"{iso(now)} {title}: {message}")
        sent_path = paths.state_dir / "alerts_sent.json"
        sent = read_json(sent_path, {}) or {}
        key = hashlib.sha256(f"{title}|{message}".encode()).hexdigest()[:16]
        last = sent.get(key)
        if last is not None and now.timestamp() - last < DEDUPE_SEC:
            return False
        sent = {k: v for k, v in sent.items() if now.timestamp() - v < DEDUPE_SEC}
        sent[key] = now.timestamp()
        atomic_write_json(sent_path, sent)
        topic = (cfg.get("alerts") or {}).get("ntfy_topic", "")
        if not topic:
            return False
        if post is None:
            import requests
            post = requests.post
        resp = post(NTFY_URL.format(topic=topic), data=message.encode("utf-8"),
                    headers={"Title": title}, timeout=TIMEOUT_SEC)
        return getattr(resp, "status_code", 0) == 200
    except Exception as exc:
        try:
            append_line(Paths(root).state_dir / "alerts.log", f"{iso(now)} ALERT FAILED: {exc}")
        except Exception:
            pass
        return False
