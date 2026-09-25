"""
test_strategy.py | Acceptance tests for the money math.
Run: python test_strategy.py   (or: python -m pytest test_strategy.py)
Every test must pass before the agent is allowed to trade.
"""
import math

import pandas as pd

from strategy import (Params, drawdown_multiplier, indicators, initial_stop, model_bid, ratchet_stop,
                      round_down, should_ratchet, size_position, stop_fill, wilder, worst_case_loss)
from backtest import simulate, metrics

P = Params()


def frame(closes):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": [float(c) for c in closes]}, index=idx)


def test_wilder_constant_series_stays_constant():
    s = pd.Series([2.0] * 30)
    w = wilder(s, 20)
    assert math.isnan(w.iloc[18]) and w.iloc[19] == 2.0 and w.iloc[-1] == 2.0


def test_wilder_seed_is_simple_average_then_smooths():
    s = pd.Series([1.0, 2.0, 3.0, 10.0])
    w = wilder(s, 3)
    assert w.iloc[2] == 2.0
    assert abs(w.iloc[3] - (2.0 * 2 + 10.0) / 3) < 1e-12


def test_signal_needs_breakout_and_trend():
    closes = [100.0] * 60 + [101.0]
    ind = indicators(frame(closes), P)
    assert bool(ind["signal"].iloc[-1]) is True
    assert bool(ind["signal"].iloc[-2]) is False
    falling = [200.0 - i for i in range(60)] + [150.0]
    ind2 = indicators(frame(falling), P)
    assert bool(ind2["signal"].iloc[-1]) is False  # beats 20-day high? no. Below SMA anyway.


def test_signal_false_during_warmup():
    ind = indicators(frame([100 + i for i in range(40)]), P)
    assert not ind["signal"].any()


def test_initial_stop_math_and_invalid_inputs():
    assert initial_stop(100.0, 3.0, P) == 94.0
    assert math.isnan(initial_stop(100.0, float("nan"), P))
    assert math.isnan(initial_stop(0.0, 3.0, P))
    assert math.isnan(initial_stop(100.0, -1.0, P))


def test_ratchet_never_lowers():
    assert ratchet_stop(94.0, 90.0) == 94.0
    assert ratchet_stop(94.0, 97.0) == 97.0
    assert ratchet_stop(94.0, float("nan")) == 94.0


def test_should_ratchet_needs_a_quarter_n_raise():
    assert P.min_ratchet_n == 0.25
    assert should_ratchet(90.0, 91.0, 4.0, 0.25) is True          # exactly 0.25N higher
    assert should_ratchet(90.0, 90.99, 4.0, 0.25) is False        # just under 0.25N
    assert should_ratchet(90.0, 89.0, 4.0, 0.25) is False         # never down
    assert should_ratchet(90.0, 90.0, 4.0, 0.0) is False          # no raise, even with no minimum
    assert should_ratchet(90.0, 90.01, 4.0, 0.0) is True
    assert should_ratchet(90.0, float("nan"), 4.0, 0.25) is False
    assert should_ratchet(90.0, 95.0, 0.0, 0.25) is False         # no valid N, no raise


def test_stop_triggers_on_the_modeled_bid_and_fills_at_the_stop():
    assert abs(model_bid(100.0, 0.01) - 99.0) < 1e-12
    # low 100.5: bid at the low 99.495 is above a 99.4 stop, so no trigger
    assert stop_fill(102.0, 100.5, 99.4, 0.01) is None
    # low 100.3: bid at the low 99.297 reaches the 99.4 stop, fill at the stop
    assert stop_fill(102.0, 100.3, 99.4, 0.01) == 99.4
    # open 100.0: bid at the open 99.0 is already below the 99.4 stop, fill at the open's bid
    assert abs(stop_fill(100.0, 98.0, 99.4, 0.01) - 99.0) < 1e-12
    assert stop_fill(100.0, 98.0, float("nan"), 0.01) is None


def test_drawdown_multiplier_steps():
    assert drawdown_multiplier(400, 400, P) == 1.0
    assert drawdown_multiplier(360.1, 400, P) == 1.0
    assert abs(drawdown_multiplier(360, 400, P) - 0.8) < 1e-12
    assert abs(drawdown_multiplier(320.5, 400, P) - 0.8) < 1e-12
    assert abs(drawdown_multiplier(320, 400, P) - 0.6) < 1e-12
    assert drawdown_multiplier(0, 400, P) == 0.0


def test_round_down_never_rounds_up():
    assert round_down(1.23456, 0.001) == 1.234
    assert round_down(0.0009, 0.001) == 0.0
    assert round_down(-1, 0.001) == 0.0


def test_size_400_account_risks_four_dollars():
    s = size_position(equity=400, peak=400, cash=400, fill=100, stop=94,
                      open_risk=0, open_positions=0, p=P)
    assert s.ok and abs(s.risk - 4.0) < 1e-9 and abs(s.notional - 400 * 0.01 / 6 * 100) < 1e-9


def test_size_is_capped_at_30_percent_of_equity():
    s = size_position(equity=400, peak=400, cash=400, fill=100, stop=96.5,
                      open_risk=0, open_positions=0, p=P)
    assert s.ok and abs(s.notional - 114.2857142857) < 1e-6
    s2 = size_position(equity=1000, peak=1000, cash=1000, fill=100, stop=96.9,
                       open_risk=0, open_positions=0, p=P)
    assert s2.ok and abs(s2.notional - 300.0) < 1e-9


