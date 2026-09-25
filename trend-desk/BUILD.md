# BUILD.md: BUILD, VERIFY, AND LAUNCH THE TREND DESK

Who runs this: Claude Code, in an interactive session, with Lando at the keyboard.

How Lando starts it: open Claude Code in this folder and type:
"Read CLAUDE.md and BUILD.md. Execute BUILD.md step by step. Stop at every GATE, show me the result, and wait for me."

## 0. RULES FOR THE BUILD

- Build exactly what this file specifies. If something is impossible on this machine or with the live Robinhood tools, stop and explain. Do not substitute your own design.
- Python 3.11 or newer. Dependencies: pandas, requests, pytest. Nothing else.
- The files in reference/ are finished and tested: strategy.py, backtest.py, gate.py, test_strategy.py. Copy them into scripts/ (tests into tests/) unchanged. strategy.py is the only place signal, stop, and sizing math may live. Everything else imports it. Never reimplement it.
- Every file stays under 300 lines. Split by responsibility if needed.
- All timestamps are UTC, ISO 8601. Human-facing reports also show America/Denver time.
- Every state write is atomic: write a temp file in the same folder, flush, fsync, rename.
- Fail closed. If anything that decides whether money moves hits an error, money does not move.
- Use whichever interpreter command works here (python3, python, or py) and use it everywhere, including prompts and settings. If it is not python3, update prompts/ and CLAUDE.md before lock-down and tell Lando.
- No secrets in any file. The Robinhood login lives in Claude Code's own MCP auth, never in this project.
- This desk runs on Windows. Read setup/WINDOWS.md before writing runner.py, watcher.py, or anything that touches files, processes, or the scheduler.

## 1. FOLDER LAYOUT

    trend-desk/
      CLAUDE.md                 agent rulebook (read-only after lock-down)
      BUILD.md                  this file
      config/risk.json          every number the system uses (read-only to the agent)
      config/tools.json         Robinhood tool map, built in Phase 1 (read-only after)
      prompts/daily.md          the three run prompts
      prompts/reconcile.md
      prompts/weekly.md
      reference/                the four tested reference files
      setup/settings.template.json   locked permissions and hooks, used in Phase 2
      setup/tools.template.json      the nine Robinhood tools the desk may touch, used in Phase 1
      setup/ROBINHOOD_FACTS.md       what a live check of the Robinhood tools showed
      setup/WINDOWS.md               Windows-specific build and scheduling rules
      START_HERE.txt            Lando's one-page quick start
      scripts/strategy.py       copied from reference/, unchanged
      scripts/backtest.py       copied from reference/, unchanged
      scripts/gate.py           copied from reference/, unchanged
      scripts/data.py           public market data
      scripts/capture.py        PostToolUse hook: records every Robinhood tool result
      scripts/broker.py         turns recorded results into clean objects
      scripts/guard.py          PreToolUse hook: blocks any order without an approval
      scripts/state.py          state file reads and atomic writes
      scripts/engine.py         the brain: preflight, plan, confirm, close, weekly
      scripts/alert.py          push alerts to Lando's phone
      scripts/watcher.py        runs every 5 minutes, no AI, decides when to wake the agent
      scripts/runner.py         starts Claude Code headless with a lock and a timeout
      tests/                    all tests plus fixtures from real Robinhood responses
      data/                     daily OHLC files, one per coin
      state/                    state.json, approvals.json, journal.csv, events.jsonl, tape/, LESSONS.md
      reports/daily/  reports/weekly/
      logs/runs/
      STATUS.md                 one-glance dashboard, rewritten after every run
      .claude/settings.json     permissions and hooks, installed in Phase 2

## 2. COMPONENTS

### 2.1 scripts/data.py (public data, no keys, no AI)

- Source: Coinbase Exchange public API, no key needed.
  Candles: GET https://api.exchange.coinbase.com/products/{COIN}-USD/candles?granularity=86400&start={iso}&end={iso}
  Each bucket is [time, low, high, open, close, volume], newest first, at most 300 per request. Verify the field order against a live call and pin it with a test on a saved real response before trusting it.
  Ticker: GET https://api.exchange.coinbase.com/products/{COIN}-USD/ticker
