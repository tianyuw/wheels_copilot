#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
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
    FlatFilesStore,
    date_range,
)


def main() -> int:
    args = parse_args()
    start = parse_date(args.start)
    end = parse_date(args.end)
    warm_start = start - timedelta(days=args.lookback_calendar_days)
    tickers = resolve_tickers(args)
    if not tickers:
        raise SystemExit("No tickers selected for FlatFiles warmup.")
    datasets = resolve_datasets(args.datasets)
    option_types = resolve_option_types(args.option_types)
    days = [day for day in date_range(warm_start, end) if day.weekday() < 5]
    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(start, end)
    output_dir.mkdir(parents=True, exist_ok=True)

    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "warm_start": warm_start.isoformat(),
        "lookback_calendar_days": args.lookback_calendar_days,
        "trading_weekdays": len(days),
        "ticker_count": len(tickers),
        "tickers": tickers,
        "datasets": datasets,
        "option_types": option_types,
        "cache_dir": str(Path(args.cache_dir)),
        "raw_cache_dir": str(Path(args.raw_cache_dir)),
        "indexed_cache_dir": str(Path(args.indexed_cache_dir)),
        "workers": args.workers,
        "dry_run": args.dry_run,
    }
    write_json(output_dir / "plan.json", plan)
    print(json.dumps(plan, indent=2, ensure_ascii=False))

    if args.dry_run:
        dry_run = build_dry_run_report(
            days=days,
            tickers=set(tickers),
            datasets=datasets,
            option_types=option_types,
            args=args,
        )
        write_json(output_dir / "dry_run.json", dry_run)
        print(json.dumps(dry_run, indent=2, ensure_ascii=False))
        return 0

    results = run_warmup(
        days=days,
        tickers=set(tickers),
        datasets=datasets,
        option_types=option_types,
        args=args,
    )
    summary = summarize_results(results)
    write_json(output_dir / "status.json", {"summary": summary, "results": results})
    print(json.dumps({"output_dir": str(output_dir), "summary": summary}, indent=2))
    return 0 if summary["failed_days"] == 0 else 1


def run_warmup(
    *,
    days: list[date],
    tickers: set[str],
    datasets: list[str],
    option_types: list[str],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    workers = max(1, min(args.workers, len(days) or 1))
    payloads = [
        {
            "day": day.isoformat(),
            "tickers": sorted(tickers),
            "datasets": datasets,
            "option_types": option_types,
            "cache_dir": args.cache_dir,
            "raw_cache_dir": args.raw_cache_dir,
            "indexed_cache_dir": args.indexed_cache_dir,
            "cache_timeout_seconds": args.cache_timeout_seconds,
        }
        for day in days
    ]
    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(warmup_worker, payload): payload for payload in payloads}
        for future in as_completed(futures):
            payload = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "date": payload["day"],
                    "status": "failed",
                    "error": repr(exc),
                }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    results.sort(key=lambda row: row["date"])
    return results


def warmup_worker(payload: dict[str, Any]) -> dict[str, Any]:
    day = date.fromisoformat(payload["day"])
    started = datetime.now(timezone.utc)
    store = FlatFilesStore(
        cache_dir=Path(payload["cache_dir"]),
        raw_cache_dir=Path(payload["raw_cache_dir"]),
        indexed_cache_dir=Path(payload["indexed_cache_dir"]),
        cache_timeout_seconds=float(payload["cache_timeout_seconds"]),
    )
    result = store.warmup_day(
        day,
        set(payload["tickers"]),
        datasets=payload["datasets"],
        option_types=payload["option_types"],
    )
    completed = datetime.now(timezone.utc)
    result.update(
        {
            "status": "completed",
            "elapsed_seconds": (completed - started).total_seconds(),
            "cache_stats": store.cache_stats_snapshot(),
        }
    )
    return result


