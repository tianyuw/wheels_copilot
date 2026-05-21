from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .config import load_config
from .daily_runner import run_autonomous_daily


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the autonomous daily Markus wheel workflow"
    )
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument("--period", default="1y")
    parser.add_argument(
        "--tickers",
        help="Comma-separated ticker override. Defaults to config watchlist.",
    )
    parser.add_argument(
        "--date",
        dest="run_date",
        help="Run date in YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory. Defaults to workspace/daily_runs/YYYY-MM-DD.",
    )
    parser.add_argument(
        "--execute-paper",
        action="store_true",
        help="Submit validated paper orders without interactive confirmation.",
    )
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    as_of = date.fromisoformat(args.run_date) if args.run_date else date.today()
    result = run_autonomous_daily(
        cfg,
        tickers=_parse_tickers(args.tickers),
        period=args.period,
        as_of=as_of,
        output_dir=args.output,
        execute_paper=args.execute_paper,
    )
    if args.json_stdout:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Output directory: {result['output_dir']}")
        print(f"Status: {result['status']}")
        for name, step in result.get("steps", {}).items():
            print(f"{name}: {step.get('status')}")
        if result.get("errors"):
            for error in result["errors"]:
                print(f"error[{error.get('scope')}]: {error.get('error')}")
    return 0 if result["status"] == "COMPLETED" else 1


def _parse_tickers(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip().upper() for part in raw.split(",") if part.strip()]
