#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wheels_copilot.config import load_config
from wheels_copilot.historical_data import (
    DEFAULT_FLATFILES_CACHE_DIR,
    DEFAULT_FLATFILES_INDEXED_DIR,
    DEFAULT_FLATFILES_RAW_DIR,
)
from wheels_copilot.historical_fundamentals import DEFAULT_FUNDAMENTALS_CACHE_DIR
from wheels_copilot.price_space_breaks import DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR
from wheels_copilot.sensitivity import (
    build_run_specs,
    compute_resource_plan,
    default_data_source_metadata,
    load_scenario_file,
    load_leaderboard_rows,
    run_sensitivity,
    write_json,
)


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    scenario = load_scenario_file(Path(args.scenario_file))
    universe = resolve_universe(config, args)
    if not universe:
        raise SystemExit("No tickers selected for sensitivity run.")

    start = parse_date_string(args.start)
    end = parse_date_string(args.end)
    scenario_name = str(scenario["name"])
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else default_output_dir(scenario_name, start, end)
    )
    cache_dir = Path(args.cache_dir)
    raw_cache_dir = Path(args.raw_cache_dir)
    indexed_cache_dir = Path(args.indexed_cache_dir)
    specs = build_run_specs(
        scenario=scenario,
        base_config=config,
        start=start,
        end=end,
        universe=universe,
        universe_name=args.universe,
        max_runs=args.max_runs,
        max_combinations_per_experiment=args.max_combinations_per_experiment,
        allow_large_matrix=args.allow_large_matrix,
        data_source_metadata=default_data_source_metadata(),
    )
    resource_plan = compute_resource_plan(
        total_runs=len(specs),
        requested_workers=args.workers,
        memory_per_worker_gb=args.memory_per_worker_gb,
        reserve_memory_gb=args.reserve_memory_gb,
        configured_max_workers=args.max_workers,
        cpu_reserve=args.cpu_reserve,
    )
    run_options = {
        "schedule": "daily",
        "lookback_calendar_days": args.lookback_calendar_days,
        "slippage_pct": args.slippage_pct,
        "option_fee_per_contract": args.option_fee_per_contract,
        "risk_free_rate": args.risk_free_rate,
        "max_orders_per_day": args.max_orders_per_day,
        "split_ratio_low": args.split_ratio_low,
        "split_ratio_high": args.split_ratio_high,
        "fundamental_profile": args.fundamental_profile,
        "cc_risk_profile": args.cc_risk_profile,
        "fundamentals_cache_dir": Path(args.fundamentals_cache_dir),
        "fundamentals_env_file": Path(args.fundamentals_env_file)
        if args.fundamentals_env_file
        else None,
        "fundamentals_timeout_seconds": args.fundamentals_timeout_seconds,
        "price_space_break_classifier": args.price_space_break_classifier,
        "price_space_break_cache_dir": Path(args.price_space_break_cache_dir),
        "price_space_break_env_file": Path(args.price_space_break_env_file)
        if args.price_space_break_env_file
        else None,
        "price_space_break_timeout_seconds": args.price_space_break_timeout_seconds,
        "price_space_split_reset_min_support_bars": (
            args.price_space_split_reset_min_support_bars
        ),
    }
    plan = {
        "scenario": scenario_name,
        "output_dir": str(output_dir),
        "cache_dir": str(cache_dir),
        "raw_cache_dir": str(raw_cache_dir),
        "indexed_cache_dir": str(indexed_cache_dir),
        "require_warm_cache": args.require_warm_cache,
        "start": start,
        "end": end,
        "universe": universe,
        "run_count": len(specs),
        "resource_plan": resource_plan.__dict__,
        "run_options": run_options,
        "runs": [
            {
                "run_id": spec.run_id,
                "name": spec.name,
                "experiment_name": spec.experiment_name,
                "is_baseline": spec.is_baseline,
                "parameter_patch": spec.parameter_patch,
            }
            for spec in specs
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "plan.json", plan)
    if args.print_full_plan:
        print(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
    else:
        print(
            json.dumps(
                {
                    "scenario": scenario_name,
                    "output_dir": str(output_dir),
                    "plan": str(output_dir / "plan.json"),
                    "start": start,
                    "end": end,
                    "universe_count": len(universe),
                    "run_count": len(specs),
                    "resource_plan": resource_plan.__dict__,
                    "run_options": run_options,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )

    if args.dry_run:
        return 0

    results = run_sensitivity(
        specs=specs,
        output_dir=output_dir,
        cache_dir=cache_dir,
        resource_plan=resource_plan,
        resume=args.resume,
        rerun_failed=args.rerun_failed,
        skip_cache_preflight=args.skip_cache_preflight,
        raw_cache_dir=raw_cache_dir,
        indexed_cache_dir=indexed_cache_dir,
        require_warm_cache=args.require_warm_cache,
        run_options=run_options,
    )
    leaderboard_rows = load_leaderboard_rows(output_dir)
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "completed": len(leaderboard_rows),
                "newly_completed": sum(
                    1 for row in results if row.get("status") == "completed"
                ),
                "failed": sum(1 for row in results if row.get("status") == "failed"),
                "leaderboard": str(output_dir / "leaderboard.csv"),
                "report": str(output_dir / "report.md"),
                "progress": str(output_dir / "progress.json"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a resource-aware parallel sensitivity matrix for the Wheel Copilot backtest."
    )
    parser.add_argument("--start", required=True, help="Backtest start date, YYYY-MM-DD.")
    parser.add_argument("--end", required=True, help="Backtest end date, YYYY-MM-DD.")
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument(
        "--scenario-file",
        default="config/backtest_sensitivity_first_pass.yaml",
        help="YAML file defining sensitivity experiments.",
    )
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
        help="Fail instead of downloading/building FlatFiles cache during sensitivity runs.",
    )
    parser.add_argument("--output-dir", help="Directory for plan, run outputs, and reports.")
    parser.add_argument(
        "--workers",
        default="auto",
        help="Worker count or auto. Explicit values are still capped by --max-workers.",
    )
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--cpu-reserve", type=int, default=2)
    parser.add_argument("--memory-per-worker-gb", type=float, default=3.0)
    parser.add_argument("--reserve-memory-gb", type=float, default=6.0)
    parser.add_argument("--max-runs", type=int, default=50)
    parser.add_argument("--max-combinations-per-experiment", type=int, default=24)
    parser.add_argument(
        "--allow-large-matrix",
        action="store_true",
        help="Allow an individual experiment to exceed --max-combinations-per-experiment.",
    )
    parser.add_argument("--resume", action="store_true", help="Skip completed run directories.")
    parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="When used with --resume, rerun failed run directories instead of skipping them.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Write and print plan only.")
    parser.add_argument(
        "--print-full-plan",
        action="store_true",
        help="Print every run spec to stdout. The full plan is always written to plan.json.",
    )
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


def parse_date_string(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def default_output_dir(scenario_name: str, start: str, end: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in scenario_name
    )
    return ROOT / "workspace" / "sensitivity_runs" / f"{safe_name}_{start}_{end}_{stamp}"


if __name__ == "__main__":
    raise SystemExit(main())
