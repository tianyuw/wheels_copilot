#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wheels_copilot.backtest import run_backtest, write_backtest_outputs
from wheels_copilot.config import load_config
from wheels_copilot.historical_fundamentals import DEFAULT_FUNDAMENTALS_CACHE_DIR
from wheels_copilot.historical_data import (
    DEFAULT_FLATFILES_CACHE_DIR,
    DEFAULT_FLATFILES_INDEXED_DIR,
    DEFAULT_FLATFILES_RAW_DIR,
    FlatFilesStore,
)
from wheels_copilot.price_space_breaks import DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    universe = resolve_universe(config, args)
    if not universe:
        raise SystemExit("No tickers selected for backtest.")

    store = FlatFilesStore(
        cache_dir=Path(args.cache_dir),
        raw_cache_dir=Path(args.raw_cache_dir),
        indexed_cache_dir=Path(args.indexed_cache_dir),
        require_warm_cache=args.require_warm_cache,
    )
    if not args.skip_cache_preflight:
        store.require_writable_cache()

    result = run_backtest(
        config=config,
        data=store,
        universe=universe,
        start=parse_date(args.start),
        end=parse_date(args.end),
        schedule=args.schedule,
        lookback_calendar_days=args.lookback_calendar_days,
        slippage_pct=args.slippage_pct,
        option_fee_per_contract=args.option_fee_per_contract,
        risk_free_rate=args.risk_free_rate,
        max_orders_per_day=args.max_orders_per_day,
        split_ratio_low=args.split_ratio_low,
        split_ratio_high=args.split_ratio_high,
        fundamental_profile=args.fundamental_profile,
        cc_risk_profile=args.cc_risk_profile,
        post_earnings_cooldown_days=args.post_earnings_cooldown_days,
        fundamentals_cache_dir=Path(args.fundamentals_cache_dir),
        fundamentals_env_file=Path(args.fundamentals_env_file)
        if args.fundamentals_env_file
        else None,
        fundamentals_timeout_seconds=args.fundamentals_timeout_seconds,
        price_space_break_classifier=args.price_space_break_classifier,
        price_space_break_cache_dir=Path(args.price_space_break_cache_dir),
        price_space_break_env_file=Path(args.price_space_break_env_file)
        if args.price_space_break_env_file
        else None,
        price_space_break_timeout_seconds=args.price_space_break_timeout_seconds,
        price_space_split_reset_min_support_bars=(
            args.price_space_split_reset_min_support_bars
        ),
    )
    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(args)
    paths = write_backtest_outputs(result, output_dir)
    print(json.dumps({name: str(path) for name, path in paths.items()}, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the phase-one Wheel Copilot historical backtest."
    )
    parser.add_argument("--start", required=True, help="Backtest start date, YYYY-MM-DD.")
    parser.add_argument("--end", required=True, help="Backtest end date, YYYY-MM-DD.")
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument(
        "--universe",
        choices=["watchlist", "tickers", "file"],
        default="watchlist",
        help="Ticker universe source.",
    )
    parser.add_argument("--tickers", help="Comma-separated tickers when --universe tickers.")
    parser.add_argument("--universe-file", help="One ticker per line or JSON list.")
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_FLATFILES_CACHE_DIR),
        help="Legacy Massive FlatFiles cache directory.",
    )
    parser.add_argument(
        "--raw-cache-dir",
        default=str(DEFAULT_FLATFILES_RAW_DIR),
        help="Local raw Massive FlatFiles CSV.GZ cache directory.",
    )
    parser.add_argument(
        "--indexed-cache-dir",
        default=str(DEFAULT_FLATFILES_INDEXED_DIR),
        help="Local indexed Massive FlatFiles Parquet cache directory.",
    )
    parser.add_argument(
        "--require-warm-cache",
        action="store_true",
        help="Fail instead of downloading/building FlatFiles cache during the backtest.",
    )
    parser.add_argument("--output-dir", help="Directory for JSON/CSV/Markdown outputs.")
    parser.add_argument("--schedule", choices=["daily"], default="daily")
    parser.add_argument("--lookback-calendar-days", type=int, default=430)
    parser.add_argument("--slippage-pct", type=float, default=0.05)
    parser.add_argument("--option-fee-per-contract", type=float, default=0.10)
    parser.add_argument("--risk-free-rate", type=float, default=0.04)
    parser.add_argument("--max-orders-per-day", type=int)
    parser.add_argument("--split-ratio-low", type=float, default=0.75)
    parser.add_argument("--split-ratio-high", type=float, default=1.25)
    parser.add_argument(
        "--fundamental-profile",
        choices=[
            "technical_only",
            "fundamentals_warn",
            "fundamentals_moderate",
            "fundamentals_strict_financials",
            "fundamentals_strict_all",
        ],
        default="technical_only",
        help="Historical fundamental gate profile.",
    )
    parser.add_argument(
        "--cc-risk-profile",
        choices=["strict", "warn_unknown_dates"],
        help="Backtest-only covered-call risk profile. Defaults to config backtest.cc_risk_profile or strict.",
    )
    parser.add_argument(
        "--post-earnings-cooldown-days",
        type=int,
        help=(
            "Block new CSP entries when the previous earnings report was within this "
            "many NYSE trading days. Defaults to config backtest.post_earnings_cooldown_days or 0."
        ),
    )
    parser.add_argument(
        "--fundamentals-cache-dir",
        default=str(DEFAULT_FUNDAMENTALS_CACHE_DIR),
        help="Historical fundamentals REST cache directory.",
    )
    parser.add_argument(
        "--fundamentals-env-file",
        help="Optional .env file containing Massive and Unusual Whales credentials.",
    )
    parser.add_argument("--fundamentals-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--price-space-break-classifier",
        choices=["off", "massive_splits"],
        help="Classify price-space breaks. massive_splits allows real gaps, resets confirmed split lookbacks, and blocks unknown breaks.",
    )
    parser.add_argument(
        "--price-space-break-cache-dir",
        default=str(DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR),
        help="Cache directory for Massive split corporate-action lookups.",
    )
    parser.add_argument(
        "--price-space-break-env-file",
        help="Optional .env file containing Massive credentials for the classifier.",
    )
    parser.add_argument("--price-space-break-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--price-space-split-reset-min-support-bars",
        type=int,
        help="Minimum post-split support bars before new CSP entries are considered. Defaults to config backtest.price_space_split_reset_min_support_bars or 30.",
    )
    parser.add_argument(
        "--skip-cache-preflight",
        action="store_true",
        help="Skip Data volume write/read/delete preflight. Intended for tests only.",
    )
    return parser.parse_args()


def resolve_universe(config: dict[str, Any], args: argparse.Namespace) -> list[str]:
    if args.universe == "watchlist":
        tickers = config.get("watchlist", {}).get("tickers", [])
        return normalize_tickers(tickers)
    if args.universe == "tickers":
        if not args.tickers:
            raise SystemExit("--tickers is required when --universe tickers")
        return normalize_tickers(args.tickers.split(","))
    if args.universe == "file":
        if not args.universe_file:
            raise SystemExit("--universe-file is required when --universe file")
        return normalize_tickers(read_universe_file(Path(args.universe_file)))
    raise SystemExit(f"Unsupported universe source: {args.universe}")


def read_universe_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError(f"Universe JSON must be a list: {path}")
        return [str(item) for item in payload]
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def normalize_tickers(values: list[str]) -> list[str]:
    return sorted({str(value).strip().upper() for value in values if str(value).strip()})


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def default_output_dir(args: argparse.Namespace) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "workspace" / "backtests" / f"phase1_{args.start}_{args.end}_{stamp}"


if __name__ == "__main__":
    raise SystemExit(main())