- Commands:
  backfill --since 2019-10-01   all universe coins, paging back 300 days at a time
  update                        refetch the last 10 days and merge
  price --coin BTC              prints {"coin","price","time"} as JSON
  check                         validates every file, prints last date and status per coin
- Output: data/{COIN}.csv with columns date,open,high,low,close,volume, ascending, unique dates.
- Only completed UTC days. Never write today's partial candle.
- Validation per coin: prices positive, high >= max(open, close), low <= min(open, close), no missing day in the last 60 days, last date equals yesterday UTC. A failing coin is marked STALE in data/status.json. The engine refuses entries on STALE coins but keeps managing exits.
- Network: 0.4 seconds between requests, 3 retries with 1, 3, 9 second backoff, 15 second timeout.
- Coins that listed after 2020 (SOL, DOGE on Coinbase) backfill from their first date. That is fine.

### 2.2 scripts/capture.py (PostToolUse hook: the tape)

- Registered for matcher mcp__robinhood-trading__.* in .claude/settings.json.
- Reads the hook JSON from stdin: tool_name, tool_input, and the tool's output. The docs call the output field tool_response. Confirm the real key in Phase 1: on its first run, write the sorted list of stdin keys to state/tape/_keys.json.
- Writes state/tape/{utc_timestamp}_{short_tool_name}.json containing {ts, tool_name, tool_input, tool_response}. Also overwrites state/tape/latest_{short_tool_name}.json. Appends {ts, type: "tape", tool} to state/events.jsonl.
- When the result is an order (placement or status) and broker.py can read its id, also write state/tape/order_{id}.json.
- Never blocks anything. Always exits 0 with nothing on stdout. Errors go to state/capture_errors.log.
- Deletes tape files older than 30 days. Never deletes latest_* or order_* files for open orders.

### 2.3 scripts/broker.py (the only code that reads Robinhood responses)

- Reads the tape and returns clean objects. No other file parses raw Robinhood output.
  account(max_age)        {equity, cash, buying_power, ts}
  positions(max_age)      {coin: qty} for crypto held in the Agentic account
  open_orders(max_age)    list of Order
  recent_orders(max_age)  list of Order from the order history call
  order(order_id, after)  one Order, from a status call made after a given time
  quotes(max_age)         {coin: {bid, ask, ts}}
- Order fields: id, coin, side (buy or sell), type (limit, market, stop_market, stop_limit), qty, filled_qty, avg_fill_price, limit_price, stop_price, status (open, filled, partially_filled, canceled, rejected, pending), created_at, updated_at, reject_reason.
- Tape older than max_age raises StaleTape. A missing or unreadable required field raises TapeError. No defaults. No guessing.
- Field mappings come from real responses captured in Phase 1. Every response shape seen gets saved to tests/fixtures/ with a test that parses it.

### 2.4 scripts/guard.py (PreToolUse hook: nothing trades without an approval)

- Registered for matcher mcp__robinhood-trading__.* in .claude/settings.json.
- Reads tool_name and tool_input from stdin. Looks the tool up in config/tools.json. Classes:
  read, preview    allowed. Exit 0 with no output, so normal permissions apply.
  place, cancel    allowed only with a matching approval (below).
  forbidden        always denied. Anything that moves money or crypto, changes settings, or trades stocks or options.
  not in the map   always denied.
- Matching approval, from state/approvals.json. All must hold:
  same coin, side, and order type
  quantity within 0.1 percent (or notional within 0.1 percent if the tool takes dollars)
  limit and stop prices within 0.05 percent
  for cancels, the exact order id
  not expired (expires_at), not used, and the approval's own mode stamp is LIVE or TEST.
  The guard never reads the mode in config. It trusts only the stamp the engine wrote on the approval.
  rhs_account_number equals the one agentic account recorded in state.json
  ref_id equals the approval's ref_id
  for stop orders (stop_loss or stop_limit): time_in_force is exactly "gtc"
- On a match: mark the approval used (atomic write, with a file lock), exit 0 with no output.
- On a deny: exit 0 and print exactly
  {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"<short reason>"}}
  Use this JSON form, not exit code 2. Public bug reports show exit code 2 failing to block in some versions, and the docs state a deny decision holds in every permission mode.
