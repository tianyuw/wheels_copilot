from __future__ import annotations

import csv
import gzip
import json
import math
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from .execution_price import BacktestExecutionModel, modeled_quote_from_reference
from .models import OptionQuote, PriceBar
from .option_math import black_scholes_call_delta, black_scholes_put_delta, norm_cdf
from .trading_calendar import nyse_trading_days

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms.
    fcntl = None  # type: ignore[assignment]


DEFAULT_FLATFILES_CACHE_DIR = Path("/Volumes/Data/wheels_copilot/flatfiles_cache")
DEFAULT_FLATFILES_RAW_DIR = Path("/Volumes/Data/wheels_copilot/flatfiles_raw")
DEFAULT_FLATFILES_INDEXED_DIR = Path("/Volumes/Data/wheels_copilot/flatfiles_indexed")
DEFAULT_ENDPOINT_URL = "https://files.massive.com"
DEFAULT_BUCKET = "flatfiles"
DEFAULT_CACHE_LOCK_TIMEOUT_SECONDS = 300.0
INDEXED_CACHE_SCHEMA_VERSION = "flatfiles_indexed.v1"
DATASET_CACHE_DIRS = {
    "stocks-day-aggs": "stocks_day_aggs",
    "options-day-aggs": "options_day_aggs",
}


class BacktestDataError(RuntimeError):
    pass


class WarmCacheMiss(BacktestDataError):
    def __init__(
        self,
        *,
        dataset: str,
        day: date,
        reason: str,
        tickers: Sequence[str] | None = None,
        option_type: str | None = None,
    ) -> None:
        details = [f"dataset={dataset}", f"date={day.isoformat()}"]
        if option_type:
            details.append(f"option_type={option_type}")
        if tickers:
            preview = ",".join(sorted(tickers)[:12])
            if len(tickers) > 12:
                preview += f",...(+{len(tickers) - 12})"
            details.append(f"tickers={preview}")
        super().__init__(f"warm cache miss ({'; '.join(details)}): {reason}")
        self.dataset = dataset
        self.day = day
        self.reason = reason
        self.tickers = tuple(sorted(tickers or []))
        self.option_type = option_type


class FlatFileMissing(RuntimeError):
    pass