def test_size_refusals():
    base = dict(equity=400, peak=400, cash=400, fill=100, open_risk=0, open_positions=0, p=P)
    assert size_position(**{**base, "stop": 100}).reason == "stop at or above entry"
    assert size_position(**{**base, "stop": 98}).reason == "stop too tight for spread costs"
    assert size_position(**{**base, "stop": 94, "open_positions": 3}).reason == "max positions reached"
    assert size_position(**{**base, "stop": 94, "open_risk": 12}).reason == "open risk limit reached"
    assert size_position(**{**base, "stop": 94, "cash": 25}).reason == "position below minimum size"
    assert size_position(**{**base, "stop": float("nan")}).reason == "invalid stop"
    bad_count = size_position(**{**base, "stop": 94, "open_positions": True})
    assert bad_count.reason == "invalid open_positions"


def test_size_shrinks_in_drawdown():
    s = size_position(equity=350, peak=400, cash=350, fill=100, stop=94,
                      open_risk=0, open_positions=0, p=P)
    assert s.ok and abs(s.risk - 0.01 * 350 * 0.8) < 1e-9


def choppy_base(days=60):
    """Realistic noise so N is meaningful: closes alternate 97 and 103."""
    return [97.0 if i % 2 == 0 else 103.0 for i in range(days)]


def test_backtest_trend_makes_money_and_flat_does_nothing():
    up = choppy_base() + [104.0 + 2.0 * i for i in range(60)]
    res = simulate({"X": frame(up)}, P, start="2024-01-01", end=None, cost_side=0.006,
                   start_equity=400, floor=320)
    assert not res["trades"] and res["open_positions"] == 1
    assert res["curve"]["equity"].iloc[-1] > 400
    flat = simulate({"X": frame([100.0] * 150)}, P, start="2024-01-01", end=None,
                    cost_side=0.006, start_equity=400, floor=320)
    assert flat["curve"]["equity"].iloc[-1] == 400 and not flat["trades"]


def test_backtest_tiny_volatility_is_refused_by_cost_filter():
    creep = [100.0] * 60 + [100.0 * (1.01 ** i) for i in range(1, 40)]
    res = simulate({"X": frame(creep)}, P, start="2024-01-01", end=None, cost_side=0.006,
                   start_equity=400, floor=320)
    assert res["open_positions"] == 0 and res["skipped"].get("stop too tight for spread costs", 0) > 0


def test_backtest_stop_out_loses_about_one_r_after_costs():
    closes = choppy_base() + [110.0, 97.0] + [97.0] * 5
    res = simulate({"X": frame(closes)}, P, start="2024-01-01", end=None, cost_side=0.006,
                   start_equity=400, floor=320)
    t = res["trades"][0]
    assert t.pnl < 0 and t.cost > 0 and -1.3 < t.r < -1.0
    m = metrics(res, 400)
    assert m["trades"] == 1 and 390 < m["final_equity"] < 400


def test_floor_halts_new_entries_after_a_crash_through_the_stop():
    closes = choppy_base() + [110.0, 60.0] + [110.0, 97.0] * 20
    res = simulate({"X": frame(closes)}, P, start="2024-01-01", end=None, cost_side=0.006,
                   start_equity=400, floor=395, gap_frac=0.05)
    assert res["halted_on"] is not None and len(res["trades"]) == 1


def test_worst_case_loss_math():
    assert abs(worst_case_loss(2.0, 100.0, 90.0, 0.05) - 2.0 * (100.0 - 85.5)) < 1e-9
    assert worst_case_loss(0.0, 100.0, 90.0, 0.05) == 0.0
    assert worst_case_loss(1.0, float("nan"), 90.0, 0.05) == 0.0


def test_floor_buffer_caps_size_near_the_floor():
    s = size_position(equity=324, peak=400, cash=324, fill=100, stop=94, open_risk=0,
                      open_positions=0, p=P, floor=320, gap_frac=0.05, open_worst_case=0)
    assert s.ok and abs(s.notional - 4.0 / (100 - 94 * 0.95) * 100) < 1e-9
    worst_if_crash = s.qty * (100 - 94 * 0.95)
    assert 324 - worst_if_crash >= 320 - 1e-9


def test_floor_buffer_refuses_when_no_room():
    base = dict(peak=400, fill=100, stop=94, open_risk=0, open_positions=0, p=P,
                floor=320, gap_frac=0.05)
    full = "floor buffer reached"
    assert size_position(equity=320, cash=320, open_worst_case=0, **base).reason == full
    assert size_position(equity=330, cash=330, open_worst_case=12, **base).reason == full
    assert size_position(equity=400, cash=400, open_worst_case=0, **{**base, "gap_frac": 0.7}).reason \
        == "invalid gap_frac"


def test_floor_buffer_does_not_bind_far_above_the_floor():
    a = size_position(equity=400, peak=400, cash=400, fill=100, stop=94, open_risk=0,
                      open_positions=0, p=P, floor=320, gap_frac=0.05, open_worst_case=0)
    b = size_position(equity=400, peak=400, cash=400, fill=100, stop=94, open_risk=0,
                      open_positions=0, p=P)
    assert a.ok and b.ok and abs(a.notional - b.notional) < 1e-9


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print(f"PASS {n}")
    print(f"{len(names)} tests passed")