- An approval stamped DRY is always denied. In DRY mode the engine writes no approvals at all, since DRY plans use preview tools.
- If a file named STOP exists in the project root: deny every approval of kind BUY. Stops, exits, and cancels that protect positions still pass.
- Any exception anywhere in the guard: deny, reason "guard error".
- Log every decision (allow or deny, tool, reason) to state/events.jsonl.
- guard.py --selftest feeds itself an unapproved place call and a forbidden call and must print DENY twice. runner.py runs this before every session.

### 2.5 scripts/state.py

- Load and save state.json, approvals.json, journal.csv, events.jsonl with schema checks and atomic writes.
- File locking: fcntl on macOS and Linux, msvcrt on Windows.
- Journal is append-only. No function exists that rewrites or deletes rows.

### 2.6 scripts/engine.py (the brain)

Every command prints human-readable lines, then one final line. The agent only ever acts on what engine.py prints.

preflight --job DAILY|RECONCILE|WEEKLY
- Validates config/risk.json (all keys present, ranges sane), state.json, tools.json.
- Runs tests/test_strategy.py in a subprocess. Any failure: state PAUSED, alert, print NOT_READY.
- Runs guard.py --selftest. Failure: PAUSED, alert, NOT_READY.
- Prints the exact Robinhood read calls to make, in order, with exact arguments: account, crypto positions, open orders, order history for the last 7 days, quotes for every universe coin and every held coin.
- WEEKLY lists no read calls.
- Last line: READY or NOT_READY <reason>. HALTED, PAUSED, or a STOP file still print READY, because protection must keep running. The plan enforces what is allowed.

plan --job DAILY|RECONCILE
Runs these steps in this order. Each step can add actions. Every order action comes with an approval written to approvals.json.
1. Tape check. Every read listed in preflight must be on the tape and younger than tape_max_age_sec. If not, print "CALL READ TOOLS FIRST" and exit with no actions.
   Hook check. Every read on the tape this run must have a matching guard decision in state/events.jsonl. No guard entries means the guard hook is not running: PAUSED, no orders, alert.
   Account check. get_accounts must show exactly one account the agent may trade (agentic_allowed true). Its rhs_account_number is the only account any order may use. Record it in state.json on first run. If it ever changes or a second one appears: PAUSED.
2. Reconcile positions (both jobs):
   a. Coin held at Robinhood and in state: quantities must match within dust_qty_frac. If Robinhood holds less, update state and resize the stop.
   b. Coin in state but gone at Robinhood: the stop filled. Find the fill in order history, write the journal row (exit reason stop), update realized P&L, remove the position, record the exit date in state.exited. No matching fill found: PAUSED, reason "position vanished".
   c. Coin held at Robinhood but not in state: if an approved BUY for that coin was used in the last 24 hours, adopt it using that order's real fill. Otherwise PAUSED, reason "unknown position", and still protect it with a stop at current bid minus 2N, because no position sits unprotected.
3. Stop coverage (both jobs): every position needs exactly one open stop order at Robinhood, for its full quantity, at the stop in state.
   Missing: action PROTECT (place the stop). If the bid is already at or below that stop: action EXIT instead.
   Wrong quantity or price: cancel it, place the right one.
   Extra stops for the same coin: cancel the extras.