def build_dry_run_report(
    *,
    days: list[date],
    tickers: set[str],
    datasets: list[str],
    option_types: list[str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    store = FlatFilesStore(
        cache_dir=Path(args.cache_dir),
        raw_cache_dir=Path(args.raw_cache_dir),
        indexed_cache_dir=Path(args.indexed_cache_dir),
    )
    rows: list[dict[str, Any]] = []
    missing_raw = 0
    missing_indexed = 0
    for day in days:
        row: dict[str, Any] = {"date": day.isoformat(), "datasets": {}}
        for dataset in datasets:
            raw_exists = store.raw_cache_path(dataset, day).exists() or store.raw_missing_path(
                dataset, day
            ).exists()
            indexed_missing_marker = store.indexed_missing_path(dataset, day).exists()
            if dataset == "stocks-day-aggs":
                stock_coverage = store._stock_coverage(day)
                missing_tickers = sorted(tickers - stock_coverage)
                indexed_exists = indexed_missing_marker or not missing_tickers
                coverage_detail: dict[str, Any] = {
                    "covered_tickers": len(stock_coverage),
                    "missing_tickers": len(missing_tickers),
                }
            else:
                option_detail = {}
                option_types_covered = True
                for option_type in option_types:
                    option_coverage = store._option_coverage(day, option_type)
                    missing_tickers = sorted(tickers - option_coverage)
                    option_detail[option_type] = {
                        "covered_tickers": len(option_coverage),
                        "missing_tickers": len(missing_tickers),
                    }
                    option_types_covered = option_types_covered and not missing_tickers
                indexed_exists = indexed_missing_marker or option_types_covered
                coverage_detail = {"option_type_coverage": option_detail}
            if not raw_exists:
                missing_raw += 1
            if not indexed_exists:
                missing_indexed += 1
            row["datasets"][dataset] = {
                "raw_exists": raw_exists,
                "indexed_exists": indexed_exists,
                **coverage_detail,
            }
            if dataset == "options-day-aggs":
                row["datasets"][dataset]["option_types"] = option_types
        rows.append(row)
    return {
        "days": len(days),
        "ticker_count": len(tickers),
        "missing_raw_files": missing_raw,
        "missing_indexed_files": missing_indexed,
        "rows": rows,
    }


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in results if row.get("status") == "completed"]
    failed = [row for row in results if row.get("status") != "completed"]
    return {
        "completed_days": len(completed),
        "failed_days": len(failed),
        "elapsed_seconds_total": sum(float(row.get("elapsed_seconds") or 0) for row in completed),
        "failed_dates": [row.get("date") for row in failed],
    }


def resolve_tickers(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    if args.config_watchlist:
        config = load_config(args.config)
        values.extend(config.get("watchlist", {}).get("tickers", []))
    for path in args.universe_file or []:
        values.extend(read_universe_file(Path(path)))
    if args.tickers:
        values.extend(args.tickers.split(","))
    return sorted({str(value).strip().upper() for value in values if str(value).strip()})


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


def resolve_datasets(value: str) -> list[str]:
    if value == "all":
        return ["stocks-day-aggs", "options-day-aggs"]
    if value == "stocks":
        return ["stocks-day-aggs"]
    if value == "options":
        return ["options-day-aggs"]
    raise ValueError(f"unsupported datasets value: {value}")


def resolve_option_types(value: str) -> list[str]:
    if value == "all":
        return ["put", "call"]
    if value in {"put", "call"}:
        return [value]
    raise ValueError(f"unsupported option_types value: {value}")


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def default_workers() -> int:
    return max(1, min(6, (os.cpu_count() or 2) - 1))


def default_output_dir(start: date, end: date) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "workspace" / "flatfiles_warmup" / f"{start}_{end}_{stamp}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Warm local Massive FlatFiles raw and indexed caches before backtests."
    )
    parser.add_argument("--start", required=True, help="Backtest start date, YYYY-MM-DD.")
    parser.add_argument("--end", required=True, help="Backtest end date, YYYY-MM-DD.")
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument(
        "--config-watchlist",
        action="store_true",
        help="Include tickers from the configured watchlist.",
    )
    parser.add_argument(
        "--universe-file",
        action="append",
        help="Universe file. Can be passed multiple times; all tickers are unioned.",
    )
    parser.add_argument("--tickers", help="Comma-separated tickers to include.")
    parser.add_argument("--lookback-calendar-days", type=int, default=0)
    parser.add_argument("--datasets", choices=["all", "stocks", "options"], default="all")
    parser.add_argument("--option-types", choices=["all", "put", "call"], default="all")
    parser.add_argument("--cache-dir", default=str(DEFAULT_FLATFILES_CACHE_DIR))
    parser.add_argument("--raw-cache-dir", default=str(DEFAULT_FLATFILES_RAW_DIR))
    parser.add_argument("--indexed-cache-dir", default=str(DEFAULT_FLATFILES_INDEXED_DIR))
    parser.add_argument("--output-dir")
    parser.add_argument("--workers", type=int, default=default_workers())
    parser.add_argument("--cache-timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report cache coverage without downloading or indexing files.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
