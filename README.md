# Wheels Copilot

Markus-style weekly wheel strategy research and paper-trading system.

Current implementation scope:

- TradingView-style support-zone engine.
- Dynamic CSP delta policy based on support strength.
- Alpaca SIP/OPRA market-data adapter for stock bars and option quotes.
- Markus fundamental quality gate.
- Earnings gate that keeps CSP expirations before the next earnings date.
- CSP candidate selector.
- Trade proposal and shadow order planner for dry-run Alpaca order payloads.
- Shadow order readiness gate using fresh Alpaca OPRA latest quotes.
- Unit tests for support scoring, delta policy, fundamentals, and earnings.

No live or paper orders are submitted by the current code. `shadow_orders.json`
and `validated_shadow_orders.json` are auditable dry-run artifacts only.
Covered-call selection, assignment lifecycle, persistence, and Alpaca order
submission are not implemented yet.

All stock and option price data is fetched from Alpaca. The scan fails closed
unless the stock feed is `sip` and the option feed is `opra`; it does not
fallback to delayed or indicative feeds. Option quotes must have a valid
positive bid/ask and must be no older than `market_data.max_option_quote_age_seconds`.
Yahoo Finance is retained only for fundamental fields that Alpaca does not
provide, such as profitability history, P/E, dividend yield, and earnings date.

## Dry Run

```bash
python3 scripts/dry_run.py --ticker AAPL
python3 scripts/dry_run.py --ticker AAPL --json
```

## Daily Watchlist Scan

```bash
python3 scripts/scan_watchlist.py
python3 scripts/scan_watchlist.py --tickers AAPL,UPS --output /tmp/wheels-scan --overwrite
python3 scripts/scan_watchlist.py --with-alpaca
```

`--date` controls the report date and option DTE calculation. It does not
perform a historical market-data replay; current Alpaca SIP bars and OPRA
option data are still fetched.

The scan requires `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the environment or
local `.env` for Alpaca market data. `--with-alpaca` also performs read-only
calls against the Alpaca paper Trading API for account, positions, and open
orders, then applies portfolio risk gates. It does not submit or cancel orders.

Default output:

```text
workspace/scans/YYYY-MM-DD/
  scan_results.json
  scan_report.md
  scan_summary.csv
  trade_proposals.json
  shadow_orders.json
  validated_shadow_orders.json
```

`trade_proposals.json` records candidate decisions (`PROPOSED`, `WATCH`,
`REJECTED_BY_GATE`, or `REJECTED_BY_ALLOCATION`) after sequential cash
allocation. Planner allocation is deterministic: AUTO_TRADE candidates are
prioritized by support score, then weekly return, then ticker. `shadow_orders.json`
contains only `PROPOSED` trades as dry-run Alpaca limit-order payloads with
`dry_run_only: true`. `validated_shadow_orders.json` revalidates each shadow
order with fresh Alpaca OPRA latest quotes and active/tradable contract checks,
then marks it `submit_ready` or records blocking reasons. The CLI does not
submit orders.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