4. Orphans (both jobs): approved buy orders still open past entry_fill_timeout_sec get canceled. Stop orders for coins with no position get canceled. Any open order or filled order in the last 7 days that no approval explains: PAUSED, reason "order not placed by desk". Do not cancel those. Lando decides.
5. Risk state (both jobs): equity from the account read. Equity at or below floor_equity: HALTED. Realized loss today at or past daily_loss_limit_frac of the day's start equity: COOLDOWN_DAY. Same for the week and weekly_loss_limit_frac: COOLDOWN_WEEK. Peak equity only updates in close.
6. DAILY only. Data check: every coin's last candle must be yesterday UTC. STALE coins get no entries.
7. DAILY only. Ratchets: for each position, new level = strategy.ratchet_stop(stop, exit_level from yesterday's candle). Only act if the new level is at least min_ratchet_n times N above the current stop. Action sequence: CANCEL old stop, then PLACE new stop. If the bid is already at or below the new level: EXIT instead.
8. DAILY only. Entries, only if state allows (not HALTED, PAUSED, or in cooldown), mode is LIVE or DRY, and no STOP file:
   a. Signals from strategy.indicators on completed candles: signal true on yesterday's candle.
   b. Skip coins already held, coins exited yesterday, STALE coins.
   c. Rank by momentum, strongest first.
   d. Quote checks, each with a logged reason when it fails: spread (ask minus bid, over mid) at most max_spread_frac. Robinhood mid within price_check_tolerance_frac of the public price. Ask at most signal close plus chase_limit_n times N.
   e. Limit price = ask times (1 + entry_limit_slippage_frac), rounded to the coin's price increment.
   f. Stop = strategy.initial_stop(limit price, N). Size = strategy.size_position(equity, peak, cash, fill = limit price, stop, open risk, open positions, floor = floor_equity, gap_frac = floor_gap_allowance_frac, open_worst_case = the sum of strategy.worst_case_loss over every open position at the current bid). That last part is the worst-case floor: every trade is sized so the account stays at or above floor_equity even if every stop, new and open, fills 5 percent below its price. Quantity rounded down with strategy.round_down to the coin's quantity increment. Recompute notional and risk after rounding. Below min_notional or below Robinhood's minimum order: skip.
   g. Update cash, open risk, and open positions in memory before sizing the next candidate, so one plan can never overspend.
9. DAILY only. Stop refresh: any stop older than stop_refresh_days gets the same cancel-then-place sequence at the same price, because good-till-canceled crypto stops at Robinhood expire after 90 days.
10. Every order action's args include: the agentic account's rhs_account_number, a fresh UUID ref_id stored in its approval, and for stops time_in_force "gtc". Robinhood defaults stops to good-for-day. A stop sent without gtc dies at the end of the day and leaves the position naked.
11. Write approvals with expires_at = now + approval_ttl_sec. Save the plan to state/plans/{ts}.json. Print the plan JSON.

Plan JSON shape:
    {"job":"DAILY","mode":"LIVE","state":"SCANNING","actions":[
      {"id":"A1","kind":"BUY","coin":"BTC","tool":"<exact tool name>","args":{...exact...},
       "status_tool":"<exact tool name>","status_args":{...},"why":"close 71234.50 above 20-day high 70980.00, N 2410.30"}],
     "skipped":[{"coin":"DOGE","reason":"spread 1.9% above 1.5%"}],"notes":[]}
Action kinds: BUY, PROTECT, CANCEL, RATCHET_CANCEL, RATCHET_PLACE, EXIT.
Protective actions always come first in the list: PROTECT and EXIT, then cancels and ratchets, then BUYs.
In DRY mode every order action becomes a PREVIEW action that calls the preview tool with the same arguments, and no approvals are written.

confirm --action ID
Reads the order status captured after the action was issued. Prints exactly one of:
  NEXT                  done, go to the next action
  WAIT <seconds>        run engine.py wait <seconds>, call the status tool again, confirm again
  NEW ACTION <json>     execute this action now, then confirm it
  RETRY                 the engine issued a fresh approval with the SAME ref_id, repeat this same action once.
                        Same ref_id means Robinhood drops the duplicate if the first attempt went through.
  STOP                  stop executing actions, go to close
Rules by kind:
  BUY filled: record the position at the real average fill and filled quantity. Stop = strategy.initial_stop(avg fill, N). NEW ACTION PROTECT.
  BUY partially filled past the timeout: NEW ACTION CANCEL the remainder, then PROTECT the filled quantity.
  BUY open inside the timeout: WAIT 20. Past the timeout with nothing filled: NEW ACTION CANCEL.
  BUY rejected: incident, NEXT.
  PROTECT or RATCHET_PLACE live with the right quantity and stop price: NEXT.
  PROTECT rejected: RETRY while retries are under stop_retry_limit. After that: NEW ACTION EXIT, state PAUSED, alert.
  RATCHET_PLACE rejected twice: NEW ACTION PROTECT at the old stop. If that fails too: NEW ACTION EXIT, PAUSED, alert.
  CANCEL confirmed: NEXT, or the next step of its sequence.
  Any stop or EXIT that fills more than stop_slip_pause_frac below its stop price: journal row, incident, state PAUSED with reason "stop slipped past allowance", alert. A slip that big means the market is breaking, and a human looks before the desk buys again.
  EXIT filled: journal row, NEXT. Not filled in time: cancel and reprice lower by exit_reprice_step_frac, up to exit_reprice_attempts. Then a last attempt at bid minus 5 percent, incident, alert.
  Status unknown three times in a row: STOP, incident, alert.

close --job DAILY|RECONCILE|WEEKLY
- Updates peak equity, rolls day and week counters, writes reports/daily/{date}.md on DAILY, rewrites STATUS.md, sends the daily summary alert on DAILY, appends to state/runs.jsonl.
- Last line is the status line, one JSON object:
  {"job":"DAILY","state":"SCANNING","mode":"LIVE","equity":412.30,"day_pnl":1.20,"week_pnl":3.05,"open_positions":1,"open_risk":4.00,"floor":320,"trades_today":0,"incidents_today":0,"next_daily_utc":"2026-10-01T00:07:00Z"}

incident --text "<text>"      appends to state/incidents.jsonl and events. Alerts when the text mentions guard, stop, unknown, reject, or pause.
weekly                         prints the week and since-inception numbers: trades, win rate, average R, expectancy, spread paid, slippage, stop confirmation time, incidents, guard blocks, equity change, max drawdown, and the same numbers from the Phase 0 backtest for comparison. Drift alarm: once there are 30 or more closed live trades, a negative live expectancy sets PAUSED, reason DRIFT.
status                         prints the status line and rewrites STATUS.md.
wait <seconds>                 sleeps, 60 seconds maximum. Lets the agent wait for fills without shell access.
clear --reason "<text>"        human only. Refuses when stdin is not a terminal or when TREND_DESK_RUNNER is set. Asks Lando to type CLEAR DESK. Clears PAUSED or HALTED, logs who, when, and why.
test-approval                  Phase 1 only. Issues one approval stamped TEST for exactly the guard-test order: a limit buy worth at most $5, priced at or below 60 percent of the current bid so it cannot fill. Refuses once mode is LIVE, and refuses any other order shape. With --cancel <order id> it issues the one TEST cancel approval for that same order.

### 2.7 scripts/alert.py (push alerts, optional but recommended)

- Uses ntfy.sh: POST https://ntfy.sh/{topic} with a title and a short message. Lando installs the free ntfy app and subscribes to the topic.
- During setup, generate the topic as 32 random letters and digits and have Lando paste it into config/risk.json. Anyone with the topic can read it, so messages never contain order ids, account details, or tokens. Only state, equity, P&L, coin names, and a short reason.
- Empty topic: alerts only go to state/alerts.log.
- The same alert text sends at most once per hour.
- Sends on: HALTED, PAUSED, any incident flagged for alert, a failed or missed DAILY run, a stale watcher, an expired Robinhood login, and the DAILY summary line.

### 2.8 scripts/watcher.py (every 5 minutes, no AI, costs nothing)

Each tick:
1. If the runner lock is held, exit.
2. Write state/watcher_heartbeat.json.
3. Stop breach check, for each position in state: public price from data.py. If the price is below the recorded stop by more than 0.5 percent, the stop should already have filled. Start runner.py RECONCILE, at most once per 30 minutes per coin.
4. DAILY trigger: at or after schedule.daily_run_utc with no successful DAILY for today, start runner.py DAILY. After a failure, retry every 30 minutes, three attempts, then alert and wait for the next day.
5. WEEKLY trigger: on weekly_review_day, after that day's DAILY succeeds, start runner.py WEEKLY once.
6. Health: no successful DAILY by missed_daily_alert_utc, alert. Last successful run older than stale_run_alert_hours, alert.
A STOP file does not stop the watcher. Protection keeps running.

### 2.9 scripts/runner.py (starts Claude Code headless)

- Usage: runner.py DAILY|RECONCILE|WEEKLY
- Lock file state/runner.lock holding pid and start time. Older than 45 minutes counts as stale: break it and alert.
- Before starting Claude: guard self-test and config validation. Either failing: PAUSED, alert, do not start Claude.
- Command, verified against claude --help on this machine in Phase 1 and adjusted if flag names differ:
  claude -p "<full text of prompts/{job}.md>" --permission-mode dontAsk --max-turns 60 --output-format json
  dontAsk turns every permission prompt into a denial, so a headless run can only use what settings.json allows.
- Environment: TREND_DESK_RUNNER=1.
- Hard timeout 20 minutes, then kill the whole process tree, mark the run failed, alert.
- Save full output to logs/runs/{ts}_{job}.log.
- Success means: exit code 0, the final output line parses as the status JSON, and engine close wrote this run to state/runs.jsonl.

## 3. FILE FORMATS

state/state.json
    {"schema":1,"state":"SCANNING","reason":"","peak_equity":400.0,
     "day":{"date":"2026-10-01","start_equity":400.0,"realized":0.0},
     "week":{"iso":"2026-W40","start_equity":400.0,"realized":0.0},
     "positions":{"BTC":{"qty":0.00084,"entry_fill":71590.2,"entry_ts":"...","n_at_entry":3104.4,
                         "initial_stop":65381.4,"stop":65381.4,"stop_order_id":"...","entry_order_id":"..."}},
     "exited":{"ETH":"2026-09-30"},
     "last_success":{"DAILY":"...","RECONCILE":"...","WEEKLY":"..."}}
Mode is never stored in state. It is read from config/risk.json every run.

state/approvals.json: list of
    {"id","action_id","kind","coin","side","order_type","qty","notional","limit_price","stop_price",
     "order_id","issued_at","expires_at","used","used_at","mode"}

state/journal.csv columns:
    trade_id, coin, entry_ts, entry_fill, qty, initial_stop, exit_ts, exit_fill, exit_reason, pnl_usd, r_multiple, spread_paid_usd, days_held
exit_reason is one of: stop, trail, emergency, unknown.

config/tools.json, built in Phase 1:
    {"server":"robinhood-trading","python":"python3",
     "tools":{"<full tool name>":{"class":"read|preview|place|cancel|forbidden","role":"account|positions|open_orders|order_history|order_status|quote|crypto_order|cancel_order",
              "args_map":{"coin":"<field>","side":"<field>","order_type":"<field>","qty":"<field>","limit_price":"<field>","stop_price":"<field>","order_id":"<field>"},
              "order_type_values":{"limit":"<value>","stop_market":"<value>","stop_limit":"<value>"}}},
     "stop_order_type":"stop_market or stop_limit",
     "increments":{"BTC":{"qty":0.00000001,"price":0.01}},
     "min_order_usd":{"BTC":1.0}}

## 4. TESTS (ALL MUST PASS BEFORE ANY GATE)

1. tests/test_strategy.py from reference/, unchanged.
2. Guard: unapproved place is denied. Matching approval is allowed and marked used. Same approval used twice is denied. Expired is denied. Quantity off by 1 percent is denied. Price off by 0.2 percent is denied. Unknown tool is denied. Forbidden tool is denied. A forced exception is denied. STOP file denies a buy and allows a protective stop. An approval stamped DRY is denied. A TEST approval passes only for its exact order, and only while config mode is DRY. A stop without time_in_force gtc is denied. A wrong ref_id or account is denied.
3. Capture: writes tape and latest files, never exits non-zero, survives garbage on stdin.
4. Broker: every fixture parses. Stale tape raises. A missing field raises.
5. Engine plan, one test per scenario, using fixtures:
   no signal gives no actions
   near the floor, a signal is sized down by the worst-case floor, or skipped with reason "floor buffer reached"
   a stop that filled 6 percent below its price sets PAUSED
   a clean signal gives one BUY with the exact quantity and limit
   a wide spread skips with the reason
   a position missing its stop gives PROTECT as the first action
   a position gone with a stop fill in history records the exit and gives no actions
   an unknown filled order sets PAUSED and still protects
   equity at the floor sets HALTED, no entries, protection still planned
   daily loss limit sets COOLDOWN_DAY and no entries
   a ratchet above 0.25N gives cancel then place, below it gives nothing
   a bid below the new trail level gives EXIT
   stale tape gives no actions
   a STALE coin gets no entry while exits still work
   a STOP file blocks entries while protection still works
   two signals in one plan never exceed cash, open risk, or max positions
6. Engine confirm: filled BUY gives PROTECT for the exact filled quantity. Partial fill past timeout gives cancel then protect. Stop rejected twice gives EXIT and PAUSED. Failed ratchet puts the old stop back, then exits if that fails too.
7. Watcher: one DAILY per day, RECONCILE on a breach with the 30-minute cooldown, respects the lock.
8. Runner: lock, stale lock break, timeout kill, success detection.
9. Parity: replay the last 400 days through the engine's plan and confirm logic against a simulated broker that fills at the close plus cost. The trades must match backtest.py over the same window exactly: same coins, same entry dates, same exit dates. This proves live logic equals backtest logic.

## 5. PHASES AND GATES

Do the phases in order. Show Lando the result of every gate. Never skip ahead.

### PHASE A: Build
1. Create the folder layout. Copy the reference files. Write every module in section 2.
2. pip install pandas requests pytest
3. Write and run every test in section 4 that does not need real Robinhood data. Broker and plan fixtures come in Phase 1.
GATE A: all tests pass. Show the test summary.

### PHASE 0: Market data and the backtest gate
1. python3 scripts/data.py backfill --since 2019-10-01
2. python3 scripts/data.py check
3. python3 scripts/gate.py --data-dir data --format ohlc --config config/risk.json
GATE 0: the last line must be GATE: PASS.
If it says FAIL, stop and show Lando the full output. Lando decided this in advance: a FAIL shelves the desk as configured. No removing coins, no changing parameters, no changing dates, no rerunning until it passes. Tuning rules until a backtest looks good is how accounts die. The only rerun allowed is after fixing a proven bug or bad data, with the evidence shown to Lando first.

### PHASE 1: Robinhood checks (Lando present)
1. claude mcp list must show robinhood-trading connected. If not, Lando runs, from inside this folder:
   claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
   then /mcp inside Claude Code to log in through the browser. The Agentic account must be funded, with Robinhood Crypto enabled and the crypto agreement accepted in the app.
2. List every tool the server exposes with its description and input schema. Save to state/phase1/tools_raw.json.
3. Build config/tools.json starting from setup/tools.template.json, which lists the nine tools the desk needs as they appeared in a live check. Replace SERVER with the real server name. Every other tool the server exposes is forbidden. Show Lando the table and get his OK.
4. Call each read tool once. Save the raw responses as fixtures. Write the broker.py mappings and their tests.
5. Confirm the PostToolUse output key from state/tape/_keys.json. Fix capture.py if it differs.
6. Order capability, preview only, nothing placed:
   a limit buy of $10 of BTC at the ask
   a stop sell for a small quantity below market, first as stop-market, then as stop-limit
   Record which stop type works in tools.json. If only stop-limit works, the engine sets its limit at the stop price minus 3 percent. If neither works, GATE 1 FAILS. This system never runs without stops resting at Robinhood.
   Record quantity increments, price increments, and minimum order size for every universe coin.
7. Spread survey: quote every universe coin with rhs_account_number set, so prices reflect the account's real routing. Take 5 samples 60 seconds apart now, then 3 samples at 00:07 UTC on three different days, since that is when the desk trades. Median spread above max_spread_frac removes the coin. If the median is above what cost_per_side_assumed covers (2 x 0.95% = 1.9% round trip), raise the cost assumption to match and rerun Gate 0. Costs only ever get corrected upward to match reality, never down to pass a gate.
8. Guard live test, Lando watching:
   a. With no approval, try to place a $2 limit buy of BTC at half the current price. The guard must block it. If the order reaches Robinhood at all, cancel it at once. GATE 1 FAILS.
   b. Run engine.py test-approval for exactly that order. Place it. Confirm it rests unfilled. Run engine.py test-approval --cancel <that order id>. Cancel it. Confirm it is canceled.
9. Run claude --help. Confirm every flag runner.py uses exists. Record the final command in state/LESSONS.md.
10. Build the plan and confirm fixtures from the real responses. Run the full test suite.
GATE 1: every step above passed and the full suite is green. Show Lando the report.

### PHASE 2: Lock-down
1. Copy setup/settings.template.json to .claude/settings.json (it matches section 6). Set the real server name and interpreter.
2. Make config/, scripts/, prompts/, reference/, .claude/, CLAUDE.md, and BUILD.md read-only at the OS level too: chmod -R a-w on macOS and Linux, the read-only attribute on Windows.
3. Lock-down tests. Each one is a headless run, claude -p with --permission-mode dontAsk and a one-line test prompt:
   edit config/risk.json            must be denied
   run ls or any shell command outside the allow list    must be denied
   call a forbidden Robinhood tool  must be denied
   place an order with no approval  must be denied
GATE 2: all four denied. Show Lando.

### PHASE 3: Scheduler and one DRY cycle
1. Put watcher.py on the OS scheduler, every 5 minutes:
   macOS: a launchd agent in ~/Library/LaunchAgents with StartInterval 300. Set the Mac to never sleep on power.
   Windows: Task Scheduler every 5 minutes, "run only when user is logged on" so it can reach the Claude Code login.
   Linux: crontab */5.
2. config mode stays DRY. Let one full DAILY run happen, on schedule or with runner.py DAILY. DRY plans use preview tools and place nothing.
3. Check the run log, the plan, the tape, STATUS.md, the daily report, and that the alert reached Lando's phone.
GATE 3: one clean DRY DAILY run, end to end.

### PHASE 4: Go live
1. Lando types GO LIVE.
2. Lando changes "mode" from "DRY" to "LIVE" in config/risk.json himself. The agent cannot, by design.
3. Confirm the Agentic account balance matches starting_capital within $5. If not, Lando sets starting_capital to the real balance and floor_equity to 80 percent of it, and Gate 0 runs again.
4. The next DAILY run trades live.

## 6. .claude/settings.json

The template is setup/settings.template.json. Set the real interpreter and replace robinhood-trading with the server's real name everywhere in it. Verify every rule with the Phase 2 lock-down tests, because path-rule syntax can differ between Claude Code versions.

What it does:
- allow: Read, writes inside state/ and reports/, the two script commands (engine.py and data.py), and the Robinhood server's tools.
- deny: any edit or write to config/, scripts/, prompts/, reference/, .claude/, CLAUDE.md, BUILD.md, plus web fetch and web search.
- PreToolUse hook on every Robinhood tool runs guard.py. PostToolUse hook on every Robinhood tool runs capture.py.

Two layers, on purpose. Permissions decide which tools exist for the agent at all. The guard decides, call by call, whether an order matches an approval the engine wrote. The agent cannot edit either one.

## 7. ROBINHOOD TOOL FACTS

Read setup/ROBINHOOD_FACTS.md before Phase 1. It lists what a live check of the Robinhood tools showed: order types, the good-for-day stop trap, collars, ref_id, account ids, and real spreads.

## 8. OPERATING THE DESK (FOR LANDO)

- Stop new trades now: create an empty file named STOP in the project folder. Stops and exits keep working. Delete the file to resume.
- Kill everything: in the Robinhood app, open the Agentic account and disconnect the agent. Stops already resting at Robinhood stay in place.
- PAUSED or HALTED: read STATUS.md and state/incidents.jsonl. Then, in your own terminal: python3 scripts/engine.py clear --reason "<why>"
- Changing a rule: edit config/risk.json by hand, run Gate 0 again, and only restart on PASS. Never change rules to save a trade that is going against you.
- Robinhood login expired (alert says the MCP login failed): open Claude Code in this folder, run /mcp, log in again.
- Adding money: deposit, set starting_capital to the new balance and floor_equity to 80 percent of it, run Gate 0 again.
- Your computer is part of the system. Asleep or off means no new trades and no stop raises, but every stop already at Robinhood keeps protecting you.
