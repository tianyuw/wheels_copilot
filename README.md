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
- Broker-sourced wheel lifecycle snapshot for `CSP_OPEN`, `ASSIGNED`, and
  `CC_OPEN` states, plus covered-call proposals, validation, and guarded paper
  execution for assigned shares.
- Unit tests for support scoring, delta policy, fundamentals, and earnings.

Normal scans do not submit orders. `shadow_orders.json` and
`validated_shadow_orders.json` remain auditable dry-run artifacts unless the CLI
is run with `--execute-paper` or `scripts/execute_validated_orders.py` is called
explicitly. Live trading is not implemented. Covered-call planning is dry-run
by default; paper execution is available with `scripts/plan_covered_calls.py
--execute-paper` after validation and strategy-specific portfolio gates pass.

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
python3 scripts/plan_covered_calls.py
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

## Covered Call Planning

After reconciliation, build a broker-sourced wheel lifecycle snapshot and
covered-call dry-run proposals:

```bash
python3 scripts/plan_covered_calls.py --output workspace/covered_calls/YYYY-MM-DD --overwrite
```

The lifecycle snapshot treats Alpaca as the source of truth. A ticker with
long stock and no open short call is marked `ASSIGNED` and becomes eligible for
covered-call evaluation. A ticker with long stock plus an open short call/order
is marked `CC_OPEN`; a ticker with short-put exposure and no stock is marked
`CSP_OPEN`.

The covered-call planner does not submit orders unless `--execute-paper` is
passed. A normal run writes:

```text
workspace/covered_calls/YYYY-MM-DD/
  wheel_lifecycle.json
  covered_call_proposals.json
  covered_call_shadow_orders.json
  validated_covered_call_shadow_orders.json
  covered_call_report.md
```

The planner blocks any call whose strike is below adjusted cost basis, whose
bid/spread/open interest fails `cc_selector`, or whose call delta is outside the
configured range. If no safe call exists, the position remains `WATCH`.

With `--execute-paper`, the same paper executor can submit validated
covered-call orders after re-fetching the Alpaca portfolio. It blocks if the
account is not active, the stock position cannot cover the call contracts, the
call strike is below adjusted cost basis, or proposal-level unresolved risks
remain. Current covered-call proposals deliberately carry
`earnings_not_checked` or `ex_dividend_not_checked` only when those gates cannot
prove the candidate safe.

Covered-call risk gates are conservative by default:

- Stock calls must expire before the next earnings date; ETFs skip the earnings
  gate because earnings do not apply.
- Dividend-paying symbols must have a known ex-dividend date.
- A call is blocked when an ex-dividend date falls after the scan date and on or
  before the call expiration.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
