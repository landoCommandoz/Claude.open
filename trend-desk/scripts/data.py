"""
data.py | Public daily candles for the Trend Desk (Coinbase Exchange, no keys, no AI)

Commands:
  backfill --since 2019-10-01   all universe coins, paging back 300 days at a time
  update                        refetch the last 10 days and merge
  price --coin BTC              prints {"coin","price","time"} as JSON
  check                         validates every file, prints last date and status per coin

Writes data/{COIN}.csv (date,open,high,low,close,volume; ascending, unique dates,
completed UTC days only) and data/status.json (OK or STALE per coin).
Every write is atomic: temp file in the same folder, flush, fsync, os.replace.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG = ROOT / "config" / "risk.json"

BASE_URL = "https://api.exchange.coinbase.com/products"
PAGE_DAYS = 300                 # Coinbase returns at most 300 buckets per request
REQUEST_GAP_SEC = 0.4
RETRY_BACKOFF_SEC = (1, 3, 9)
TIMEOUT_SEC = 15
UPDATE_DAYS = 10
GAP_CHECK_DAYS = 60
COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class DataError(Exception):
    pass


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def universe() -> list[str]:
    with open(CONFIG, encoding="utf-8") as fh:
        coins = json.load(fh)["universe"]
    if not coins or not all(isinstance(c, str) and c for c in coins):
        raise DataError("config universe is empty or invalid")
    return [c.upper() for c in coins]


# ---------- network ----------

_last_request = [0.0]


def get_json(url: str, params: dict | None = None, *, session=requests, sleep=time.sleep):
    """GET with 0.4 s spacing, 3 retries (1, 3, 9 s backoff), 15 s timeout."""
    last_err = "no attempt"
    for attempt in range(len(RETRY_BACKOFF_SEC) + 1):
        wait = REQUEST_GAP_SEC - (time.monotonic() - _last_request[0])
        if wait > 0:
            sleep(wait)
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT_SEC)
            _last_request[0] = time.monotonic()
            if resp.status_code == 200:
                return resp.json()
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except (requests.RequestException, ValueError) as exc:
            _last_request[0] = time.monotonic()
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < len(RETRY_BACKOFF_SEC):
            sleep(RETRY_BACKOFF_SEC[attempt])
    raise DataError(f"{url} failed after retries: {last_err}")


def parse_candles(raw) -> pd.DataFrame:
    """Coinbase bucket order is [time, low, high, open, close, volume], newest first."""
    if not isinstance(raw, list):
        raise DataError(f"unexpected candle response: {str(raw)[:200]}")
    rows = []
    for b in raw:
        if not isinstance(b, list) or len(b) < 6:
            raise DataError(f"malformed candle bucket: {b!r}")
        t, low, high, opn, close, vol = b[:6]
        day = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
        rows.append({"date": day.isoformat(), "open": float(opn), "high": float(high),
                     "low": float(low), "close": float(close), "volume": float(vol)})
    return pd.DataFrame(rows, columns=COLUMNS)


def fetch_range(coin: str, start: date, end: date, *, session=requests, sleep=time.sleep) -> pd.DataFrame:
    """Daily candles for start..end inclusive, paging back from end 300 days at a time."""
    frames = []
    page_end = end
    while page_end >= start:
        page_start = max(start, page_end - timedelta(days=PAGE_DAYS - 1))
        params = {"granularity": 86400,
                  "start": f"{page_start.isoformat()}T00:00:00Z",
                  "end": f"{page_end.isoformat()}T00:00:00Z"}
        raw = get_json(f"{BASE_URL}/{coin}-USD/candles", params, session=session, sleep=sleep)
        frames.append(parse_candles(raw))
        page_end = page_start - timedelta(days=1)
    return normalize(pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS))


def normalize(df: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    """Ascending, unique dates (last one wins), completed UTC days only."""
    today = today or utc_today()
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = df[COLUMNS].copy()
    df = df[df["date"] < today.isoformat()]
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
    return df.reset_index(drop=True)


# ---------- files ----------

def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def csv_path(coin: str, data_dir: Path) -> Path:
    return data_dir / f"{coin}.csv"


def load(coin: str, data_dir: Path) -> pd.DataFrame:
    path = csv_path(coin, data_dir)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path, dtype={"date": str})[COLUMNS]


def save(coin: str, df: pd.DataFrame, data_dir: Path) -> None:
    atomic_write_text(csv_path(coin, data_dir), df.to_csv(index=False, lineterminator="\n"))


def merge(old: pd.DataFrame, new: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    frames = [f for f in (old, new) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return normalize(pd.concat(frames, ignore_index=True), today)


# ---------- validation ----------

def validate(df: pd.DataFrame, today: date | None = None) -> list[str]:
    """Empty list means OK. Any reason means the coin is STALE."""
    today = today or utc_today()
    yesterday = today - timedelta(days=1)
    if df.empty:
        return ["no data"]
    reasons = []
    num = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if num.isna().any().any() or (num <= 0).any().any():
        reasons.append("non-positive or missing price")
    if (num["high"] < num[["open", "close"]].max(axis=1)).any():
        reasons.append("high below open or close")
    if (num["low"] > num[["open", "close"]].min(axis=1)).any():
        reasons.append("low above open or close")
    if df["date"].duplicated().any() or not df["date"].is_monotonic_increasing:
        reasons.append("dates not ascending and unique")
    have = set(df["date"])
    window = [(yesterday - timedelta(days=i)).isoformat() for i in range(GAP_CHECK_DAYS)]
    missing = [d for d in window if d not in have]
    if missing:
        reasons.append(f"{len(missing)} missing day(s) in last {GAP_CHECK_DAYS}, e.g. {missing[0]}")
    last = df["date"].iloc[-1]
    if last != yesterday.isoformat():
        reasons.append(f"last date {last}, expected {yesterday.isoformat()}")
    return reasons


def check(coins: list[str], data_dir: Path, today: date | None = None) -> dict:
    today = today or utc_today()
    status = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "expected_last_date": (today - timedelta(days=1)).isoformat(), "coins": {}}
    for coin in coins:
        try:
            df = load(coin, data_dir)
            reasons = validate(df, today)
        except Exception as exc:  # unreadable file: STALE, never trusted
            df, reasons = pd.DataFrame(columns=COLUMNS), [f"unreadable: {exc}"]
        status["coins"][coin] = {
            "status": "STALE" if reasons else "OK",
            "first_date": df["date"].iloc[0] if not df.empty else None,
            "last_date": df["date"].iloc[-1] if not df.empty else None,
            "rows": int(len(df)), "reasons": reasons}
    atomic_write_text(data_dir / "status.json", json.dumps(status, indent=2) + "\n")
    return status


def print_status(status: dict) -> int:
    stale = 0
    for coin, s in status["coins"].items():
        detail = f" | {'; '.join(s['reasons'])}" if s["reasons"] else ""
        print(f"{coin:<5} {s['status']:<5} {s['first_date']} .. {s['last_date']} "
              f"({s['rows']} days){detail}")
        stale += s["status"] != "OK"
    print(f"DATA: {'OK' if not stale else f'STALE {stale}'}")
    return 1 if stale else 0


# ---------- commands ----------

def cmd_fetch(coins: list[str], start_for, data_dir: Path) -> int:
    failed = 0
    yesterday = utc_today() - timedelta(days=1)
    for coin in coins:
        try:
            new = fetch_range(coin, start_for(coin), yesterday)
            save(coin, merge(load(coin, data_dir), new), data_dir)
        except Exception as exc:
            failed += 1
            print(f"{coin}: FAILED {exc}")
    code = print_status(check(coins, data_dir))
    return 1 if failed else code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Public market data for the Trend Desk")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill")
    bf.add_argument("--since", required=True, type=date.fromisoformat)
    sub.add_parser("update")
    pr = sub.add_parser("price")
    pr.add_argument("--coin", required=True)
    sub.add_parser("check")
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    try:
        if args.cmd == "price":
            coin = args.coin.upper()
            t = get_json(f"{BASE_URL}/{coin}-USD/ticker")
            print(json.dumps({"coin": coin, "price": float(t["price"]), "time": t["time"]}))
            return 0
        coins = universe()
        if args.cmd == "backfill":
            return cmd_fetch(coins, lambda c: args.since, data_dir)
        if args.cmd == "update":
            since = utc_today() - timedelta(days=UPDATE_DAYS)
            return cmd_fetch(coins, lambda c: since, data_dir)
        return print_status(check(coins, data_dir))
    except Exception as exc:
        print(f"DATA: ERROR {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