class HistoricalDataStore(Protocol):
    def trading_days(self, start: date, end: date) -> list[date]:
        ...

    def load_stock_bars(
        self, tickers: list[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        ...

    def option_chain(
        self,
        underlying: str,
        as_of: date,
        *,
        dte_min: int,
        dte_max: int,
        option_type: str = "put",
        price_field: str = "open",
        slippage_pct: float = 0.0,
        risk_free_rate: float = 0.04,
        stock_price: float | None = None,
        execution_model: BacktestExecutionModel | None = None,
    ) -> list[OptionQuote]:
        ...

    def option_mark(
        self,
        symbol: str,
        as_of: date,
        *,
        price_field: str = "close",
        stock_price: float | None = None,
        risk_free_rate: float = 0.04,
    ) -> OptionQuote | None:
        ...


@dataclass(frozen=True)
class CachePreflightResult:
    cache_dir: Path
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class ParsedOptionSymbol:
    raw_symbol: str
    underlying: str
    expiration: date
    option_type: str
    strike: float


class FlatFilesStore:
    def __init__(
        self,
        *,
        cache_dir: Path = DEFAULT_FLATFILES_CACHE_DIR,
        raw_cache_dir: Path | None = None,
        indexed_cache_dir: Path | None = None,
        aws: str = "aws",
        endpoint_url: str = DEFAULT_ENDPOINT_URL,
        bucket: str = DEFAULT_BUCKET,
        cache_timeout_seconds: float = 15.0,
        require_warm_cache: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        if raw_cache_dir is None:
            raw_cache_dir = (
                DEFAULT_FLATFILES_RAW_DIR
                if self.cache_dir == DEFAULT_FLATFILES_CACHE_DIR
                else self.cache_dir / "raw"
            )
        if indexed_cache_dir is None:
            indexed_cache_dir = (
                DEFAULT_FLATFILES_INDEXED_DIR
                if self.cache_dir == DEFAULT_FLATFILES_CACHE_DIR
                else self.cache_dir / "indexed"
            )
        self.raw_cache_dir = Path(raw_cache_dir)
        self.indexed_cache_dir = Path(indexed_cache_dir)
        self.aws = aws
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.cache_timeout_seconds = cache_timeout_seconds
        self.require_warm_cache = require_warm_cache
        self.cache_stats: dict[str, int] = {
            "raw_hits": 0,
            "raw_downloads": 0,
            "raw_missing": 0,
            "indexed_hits": 0,
            "indexed_builds": 0,
            "indexed_missing": 0,
            "strict_misses": 0,
        }
        self._option_day_memory_cache: dict[
            tuple[date, str, tuple[str, ...]], dict[str, list[dict[str, Any]]]
        ] = {}
        self._option_day_memory_day: date | None = None

    def preflight_cache(self) -> CachePreflightResult:
        for cache_dir in dict.fromkeys(
            [self.cache_dir, self.raw_cache_dir, self.indexed_cache_dir]
        ):
            result = self._preflight_cache_dir(cache_dir)
            if not result.ok:
                return result
        return CachePreflightResult(cache_dir=self.cache_dir, ok=True)

    def _preflight_cache_dir(self, cache_dir: Path) -> CachePreflightResult:
        probe = cache_dir / ".preflight" / f"write_probe.{os.getpid()}.{uuid.uuid4().hex}.txt"
        try:
            ensure_parent_dir(probe, timeout_seconds=self.cache_timeout_seconds)
            script = (
                "from pathlib import Path\n"
                "import os, sys\n"
                "p = Path(sys.argv[1])\n"
                "p.write_text('flatfiles-cache-probe\\n', encoding='utf-8')\n"
                "assert p.read_text(encoding='utf-8') == 'flatfiles-cache-probe\\n'\n"
                "p.unlink()\n"
            )
            subprocess.run(
                [sys.executable, "-c", script, str(probe)],
                check=True,
                timeout=self.cache_timeout_seconds,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return CachePreflightResult(
                cache_dir=cache_dir,
                ok=False,
                reason="cache write/read/delete timed out",
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            return CachePreflightResult(
                cache_dir=cache_dir,
                ok=False,
                reason=str(exc),
            )
        return CachePreflightResult(cache_dir=cache_dir, ok=True)

    def require_writable_cache(self) -> None:
        result = self.preflight_cache()
        if not result.ok:
            raise BacktestDataError(
                f"cache directory is not writable: {result.cache_dir} ({result.reason})"
            )

    def trading_days(self, start: date, end: date) -> list[date]:
        return nyse_trading_days(start, end)

    def load_stock_bars(
        self, tickers: list[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        normalized = {ticker.upper() for ticker in tickers}
        by_ticker: dict[str, list[PriceBar]] = {ticker: [] for ticker in normalized}
        for day in self.trading_days(start, end):
            rows = self.stock_day_rows(day, normalized)
            for ticker, row in rows.items():
                by_ticker.setdefault(ticker, []).append(
                    PriceBar(
                        date=day,
                        open=to_float(row.get("open")),
                        high=to_float(row.get("high")),
                        low=to_float(row.get("low")),
                        close=to_float(row.get("close")),
                        volume=to_float(row.get("volume")),
                    )
                )
        for bars in by_ticker.values():
            bars.sort(key=lambda bar: bar.date)
        return by_ticker

    def stock_day_rows(
        self, day: date, tickers: set[str]
    ) -> dict[str, dict[str, Any]]:
        normalized = {ticker.upper() for ticker in tickers}
        if not normalized:
            return {}
        rows, missing = self._read_indexed_stock_day(day, normalized)
        if not missing:
            self.cache_stats["indexed_hits"] += 1
            return rows
        self.cache_stats["indexed_missing"] += 1
        if self.require_warm_cache:
            self.cache_stats["strict_misses"] += 1
            raise WarmCacheMiss(
                dataset="stocks-day-aggs",
                day=day,
                tickers=sorted(missing),
                reason="indexed stock cache does not cover requested tickers",
            )
        return self._build_indexed_stock_day(day, normalized)

    def option_chain(
        self,
        underlying: str,
        as_of: date,
        *,
        dte_min: int,
        dte_max: int,
        option_type: str = "put",
        price_field: str = "open",
        slippage_pct: float = 0.0,
        risk_free_rate: float = 0.04,
        stock_price: float | None = None,
        execution_model: BacktestExecutionModel | None = None,
    ) -> list[OptionQuote]:
        underlying = underlying.upper()
        chains = self.option_day_rows(as_of, {underlying}, option_type=option_type)
        rows = chains.get(underlying, [])
        options: list[OptionQuote] = []
        for row in rows:
            parsed = parse_option_symbol(str(row.get("ticker") or ""))
            if parsed is None:
                continue
            dte = (parsed.expiration - as_of).days
            if dte < dte_min or dte > dte_max:
                continue
            volume = int(to_float(row.get("volume")))
            if volume <= 0:
                continue
            raw_price = to_float(row.get(price_field))
            if raw_price <= 0:
                continue
            if execution_model is None:
                bid = ask = raw_price * max(0.0, 1.0 - slippage_pct)
            else:
                bid, ask, _spread_pct = modeled_quote_from_reference(
                    reference_price=raw_price,
                    option_type=option_type,
                    strike=parsed.strike,
                    stock_price=stock_price,
                    volume=volume,
                    execution_model=execution_model,
                )
            executable_mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
            iv = _infer_iv(
                option_type=option_type,
                price=executable_mid,
                stock_price=stock_price,
                strike=parsed.strike,
                dte=dte,
                risk_free_rate=risk_free_rate,
            )
            delta = _option_delta(
                option_type=option_type,
                stock_price=stock_price,
                strike=parsed.strike,
                dte=dte,
                implied_volatility=iv,
                risk_free_rate=risk_free_rate,
            )
            options.append(
                OptionQuote(
                    symbol=normalize_option_symbol(str(row.get("ticker") or "")),
                    expiration=parsed.expiration,
                    dte=dte,
                    strike=parsed.strike,
                    bid=bid,
                    ask=ask,
                    last=to_float(row.get("close")) or raw_price,
                    implied_volatility=iv,
                    volume=volume,
                    delta=delta,
                    data_feed=f"massive_flatfiles_options_day_aggs_{price_field}",
                )
            )
        return options

    def option_mark(
        self,
        symbol: str,
        as_of: date,
        *,
        price_field: str = "close",
        stock_price: float | None = None,
        risk_free_rate: float = 0.04,
    ) -> OptionQuote | None:
        parsed = parse_option_symbol(symbol)
        if parsed is None:
            return None
        chains = self.option_day_rows(
            as_of,
            {parsed.underlying},
            option_type=parsed.option_type,
        )
        for row in chains.get(parsed.underlying, []):
            if normalize_option_symbol(str(row.get("ticker") or "")) != normalize_option_symbol(symbol):
                continue
            dte = max((parsed.expiration - as_of).days, 0)
            price = to_float(row.get(price_field))
            if price <= 0:
                if parsed.option_type == "put":
                    price = max(0.0, parsed.strike - (stock_price or 0.0))
                else:
                    price = max(0.0, (stock_price or 0.0) - parsed.strike)
            return OptionQuote(
                symbol=normalize_option_symbol(symbol),
                expiration=parsed.expiration,
                dte=dte,
                strike=parsed.strike,
                bid=price,
                ask=price,
                last=price,
                volume=int(to_float(row.get("volume"))),
                data_feed=f"massive_flatfiles_options_day_aggs_{price_field}",
            )
        return None

    def option_day_rows(
        self,
        day: date,
        underlyings: set[str],
        *,
        option_type: str = "put",
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_underlyings = {ticker.upper() for ticker in underlyings}
        memory_hit = self._option_day_memory_hit(
            day,
            normalized_underlyings,
            option_type=option_type,
        )
        if memory_hit is not None:
            return memory_hit
        return self.prefetch_option_day_rows(
            day,
            normalized_underlyings,
            option_type=option_type,
        )

    def prefetch_option_day_rows(
        self,
        day: date,
        underlyings: set[str],
        *,
        option_type: str = "put",
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_underlyings = {ticker.upper() for ticker in underlyings}
        if not normalized_underlyings:
            return {}
        memory_hit = self._option_day_memory_hit(
            day,
            normalized_underlyings,
            option_type=option_type,
        )
        if memory_hit is not None:
            return memory_hit
        chains, missing = self._read_indexed_option_day(
            day,
            normalized_underlyings,
            option_type=option_type,
        )
        if not missing:
            self.cache_stats["indexed_hits"] += 1
            self._remember_option_day_rows(
                day,
                normalized_underlyings,
                option_type=option_type,
                chains=chains,
            )
            return chains
        self.cache_stats["indexed_missing"] += 1
        if self.require_warm_cache:
            self.cache_stats["strict_misses"] += 1
            raise WarmCacheMiss(
                dataset="options-day-aggs",
                day=day,
                option_type=option_type,
                tickers=sorted(missing),
                reason="indexed option cache does not cover requested underlyings",
            )
        chains = self._build_indexed_option_day(
            day,
            normalized_underlyings,
            option_type=option_type,
        )
        self._remember_option_day_rows(
            day,
            normalized_underlyings,
            option_type=option_type,
            chains=chains,
        )
        return chains

    def _option_day_memory_hit(
        self,
        day: date,
        underlyings: set[str],
        *,
        option_type: str,
    ) -> dict[str, list[dict[str, Any]]] | None:
        if self._option_day_memory_day is not None and self._option_day_memory_day != day:
            self._option_day_memory_cache.clear()
            self._option_day_memory_day = None
        for (cached_day, cached_type, cached_symbols), chains in self._option_day_memory_cache.items():
            if cached_day != day or cached_type != option_type:
                continue
            if underlyings.issubset(set(cached_symbols)):
                return {ticker: chains.get(ticker, []) for ticker in underlyings}
        return None

    def _remember_option_day_rows(
        self,
        day: date,
        underlyings: set[str],
        *,
        option_type: str,
        chains: dict[str, list[dict[str, Any]]],
    ) -> None:
        if self._option_day_memory_day is not None and self._option_day_memory_day != day:
            self._option_day_memory_cache.clear()
        self._option_day_memory_day = day
        if len(self._option_day_memory_cache) >= 32:
            self._option_day_memory_cache.clear()
        key = (day, option_type, tuple(sorted(underlyings)))
        self._option_day_memory_cache[key] = chains

    def iter_dataset_rows(self, dataset: str, day: date) -> Iterable[dict[str, str]]:
        raw_path = self.raw_cache_path(dataset, day)
        if raw_path.exists():
            self.cache_stats["raw_hits"] += 1
            yield from iter_csv_gzip_rows(raw_path)
            return
        if self.raw_missing_path(dataset, day).exists():
            self.cache_stats["raw_missing"] += 1
            raise FlatFileMissing(str(raw_path))
        if self.require_warm_cache:
            self.cache_stats["strict_misses"] += 1
            raise WarmCacheMiss(
                dataset=dataset,
                day=day,
                reason="raw FlatFile is not present in local cache",
            )
        self.download_raw_flatfile(dataset, day)
        self.cache_stats["raw_downloads"] += 1
        yield from iter_csv_gzip_rows(raw_path)

    def warmup_day(
        self,
        day: date,
        tickers: set[str],
        *,
        datasets: Sequence[str] = ("stocks-day-aggs", "options-day-aggs"),
        option_types: Sequence[str] = ("put", "call"),
    ) -> dict[str, Any]:
        if self.require_warm_cache:
            raise BacktestDataError("warmup_day requires a non-strict FlatFilesStore")
        normalized = {ticker.upper() for ticker in tickers}
        result: dict[str, Any] = {
            "date": day.isoformat(),
            "tickers": len(normalized),
            "datasets": {},
        }
        if "stocks-day-aggs" in datasets:
            rows = self._build_indexed_stock_day(day, normalized)
            result["datasets"]["stocks-day-aggs"] = {"rows": len(rows)}
        if "options-day-aggs" in datasets:
            option_result: dict[str, Any] = {}
            for option_type in option_types:
                chains = self._build_indexed_option_day(
                    day,
                    normalized,
                    option_type=option_type,
                )
                option_result[option_type] = {
                    "rows": sum(len(rows) for rows in chains.values())
                }
            result["datasets"]["options-day-aggs"] = option_result
        return result

    def cache_stats_snapshot(self) -> dict[str, int]:
        return dict(self.cache_stats)

    def raw_cache_path(self, dataset: str, day: date) -> Path:
        return (
            self.raw_cache_dir
            / dataset_cache_dir(dataset)
            / f"{day.year:04d}"
            / f"{day.month:02d}"
            / f"{day.isoformat()}.csv.gz"
        )

    def raw_missing_path(self, dataset: str, day: date) -> Path:
        path = self.raw_cache_path(dataset, day)
        return path.with_name(path.name + ".missing.json")

    def indexed_cache_path(self, dataset: str, day: date) -> Path:
        return (
            self.indexed_cache_dir
            / dataset_cache_dir(dataset)
            / f"{day.year:04d}"
            / f"{day.month:02d}"
            / f"{day.isoformat()}.parquet"
        )

    def indexed_manifest_path(self, dataset: str, day: date) -> Path:
        path = self.indexed_cache_path(dataset, day)
        return path.with_name(path.name + ".manifest.json")

    def indexed_missing_path(self, dataset: str, day: date) -> Path:
        path = self.indexed_cache_path(dataset, day)
        return path.with_name(path.name + ".missing.json")

    def download_raw_flatfile(self, dataset: str, day: date) -> None:
        key = key_for_dataset(dataset, day)
        uri = f"s3://{self.bucket}/{key}"
        raw_path = self.raw_cache_path(dataset, day)
        missing_path = self.raw_missing_path(dataset, day)
        lock_path = raw_path.with_name(raw_path.name + ".lock_target")
        with cache_file_lock(
            lock_path,
            timeout_seconds=max(
                self.cache_timeout_seconds,
                DEFAULT_CACHE_LOCK_TIMEOUT_SECONDS,
            ),
        ):
            if raw_path.exists():
                return
            if missing_path.exists():
                raise FlatFileMissing(uri)
            ensure_parent_dir(raw_path, timeout_seconds=self.cache_timeout_seconds)
            temp_path = raw_path.with_name(
                f"{raw_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
            )
            cmd = [
                self.aws,
                "s3",
                "cp",
                uri,
                str(temp_path),
                "--endpoint-url",
                self.endpoint_url,
                "--no-progress",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as exc:
                raise BacktestDataError(f"failed to run aws CLI for {uri}: {exc}") from exc
            if proc.returncode != 0:
                temp_path.unlink(missing_ok=True)
                if is_missing_flatfile(proc.stderr):
                    write_json_atomic(
                        missing_path,
                        {
                            "missing": True,
                            "dataset": dataset,
                            "date": day.isoformat(),
                            "source_uri": uri,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        },
                        timeout_seconds=self.cache_timeout_seconds,
                    )
                    raise FlatFileMissing(uri)
                raise BacktestDataError(
                    f"aws s3 cp failed for {uri}: {(proc.stderr or '')[:500]}"
                )
            os.replace(temp_path, raw_path)

    def _read_indexed_stock_day(
        self,
        day: date,
        tickers: set[str],
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        if self.indexed_missing_path("stocks-day-aggs", day).exists():
            return {}, set()
        coverage = self._stock_coverage(day)
        if not coverage:
            return {}, set(tickers)
        missing = set(tickers) - coverage
        rows = {
            str(row.get("ticker") or "").upper(): row
            for row in read_parquet_rows_if_exists(
                self.indexed_cache_path("stocks-day-aggs", day)
            )
        }
        return {ticker: rows[ticker] for ticker in tickers if ticker in rows}, missing

    def _build_indexed_stock_day(
        self,
        day: date,
        tickers: set[str],
    ) -> dict[str, dict[str, Any]]:
        if self.require_warm_cache:
            raise WarmCacheMiss(
                dataset="stocks-day-aggs",
                day=day,
                tickers=sorted(tickers),
                reason="strict mode cannot build indexed stock cache",
            )
        cache_path = self.indexed_cache_path("stocks-day-aggs", day)
        with cache_file_lock(
            cache_path,
            timeout_seconds=max(
                self.cache_timeout_seconds,
                DEFAULT_CACHE_LOCK_TIMEOUT_SECONDS,
            ),
        ):
            rows, missing = self._read_indexed_stock_day(day, tickers)
            if not missing:
                return rows
            existing_rows = read_parquet_rows_if_exists(cache_path)
            by_ticker = {
                str(row.get("ticker") or "").upper(): row
                for row in existing_rows
                if row.get("ticker")
            }
            try:
                for row in self.iter_dataset_rows("stocks-day-aggs", day):
                    ticker = str(row.get("ticker") or "").upper()
                    if ticker not in missing:
                        continue
                    normalized = normalize_parquet_row(row)
                    normalized["ticker"] = ticker
                    by_ticker[ticker] = normalized
            except FlatFileMissing:
                if not cache_path.exists():
                    write_json_atomic(
                        self.indexed_missing_path("stocks-day-aggs", day),
                        {
                            "missing": True,
                            "dataset": "stocks-day-aggs",
                            "date": day.isoformat(),
                            "schema_version": INDEXED_CACHE_SCHEMA_VERSION,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        },
                        timeout_seconds=self.cache_timeout_seconds,
                    )
                return {
                    ticker: by_ticker[ticker]
                    for ticker in tickers
                    if ticker in by_ticker
                }
            write_parquet_rows_atomic(
                cache_path,
                list(by_ticker.values()),
                default_columns=["ticker", "open", "high", "low", "close", "volume"],
                timeout_seconds=self.cache_timeout_seconds,
            )
            manifest = self._read_indexed_manifest("stocks-day-aggs", day)
            covered = set(manifest.get("tickers") or [])
            covered.update(tickers)
            self._write_indexed_manifest(
                "stocks-day-aggs",
                day,
                {
                    **manifest,
                    "schema_version": INDEXED_CACHE_SCHEMA_VERSION,
                    "dataset": "stocks-day-aggs",
                    "date": day.isoformat(),
                    "tickers": sorted(covered),
                    "row_count": len(by_ticker),
                    "raw_file": self._raw_file_manifest("stocks-day-aggs", day),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self.cache_stats["indexed_builds"] += 1
            return {
                ticker: by_ticker[ticker]
                for ticker in tickers
                if ticker in by_ticker
            }

    def _read_indexed_option_day(
        self,
        day: date,
        underlyings: set[str],
        *,
        option_type: str,
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        if self.indexed_missing_path("options-day-aggs", day).exists():
            return {ticker: [] for ticker in underlyings}, set()
        coverage = self._option_coverage(day, option_type)
        if not coverage:
            return {}, set(underlyings)
        missing = set(underlyings) - coverage
        chains = {ticker: [] for ticker in underlyings}
        for row in read_parquet_rows_if_exists(
            self.indexed_cache_path("options-day-aggs", day),
            columns=OPTION_DAY_READ_COLUMNS,
            filters=[
                ("underlying", "in", sorted(underlyings)),
                ("option_type", "==", option_type),
            ],
        ):
            underlying = str(row.get("underlying") or "").upper()
            row_type = str(row.get("option_type") or "").lower()
            if underlying in underlyings and row_type == option_type:
                chains[underlying].append(row)
        return chains, missing

    def _build_indexed_option_day(
        self,
        day: date,
        underlyings: set[str],
        *,
        option_type: str,
    ) -> dict[str, list[dict[str, Any]]]:
        if self.require_warm_cache:
            raise WarmCacheMiss(
                dataset="options-day-aggs",
                day=day,
                option_type=option_type,
                tickers=sorted(underlyings),
                reason="strict mode cannot build indexed option cache",
            )
        cache_path = self.indexed_cache_path("options-day-aggs", day)
        with cache_file_lock(
            cache_path,
            timeout_seconds=max(
                self.cache_timeout_seconds,
                DEFAULT_CACHE_LOCK_TIMEOUT_SECONDS,
            ),
        ):
            chains, missing = self._read_indexed_option_day(
                day,
                underlyings,
                option_type=option_type,
            )
            if not missing:
                return chains
            existing_rows = read_parquet_rows_if_exists(cache_path)
            remaining_rows = [
                row
                for row in existing_rows
                if not (
                    str(row.get("underlying") or "").upper() in missing
                    and str(row.get("option_type") or "").lower() == option_type
                )
            ]
            new_rows: list[dict[str, Any]] = []
            try:
                for row in self.iter_dataset_rows("options-day-aggs", day):
                    parsed = parse_option_symbol(str(row.get("ticker") or ""))
                    if parsed is None or parsed.option_type != option_type:
                        continue
                    if parsed.underlying not in missing:
                        continue
                    normalized = normalize_parquet_row(row)
                    normalized.update(
                        {
                            "underlying": parsed.underlying,
                            "expiration": parsed.expiration.isoformat(),
                            "option_type": parsed.option_type,
                            "strike": parsed.strike,
                            "normalized_ticker": normalize_option_symbol(parsed.raw_symbol),
                        }
                    )
                    new_rows.append(normalized)
            except FlatFileMissing:
                if not cache_path.exists():
                    write_json_atomic(
                        self.indexed_missing_path("options-day-aggs", day),
                        {
                            "missing": True,
                            "dataset": "options-day-aggs",
                            "date": day.isoformat(),
                            "schema_version": INDEXED_CACHE_SCHEMA_VERSION,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        },
                        timeout_seconds=self.cache_timeout_seconds,
                    )
                chains = {ticker: [] for ticker in underlyings}
                for row in existing_rows:
                    underlying = str(row.get("underlying") or "").upper()
                    row_type = str(row.get("option_type") or "").lower()
                    if underlying in underlyings and row_type == option_type:
                        chains[underlying].append(row)
                return chains
            all_rows = remaining_rows + new_rows
            write_parquet_rows_atomic(
                cache_path,
                all_rows,
                default_columns=[
                    "ticker",
                    "underlying",
                    "expiration",
                    "option_type",
                    "strike",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "normalized_ticker",
                ],
                timeout_seconds=self.cache_timeout_seconds,
            )
            manifest = self._read_indexed_manifest("options-day-aggs", day)
            option_coverage = dict(manifest.get("option_types") or {})
            type_coverage = set(option_coverage.get(option_type) or [])
            type_coverage.update(underlyings)
            option_coverage[option_type] = sorted(type_coverage)
            self._write_indexed_manifest(
                "options-day-aggs",
                day,
                {
                    **manifest,
                    "schema_version": INDEXED_CACHE_SCHEMA_VERSION,
                    "dataset": "options-day-aggs",
                    "date": day.isoformat(),
                    "option_types": option_coverage,
                    "row_count": len(all_rows),
                    "raw_file": self._raw_file_manifest("options-day-aggs", day),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self.cache_stats["indexed_builds"] += 1
            chains = {ticker: [] for ticker in underlyings}
            for row in all_rows:
                underlying = str(row.get("underlying") or "").upper()
                row_type = str(row.get("option_type") or "").lower()
                if underlying in underlyings and row_type == option_type:
                    chains[underlying].append(row)
            return chains

    def _read_indexed_manifest(self, dataset: str, day: date) -> dict[str, Any]:
        payload = read_json_if_exists(self.indexed_manifest_path(dataset, day))
        return payload if isinstance(payload, dict) else {}

    def _write_indexed_manifest(
        self,
        dataset: str,
        day: date,
        payload: dict[str, Any],
    ) -> None:
        write_json_atomic(
            self.indexed_manifest_path(dataset, day),
            payload,
            timeout_seconds=self.cache_timeout_seconds,
        )

    def _stock_coverage(self, day: date) -> set[str]:
        manifest = self._read_indexed_manifest("stocks-day-aggs", day)
        if manifest.get("schema_version") == INDEXED_CACHE_SCHEMA_VERSION:
            return {str(ticker).upper() for ticker in manifest.get("tickers") or []}
        return set()

    def _option_coverage(self, day: date, option_type: str) -> set[str]:
        manifest = self._read_indexed_manifest("options-day-aggs", day)
        if manifest.get("schema_version") == INDEXED_CACHE_SCHEMA_VERSION:
            option_types = manifest.get("option_types") or {}
            return {
                str(ticker).upper()
                for ticker in option_types.get(option_type) or []
            }
        return set()

    def _raw_file_manifest(self, dataset: str, day: date) -> dict[str, Any]:
        path = self.raw_cache_path(dataset, day)
        payload: dict[str, Any] = {
            "path": str(path),
            "source_key": key_for_dataset(dataset, day),
        }
        try:
            stat = path.stat()
        except OSError:
            return payload
        payload.update(
            {
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(
                    stat.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            }
        )
        return payload

    def _cache_path(self, namespace: str, day: date, symbols: set[str]) -> Path:
        digest = stable_digest("_".join(sorted(symbols)))
        return (
            self.cache_dir
            / namespace
            / f"{day.year:04d}"
            / f"{day.month:02d}"
            / f"{day.isoformat()}_{digest}.json"
        )


def key_for_dataset(dataset: str, day: date) -> str:
    if dataset == "stocks-day-aggs":
        return (
            f"us_stocks_sip/day_aggs_v1/"
            f"{day.year:04d}/{day.month:02d}/{day.isoformat()}.csv.gz"
        )
    if dataset == "options-day-aggs":
        return (
            f"us_options_opra/day_aggs_v1/"
            f"{day.year:04d}/{day.month:02d}/{day.isoformat()}.csv.gz"
        )
    raise BacktestDataError(f"unsupported flatfile dataset: {dataset}")


def dataset_cache_dir(dataset: str) -> str:
    try:
        return DATASET_CACHE_DIRS[dataset]
    except KeyError as exc:
        raise BacktestDataError(f"unsupported flatfile dataset: {dataset}") from exc


def iter_csv_gzip_rows(path: Path) -> Iterable[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


PARQUET_FLOAT_COLUMNS = {
    "open",
    "high",
    "low",
    "close",
    "vwap",
    "strike",
}
PARQUET_INT_COLUMNS = {
    "volume",
    "transactions",
    "window_start",
    "open_interest",
}
OPTION_DAY_READ_COLUMNS = [
    "ticker",
    "underlying",
    "expiration",
    "option_type",
    "strike",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "normalized_ticker",
]


def normalize_parquet_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): normalize_parquet_value(str(key), value)
        for key, value in row.items()
    }


def normalize_parquet_value(column: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if column in PARQUET_FLOAT_COLUMNS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if column in PARQUET_INT_COLUMNS:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return str(value)


def read_parquet_rows_if_exists(
    path: Path,
    *,
    columns: Sequence[str] | None = None,
    filters: Any | None = None,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on local environment.
        raise BacktestDataError(
            "pyarrow is required for FlatFiles indexed Parquet cache; "
            "install project dependencies before running backtests."
        ) from exc
    table = pq.read_table(path, columns=columns, filters=filters)
    return table.to_pylist()


def write_parquet_rows_atomic(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    default_columns: Sequence[str],
    timeout_seconds: float = 15.0,
) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on local environment.
        raise BacktestDataError(
            "pyarrow is required for FlatFiles indexed Parquet cache; "
            "install project dependencies before running backtests."
        ) from exc
    ensure_parent_dir(path, timeout_seconds=timeout_seconds)
    columns = sorted({str(key) for row in rows for key in row} | set(default_columns))
    data = {
        column: [normalize_parquet_value(column, row.get(column)) for row in rows]
        for column in columns
    }
    schema = pa.schema(
        [
            pa.field(column, parquet_column_type(pa, column))
            for column in columns
        ]
    )
    table = pa.table(data, schema=schema)
    temp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        pq.write_table(table, temp_path, compression="zstd")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def parquet_column_type(pa: Any, column: str) -> Any:
    if column in PARQUET_FLOAT_COLUMNS:
        return pa.float64()
    if column in PARQUET_INT_COLUMNS:
        return pa.int64()
    return pa.string()


def parse_option_symbol(symbol: str) -> ParsedOptionSymbol | None:
    text = str(symbol or "").strip().upper()
    if text.startswith("O:"):
        text = text[2:]
    if len(text) < 16:
        return None
    tail = text[-15:]
    underlying = text[:-15]
    if (
        not underlying
        or not tail[:6].isdigit()
        or tail[6] not in {"C", "P"}
        or not tail[7:].isdigit()
    ):
        return None
    return ParsedOptionSymbol(
        raw_symbol=symbol,
        underlying=underlying,
        expiration=date.fromisoformat(f"20{tail[:2]}-{tail[2:4]}-{tail[4:6]}"),
        option_type="call" if tail[6] == "C" else "put",
        strike=int(tail[7:]) / 1000.0,
    )


def normalize_option_symbol(value: str) -> str:
    text = str(value or "").strip().upper()
    return text[2:] if text.startswith("O:") else text


def detect_price_space_breaks(
    bars: list[PriceBar],
    *,
    ratio_low: float = 0.75,
    ratio_high: float = 1.25,
) -> list[dict[str, Any]]:
    breaks: list[dict[str, Any]] = []
    previous: PriceBar | None = None
    for bar in sorted(bars, key=lambda row: row.date):
        if previous is not None and previous.close > 0:
            ratio = bar.close / previous.close
            if ratio < ratio_low or ratio > ratio_high:
                breaks.append(
                    {
                        "date": bar.date.isoformat(),
                        "previous_close": previous.close,
                        "close": bar.close,
                        "ratio": ratio,
                    }
                )
        previous = bar
    return breaks


def date_range(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def ensure_parent_dir(path: Path, timeout_seconds: float = 15.0) -> None:
    try:
        subprocess.run(
            ["mkdir", "-p", str(path.parent)],
            check=True,
            timeout=timeout_seconds,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise BacktestDataError(
            f"timed out creating directory {path.parent}; "
            "check that the volume is mounted and writable"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise BacktestDataError(
            f"failed to create directory {path.parent}: {(exc.stderr or '').strip()}"
        ) from exc


def write_json_atomic(
    path: Path,
    payload: Any,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    ensure_parent_dir(path, timeout_seconds)
    temp_path = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    data = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    script = (
        "from pathlib import Path\n"
        "import os, sys\n"
        "tmp = Path(sys.argv[1])\n"
        "dst = Path(sys.argv[2])\n"
        "tmp.write_text(sys.stdin.read(), encoding='utf-8')\n"
        "os.replace(tmp, dst)\n"
    )
    try:
        subprocess.run(
            [sys.executable, "-c", script, str(temp_path), str(path)],
            input=data,
            check=True,
            timeout=timeout_seconds,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise BacktestDataError(f"timed out writing cache file {path}") from exc
    except subprocess.CalledProcessError as exc:
        raise BacktestDataError(
            f"failed to write cache file {path}: {(exc.stderr or '').strip()}"
        ) from exc
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def read_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except JSONDecodeError:
        quarantine_corrupt_json(path)
        return None


@contextmanager
def cache_file_lock(path: Path, *, timeout_seconds: float = 15.0):
    if fcntl is None:
        raise BacktestDataError(
            "FlatFiles cache locking requires POSIX fcntl; run backtests on macOS or Linux."
        )
    ensure_parent_dir(path, timeout_seconds)
    # Keep lock files stable under .locks/. Unlinking an active lock path can split
    # waiters across different inodes and allow duplicate writers.
    lock_path = path.parent / ".locks" / f"{path.name}.lock"
    ensure_parent_dir(lock_path, timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise BacktestDataError(f"timed out acquiring cache lock {lock_path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def quarantine_corrupt_json(path: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    quarantine = path.with_name(f"{path.name}.corrupt.{stamp}.{os.getpid()}")
    try:
        os.replace(path, quarantine)
    except OSError:
        pass


def infer_put_iv(
    *,
    price: float,
    stock_price: float | None,
    strike: float,
    dte: int,
    risk_free_rate: float,
) -> float | None:
    if (
        price <= 0
        or stock_price is None
        or stock_price <= 0
        or strike <= 0
        or dte <= 0
    ):
        return None
    low = 0.0001
    high = 5.0
    tolerance = 1e-6
    low_price = black_scholes_put_price(stock_price, strike, dte, low, risk_free_rate)
    if low_price is None or low_price > price + tolerance:
        return None
    high_price = black_scholes_put_price(stock_price, strike, dte, high, risk_free_rate)
    while high_price is not None and high_price < price and high < 20.0:
        high *= 2
        high_price = black_scholes_put_price(stock_price, strike, dte, high, risk_free_rate)
    if high_price is None or high_price < price:
        return None
    for _ in range(50):
        mid = (low + high) / 2.0
        mid_price = black_scholes_put_price(stock_price, strike, dte, mid, risk_free_rate)
        if mid_price is None:
            return None
        if abs(mid_price - price) <= tolerance:
            return mid
        if mid_price < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def infer_call_iv(
    *,
    price: float,
    stock_price: float | None,
    strike: float,
    dte: int,
    risk_free_rate: float,
) -> float | None:
    if (
        price <= 0
        or stock_price is None
        or stock_price <= 0
        or strike <= 0
        or dte <= 0
    ):
        return None
    low = 0.0001
    high = 5.0
    tolerance = 1e-6
    low_price = black_scholes_call_price(stock_price, strike, dte, low, risk_free_rate)
    if low_price is None or low_price > price + tolerance:
        return None
    high_price = black_scholes_call_price(stock_price, strike, dte, high, risk_free_rate)
    while high_price is not None and high_price < price and high < 20.0:
        high *= 2
        high_price = black_scholes_call_price(stock_price, strike, dte, high, risk_free_rate)
    if high_price is None or high_price < price:
        return None
    for _ in range(50):
        mid = (low + high) / 2.0
        mid_price = black_scholes_call_price(stock_price, strike, dte, mid, risk_free_rate)
        if mid_price is None:
            return None
        if abs(mid_price - price) <= tolerance:
            return mid
        if mid_price < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _infer_iv(
    *,
    option_type: str,
    price: float,
    stock_price: float | None,
    strike: float,
    dte: int,
    risk_free_rate: float,
) -> float | None:
    if stock_price is None:
        return None
    if option_type == "put":
        return infer_put_iv(
            price=price,
            stock_price=stock_price,
            strike=strike,
            dte=dte,
            risk_free_rate=risk_free_rate,
        )
    if option_type == "call":
        return infer_call_iv(
            price=price,
            stock_price=stock_price,
            strike=strike,
            dte=dte,
            risk_free_rate=risk_free_rate,
        )
    return None


def _option_delta(
    *,
    option_type: str,
    stock_price: float | None,
    strike: float,
    dte: int,
    implied_volatility: float | None,
    risk_free_rate: float,
) -> float | None:
    if stock_price is None or implied_volatility is None:
        return None
    if option_type == "put":
        return black_scholes_put_delta(
            stock_price=stock_price,
            strike=strike,
            dte=dte,
            implied_volatility=implied_volatility,
            risk_free_rate=risk_free_rate,
        )
    if option_type == "call":
        return black_scholes_call_delta(
            stock_price=stock_price,
            strike=strike,
            dte=dte,
            implied_volatility=implied_volatility,
            risk_free_rate=risk_free_rate,
        )
    return None


def black_scholes_put_price(
    stock_price: float,
    strike: float,
    dte: int,
    implied_volatility: float,
    risk_free_rate: float,
) -> float | None:
    if stock_price <= 0 or strike <= 0 or dte <= 0 or implied_volatility <= 0:
        return None
    t = dte / 365.0
    sigma_sqrt_t = implied_volatility * math.sqrt(t)
    if sigma_sqrt_t <= 0:
        return None
    d1 = (
        math.log(stock_price / strike)
        + (risk_free_rate + 0.5 * implied_volatility * implied_volatility) * t
    ) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    return (
        strike * math.exp(-risk_free_rate * t) * norm_cdf(-d2)
        - stock_price * norm_cdf(-d1)
    )


def black_scholes_call_price(
    stock_price: float,
    strike: float,
    dte: int,
    implied_volatility: float,
    risk_free_rate: float,
) -> float | None:
    if stock_price <= 0 or strike <= 0 or dte <= 0 or implied_volatility <= 0:
        return None
    t = dte / 365.0
    sigma_sqrt_t = implied_volatility * math.sqrt(t)
    if sigma_sqrt_t <= 0:
        return None
    d1 = (
        math.log(stock_price / strike)
        + (risk_free_rate + 0.5 * implied_volatility * implied_volatility) * t
    ) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    return (
        stock_price * norm_cdf(d1)
        - strike * math.exp(-risk_free_rate * t) * norm_cdf(d2)
    )


def is_missing_flatfile(stderr: str) -> bool:
    text = stderr.lower()
    return "404" in text or "not found" in text or "nosuchkey" in text


def stable_digest(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def to_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def timestamp_to_date_millis(value: Any) -> date | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date()
