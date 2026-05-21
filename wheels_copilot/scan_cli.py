from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .alpaca import fetch_alpaca_portfolio_snapshot
from .config import load_config
from .execution import execute_validated_shadow_orders
from .scan import resolve_output_dir, scan_watchlist, write_scan_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run daily dry-run scan for watchlist")
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument("--period", default="1y")
    parser.add_argument(
        "--tickers",
        help="Comma-separated ticker override. Defaults to config watchlist.",
    )
    parser.add_argument(
        "--date",
        dest="scan_date",
        help=(
            "Scan date in YYYY-MM-DD. Used for report date and option DTE; "
            "does not run historical market-data replay."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory. Defaults to workspace/scans/YYYY-MM-DD.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Write directly into output directory even if files already exist.",
    )
    parser.add_argument("--json-stdout", action="store_true")
    parser.add_argument(
        "--with-alpaca",
        action="store_true",
        help=(
            "Fetch Alpaca paper account/positions/open orders read-only and "
            "apply portfolio risk gates."
        ),
    )
    parser.add_argument(
        "--execute-paper",
        action="store_true",
        help=(
            "After the scan, submit submit-ready validated CSP orders to the "
            "Alpaca paper account. No interactive confirmation is requested."
        ),
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    scan_date = date.fromisoformat(args.scan_date) if args.scan_date else date.today()
    tickers = _parse_tickers(args.tickers)
    base_output = args.output or Path("workspace/scans") / scan_date.isoformat()
    output_dir = resolve_output_dir(base_output, overwrite=args.overwrite)
    portfolio_snapshot = None
    portfolio_error = None
    if args.execute_paper:
        args.with_alpaca = True
    if args.with_alpaca:
        try:
            portfolio_snapshot = fetch_alpaca_portfolio_snapshot(cfg)
        except Exception as exc:
            portfolio_error = f"{type(exc).__name__}: {exc}"

    scan = scan_watchlist(
        config=cfg,
        tickers=tickers,
        period=args.period,
        as_of=scan_date,
        portfolio_snapshot=portfolio_snapshot,
        portfolio_required=args.with_alpaca,
        portfolio_error=portfolio_error,
    )
    paths = write_scan_outputs(scan, output_dir, config=cfg)
    execution_result = None
    execution_path = None
    if args.execute_paper:
        validated = json.loads(paths["validated_shadow_orders"].read_text(encoding="utf-8"))
        execution_path = output_dir / "execution_results.json"
        previous = None
        if execution_path.exists():
            previous = json.loads(execution_path.read_text(encoding="utf-8"))
        execution_result = execute_validated_shadow_orders(
            validated,
            cfg,
            previous_execution_results=previous,
        )
        execution_path.write_text(
            json.dumps(execution_result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    if args.json_stdout:
        print(json.dumps(scan, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"Output directory: {output_dir}")
        for name, path in paths.items():
            print(f"{name}: {path}")
        if execution_path:
            print(f"execution_results: {execution_path}")
        print(f"Summary: {scan['summary']}")
        if execution_result:
            print(f"Execution summary: {execution_result['summary']}")
    return 0


def _parse_tickers(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip().upper() for part in raw.split(",") if part.strip()]
