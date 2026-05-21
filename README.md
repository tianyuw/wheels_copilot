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
- Alpaca paper order executor for submit-ready cash-secured puts, guarded by
  paper-only config, market clock, portfolio risk, and idempotent client order
  IDs.
- SQLite-backed OMS ledger and Alpaca paper reconciliation for submitted CSP
  orders.
- Unit tests for support scoring, delta policy, fundamentals, and earnings.

Normal scans do not submit orders. `shadow_orders.json` and
`validated_shadow_orders.json` remain auditable dry-run artifacts unless the CLI
is run with `--execute-paper` or `scripts/execute_validated_orders.py` is called
explicitly. Live trading is not implemented. Covered-call selection and
assignment lifecycle are not implemented yet.

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
python3 scripts/scan_watchlist.py --with-alpaca --execute-paper
python3 scripts/reconcile_orders.py
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
then marks it `submit_ready` or records blocking reasons. With `--execute-paper`,
the CLI submits only `submit_ready` orders to Alpaca paper and writes:

```text
workspace/scans/YYYY-MM-DD/
  execution_results.json
```

The executor submits no more than `execution.max_orders_per_run`, blocks
validated artifacts older than `execution.max_validated_order_age_seconds`,
blocks within `execution.no_open_minutes_before_close` of the market close,
re-fetches the Alpaca paper portfolio before each order, accounts for orders
submitted earlier in the same run, and blocks any portfolio gate warning or
rejection.

When `oms.enabled` is true, execution writes a persistent SQLite ledger before
the broker POST, then updates it with the Alpaca order ID and status. The default
database path is:

```text
workspace/oms/wheels_oms.sqlite
```

Existing validated orders can also be submitted directly:

```bash
python3 scripts/execute_validated_orders.py --input workspace/scans/YYYY-MM-DD/validated_shadow_orders.json
```

Reconcile submitted orders against Alpaca paper:

```bash
python3 scripts/reconcile_orders.py --output workspace/oms/reconciliation.json
```

Reconciliation updates OMS-submitted orders to `FILLED`, `PARTIAL`,
`CANCELED`, `EXPIRED`, `DONE_FOR_DAY`, `REJECTED`, or `ERROR`. It also recovers
ambiguous submit failures by looking up Alpaca orders with the original
`client_order_id`. Filled short-put orders create or update an `OPEN` OMS
position with assignment cash recorded. This reconciliation layer tracks orders
submitted through Wheels Copilot; externally created broker positions are a
separate follow-up sync task.

The first paper executor supports one-contract CSP orders only. Multi-contract
and partial-fill position accounting will be added after the single-contract
workflow has run cleanly.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
