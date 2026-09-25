# ROBINHOOD TOOL FACTS, CHECKED LIVE ON SEPTEMBER 25, 2026

Verify these again in Phase 1. They came from the live tool schemas and responses.
- One account is tradable by the agent: get_accounts marks it agentic_allowed true. Crypto tools take its rhs_account_number. Never pass the rhc_account_number (the linked crypto account id). It returns nothing.
- Crypto order types: market, limit, stop_loss (a stop order that triggers a market order), stop_limit.
- Stop time_in_force: gtc (90 days), gfd, gfw, gfm. The default when omitted is gfd. Always send gtc.
- Market collars: a triggered or market sell can fill up to about 5 percent below the quote in a fast move. A market buy up to about 1 percent above. The watcher's breach check exists for the case where a stop triggers and does not fill.
- place_crypto_order takes ref_id, a UUID idempotency key. Same ref_id on a retry means no duplicate order.
- Quantity or dollar_amount, never both. The desk always sends quantity so stops match the exact filled amount.
- get_crypto_quotes with rhs_account_number returns bid and ask on the account's real routing. On the check it showed Market Maker Routing with about 1.9 percent between bid and ask on BTC, ETH, SOL, and DOGE.
- get_currency_pairs returns min_order_size, min_order_quantity_increment, min_order_price_increment, and halted flags per coin. The desk skips any coin that is halted.
- In the Claude chat app the tools appeared as mcp__Robinhood__<tool>. In Claude Code the prefix follows whatever name the server was added under. The guard matcher and permissions must use that exact name, and the Phase 2 no-approval order test proves they do.

## Tool schemas pulled September 25, 2026 (from Lando; verify in Phase 1)
- place_crypto_order and preview_crypto_order take symbol as "BTC" or "BTC-USD". quantity, limit_price, stop_price, time_in_force, and ref_id are all strings. Send exactly one of quantity or dollar_amount. The desk sends quantity as a decimal string cut to the coin's increment, and the guard denies any call that sends both.
- get_crypto_quotes takes symbols like "BTC-USD" and returns them without the hyphen ("BTCUSD"). Prices are decimal strings with up to 8 places: bid_price, ask_price, mark_price, open_price (the previous close), plus routing and updated_at.
- get_accounts returns accounts[] with account_number, rhs_account_number, rhc_account_number, and agentic_allowed. The desk records both account_number and rhs_account_number.
- get_portfolio takes account_number and returns cash, crypto_value, and crypto_buying_power.buying_power as strings.
- get_currency_pairs is paginated with a next cursor. BTC, ETH, SOL, and DOGE were not in the first 100 results: keep paging until all four appear before filling increments and minimums.
- Parse every number with Decimal. Real responses captured in Phase 1 are the fixtures for broker.py's raw mappings.
