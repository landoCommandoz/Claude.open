"""Tests for scripts/data.py. No network: requests are faked."""
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import data

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2024, 3, 10)


def real_btc():
    return json.loads((FIXTURES / "coinbase_candles_BTC_2024-01-01_03.json").read_text())


def good_frame(end: date, days: int = 70) -> pd.DataFrame:
    rows = []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        rows.append({"date": d.isoformat(), "open": 100.0, "high": 110.0, "low": 90.0,
                     "close": 105.0, "volume": 1.0})
    return pd.DataFrame(rows, columns=data.COLUMNS)


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


def test_field_order_pinned_on_real_response():
    df = data.parse_candles(real_btc())
    jan2 = df[df["date"] == "2024-01-02"].iloc[0]
    assert (jan2["low"], jan2["high"], jan2["open"], jan2["close"]) == (44195.13, 45925.82, 44220.78, 44972.8)
    jan1 = df[df["date"] == "2024-01-01"].iloc[0]
    assert jan1["close"] == jan2["open"]  # consecutive days chain: proves open/close are not swapped


def test_normalize_sorts_ascending_and_drops_today():
    df = data.normalize(data.parse_candles(real_btc()), today=date(2024, 1, 3))
    assert list(df["date"]) == ["2024-01-01", "2024-01-02"]


def test_merge_keeps_newest_row_for_a_date():
    old = good_frame(TODAY - timedelta(days=1), 5)
    new = old.tail(1).copy()
    new["close"] = 107.0
    merged = data.merge(old, new, today=TODAY)
    assert len(merged) == 5 and merged["close"].iloc[-1] == 107.0


def test_validate_ok_and_each_failure():
    y = TODAY - timedelta(days=1)
    assert data.validate(good_frame(y), TODAY) == []
    assert data.validate(pd.DataFrame(columns=data.COLUMNS), TODAY) == ["no data"]
    bad = good_frame(y)
    bad.loc[5, "high"] = 50.0
    assert "high below open or close" in data.validate(bad, TODAY)
    bad = good_frame(y)
    bad.loc[5, "low"] = 200.0
    assert "low above open or close" in data.validate(bad, TODAY)
    bad = good_frame(y)
    bad.loc[5, "close"] = 0.0
    assert "non-positive or missing price" in data.validate(bad, TODAY)
    gap = good_frame(y).drop(index=65).reset_index(drop=True)
    assert any("missing day" in r for r in data.validate(gap, TODAY))
    old = good_frame(y - timedelta(days=1))
    assert any("last date" in r for r in data.validate(old, TODAY))


def test_fetch_pages_300_days_back_and_covers_range():
    start, end = date(2023, 1, 1), date(2024, 12, 31)   # 731 days -> 3 pages
    session = FakeSession([FakeResp(200, [])] * 3)
    data.fetch_range("BTC", start, end, session=session, sleep=lambda s: None)
    windows = [(p["start"][:10], p["end"][:10]) for _, p, _ in session.calls]
    assert windows[0][1] == end.isoformat() and windows[-1][0] == start.isoformat()
    for s, e in windows:
        assert (date.fromisoformat(e) - date.fromisoformat(s)).days + 1 <= data.PAGE_DAYS
    assert all(t == data.TIMEOUT_SEC for _, _, t in session.calls)


def test_get_json_retries_then_raises():
    waits = []
    session = FakeSession([FakeResp(500, {"message": "down"})] * 4)
    with pytest.raises(data.DataError):
        data.get_json("u", session=session, sleep=waits.append)
    assert len(session.calls) == 4
    assert [w for w in waits if w >= 1] == [1, 3, 9]


def test_get_json_recovers_after_one_failure():
    session = FakeSession([FakeResp(429, {"message": "slow"}), FakeResp(200, [1])])
    assert data.get_json("u", session=session, sleep=lambda s: None) == [1]


def test_error_payload_is_not_parsed_as_candles():
    with pytest.raises(data.DataError):
        data.parse_candles({"message": "NotFound"})


def test_save_is_lf_and_check_writes_status(tmp_path):
    y = TODAY - timedelta(days=1)
    data.save("BTC", good_frame(y), tmp_path)
    data.save("ETH", good_frame(y - timedelta(days=3)), tmp_path)
    raw = (tmp_path / "BTC.csv").read_bytes()
    assert b"\r" not in raw and raw.startswith(b"date,open,high,low,close,volume\n")
    status = data.check(["BTC", "ETH", "SOL"], tmp_path, TODAY)
    saved = json.loads((tmp_path / "status.json").read_text())
    assert saved["coins"]["BTC"]["status"] == "OK"
    assert saved["coins"]["ETH"]["status"] == "STALE"
    assert status["coins"]["SOL"]["reasons"] == ["no data"]
    assert not list(tmp_path.glob("*.tmp"))
