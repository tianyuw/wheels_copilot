# Wheels Copilot

Markus-style weekly wheel strategy research and paper-trading system.

Current implementation scope:

- TradingView-style support-zone engine.
- Dynamic CSP delta policy based on support strength.
- Yahoo Finance market-data adapter for dry runs.
- Markus fundamental quality gate.
- Earnings gate that keeps CSP expirations before the next earnings date.
- CSP candidate selector.
- Trade proposal and shadow order planner for dry-run Alpaca order payloads.
- Unit tests for support scoring, delta policy, fundamentals, and earnings.

No live or paper orders are submitted by the current code. `shadow_orders.json`
is an auditable dry-run artifact only.
Covered-call selection, assignment lifecycle, persistence, and Alpaca order
submission are not implemented yet.

The current Yahoo Finance option-chain adapter is for research dry runs only.
Broker-grade quotes are required before Alpaca paper execution.

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
perform a historical market-data replay; the current Yahoo Finance bars and
option chains are still fetched.

`--with-alpaca` performs read-only calls against the Alpaca paper Trading API
for account, positions, and open orders, then applies portfolio risk gates. It
requires `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the environment or local
`.env`. It does not submit or cancel orders.

Default output:

```text
workspace/scans/YYYY-MM-DD/
  scan_results.json
  scan_report.md
  scan_summary.csv
  trade_proposals.json
  shadow_orders.json
```

`trade_proposals.json` records candidate decisions (`PROPOSED`, `WATCH`,
`REJECTED_BY_GATE`, or `REJECTED_BY_ALLOCATION`) after sequential cash
allocation. Planner allocation is deterministic: AUTO_TRADE candidates are
prioritized by support score, then weekly return, then ticker. `shadow_orders.json`
contains only `PROPOSED` trades as dry-run Alpaca limit-order payloads with
`dry_run_only: true`; the CLI does not submit them.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
