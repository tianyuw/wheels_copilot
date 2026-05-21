# Wheels Copilot

Markus-style weekly wheel strategy research and paper-trading system.

Current implementation scope:

- TradingView-style support-zone engine.
- Dynamic CSP delta policy based on support strength.
- Yahoo Finance market-data adapter for dry runs.
- CSP candidate selector.
- Unit tests for support scoring and delta policy.

No live or paper orders are submitted by the current code.
Covered-call selection, assignment lifecycle, persistence, and Alpaca order
submission are not implemented yet.

The current Yahoo Finance option-chain adapter is for research dry runs only.
Broker-grade quotes are required before Alpaca paper execution.

## Dry Run

```bash
python3 scripts/dry_run.py --ticker AAPL
python3 scripts/dry_run.py --ticker AAPL --json
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```
