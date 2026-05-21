from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .config import load_config
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
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    scan_date = date.fromisoformat(args.scan_date) if args.scan_date else date.today()
    tickers = _parse_tickers(args.tickers)
    base_output = args.output or Path("workspace/scans") / scan_date.isoformat()
    output_dir = resolve_output_dir(base_output, overwrite=args.overwrite)

    scan = scan_watchlist(
        config=cfg,
        tickers=tickers,
        period=args.period,
        as_of=scan_date,
    )
    paths = write_scan_outputs(scan, output_dir)

    if args.json_stdout:
        print(json.dumps(scan, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"Output directory: {output_dir}")
        for name, path in paths.items():
            print(f"{name}: {path}")
        print(f"Summary: {scan['summary']}")
    return 0


def _parse_tickers(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip().upper() for part in raw.split(",") if part.strip()]
