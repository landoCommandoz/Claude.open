# Trend Desk changelog

## 1.3 - 2026-09-25: the live stop rules move into strategy.py

Approved by Lando (Option 1, fixed at the source).

### What changed
- `strategy.py` gains `should_ratchet(current_stop, new_level, n, min_ratchet_n)`. A stop rises only when the
  new level is at least `min_ratchet_n` x N higher. The engine and the backtest both call it.
- `min_ratchet_n` (0.25) moves from `execution` to `strategy` in `config/risk.json`, so `Params` loads it.
- `strategy.py` gains `model_bid(price, cost_side)` and `stop_fill(open, low, stop, cost_side)`. Stops trigger
  on Robinhood's bid, modeled as price x (1 - cost_per_side). A stop triggers when the bid at the day's low
  reaches it and fills at the stop price. On a gap (the bid at the open is already at or below the stop) it fills
  at the open's bid. This is the default because it is the conservative case. It changes only after Phase 1
  proves how Robinhood triggers crypto stops, with the evidence shown to Lando first.
- `backtest.py` uses both functions. `reference/` and `scripts/` were updated together. Checksums match:
  `strategy.py 19e0e218229f`, `backtest.py 84b00b4224c9`, `test_strategy.py a42a1ec8293e`, `gate.py eaa8f51273fe`
  (unchanged).
- `test_strategy.py` gains tests for `should_ratchet` and the bid stop trigger (20 tests, now 22).
- The parity simulator triggers stops with the same `stop_fill`. Parity is exact: 31 of 31 closed trades in the
  last 400 days, the same 2 open positions, end equity within one cent. The count changed from 28 because
  bid-triggered stops stop out more often. The test pins 31.
- If the last exit attempt does not fill, the desk cancels it, puts the stop back at its old level, and only
  then stops the run. If that stop is rejected twice, the desk pauses instead of looping.
- Robinhood schema facts from Sept 25 (see `setup/ROBINHOOD_FACTS.md`): order numbers go out as decimal
  strings cut to the increment; quote symbols like `BTCUSD` are read as the coin; the guard denies any call with
  both quantity and dollar_amount; every number is parsed with Decimal; `get_portfolio` is called with
  `account_number`, which the desk now records beside `rhs_account_number`. Raw response mappings still wait
  for real Phase 1 fixtures.

### Why
Gate A's parity test found two exits one day apart from the backtest. Both came from live rules the backtest did
not model: the 0.25N minimum stop raise, and the fact that Robinhood's bid sits about 0.95% below the market
price, so a stop near the price fires sooner live. Moving both rules into `strategy.py` makes the backtest and the
live desk run the same code.

### Gate 0, same data (Coinbase daily candles through 2026-09-24)

| | Before (1.2) | After (1.3) |
|---|---|---|
| Ending balance, $400 from 2020-01-01 | $1,044.13 | $905.05 |
| Growth per year | 15.3% | 12.9% |
| Worst drop from a high | -13.2% | -13.4% |
| Since 2022: ending balance / growth per year | $619.85 / 9.7% | $558.81 / 7.3% |
| Trades (per month) | 175 (2.17) | 196 (2.43) |
| Winning trades | 44.6% | 41.3% |
| Profit factor / expectancy | 2.09 / 0.648R | 1.83 / 0.482R |
| Stress profit factor (1.25% per side) | 1.89 | 1.68 |
| First year, 23 start dates: worst / median / best | $389.40 / $486.68 / $579.57 | $381.95 / $471.81 / $573.83 |
| Ended the first year below $400 | 4 of 23 | 4 of 23 |
| Hit the $320 floor | 0 of 23 | 0 of 23 |
| Gate | PASS | PASS |

Yearly after: 2020 +28.7%, 2021 +27.5%, 2022 -5.0%, 2023 +21.9%, 2024 +20.9%, 2025 +0.5%, 2026 -2.0%.
