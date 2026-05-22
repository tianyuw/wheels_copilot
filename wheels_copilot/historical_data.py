from __future__ import annotations

import csv
import gzip
import io
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from .models import OptionQuote, PriceBar
from .option_math import black_scholes_call_delta, black_scholes_put_delta, norm_cdf


DEFAULT_FLATFILES_CACHE_DIR = Path("/Volumes/Data/wheels_copilot/flatfiles_cache")
DEFAULT_ENDPOINT_URL = "https://files.massive.com"
DEFAULT_BUCKET = "flatfiles"


class BacktestDataError(RuntimeError):
    pass


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
        aws: str = "aws",
        endpoint_url: str = DEFAULT_ENDPOINT_URL,
        bucket: str = DEFAULT_BUCKET,
        cache_timeout_seconds: float = 15.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.aws = aws
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.cache_timeout_seconds = cache_timeout_seconds
        self._option_day_memory_cache: dict[
            tuple[date, str, tuple[str, ...]], dict[str, list[dict[str, Any]]]
        ] = {}
        self._option_day_memory_day: date | None = None

    def preflight_cache(self) -> CachePreflightResult:
        probe = self.cache_dir / ".preflight" / "write_probe.txt"
        try:
            ensure_parent_dir(probe, timeout_seconds=self.cache_timeout_seconds)
            script = (
                "from pathlib import Path\n"
                "import os, sys\n"
                "p = Path(sys.argv[1])\n"
                "p.write_text('flatfiles-cache-probe\\n', encoding='utf-8')\n"
                "assert p.read_text(encoding='utf-8') == 'flatfiles-cache-probe\\n'\n"
                "p.unlink()\n"
                "try:\n"
                "    p.parent.rmdir()\n"
                "except OSError:\n"
                "    pass\n"
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
                cache_dir=self.cache_dir,
                ok=False,
                reason="cache write/read/delete timed out",
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            return CachePreflightResult(
                cache_dir=self.cache_dir,
                ok=False,
                reason=str(exc),
            )
        return CachePreflightResult(cache_dir=self.cache_dir, ok=True)

    def require_writable_cache(self) -> None:
        result = self.preflight_cache()
        if not result.ok:
            raise BacktestDataError(
                f"cache directory is not writable: {result.cache_dir} ({result.reason})"
            )

    def trading_days(self, start: date, end: date) -> list[date]:
        return [day for day in date_range(start, end) if day.weekday() < 5]

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
        cache_path = self._cache_path("stocks_day", day, tickers)
        cached = read_json_if_exists(cache_path)
        if cached is not None:
            return {} if cached.get("missing") else cached.get("rows", {})

        rows: dict[str, dict[str, Any]] = {}
        try:
            for row in self.iter_dataset_rows("stocks-day-aggs", day):
                ticker = str(row.get("ticker") or "").upper()
                if ticker in tickers:
                    rows[ticker] = row
        except FlatFileMissing:
            write_json_atomic(
                cache_path,
                {"missing": True, "rows": {}},
                timeout_seconds=self.cache_timeout_seconds,
            )
            return {}
        write_json_atomic(
            cache_path,
            {"missing": False, "rows": rows},
            timeout_seconds=self.cache_timeout_seconds,
        )
        return rows

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
            executable_price = raw_price * max(0.0, 1.0 - slippage_pct)
            iv = _infer_iv(
                option_type=option_type,
                price=executable_price,
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
                    bid=executable_price,
                    ask=executable_price,
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
        memory_hit = self._option_day_memory_hit(
            day,
            normalized_underlyings,
            option_type=option_type,
        )
        if memory_hit is not None:
            return memory_hit
        cache_path = self._cache_path(
            f"options_day_{option_type}s", day, normalized_underlyings
        )
        cached = read_json_if_exists(cache_path)
        if cached is not None:
            chains = {} if cached.get("missing") else cached.get("chains", {})
            self._remember_option_day_rows(
                day,
                normalized_underlyings,
                option_type=option_type,
                chains=chains,
            )
            return chains

        chains: dict[str, list[dict[str, Any]]] = {
            ticker: [] for ticker in normalized_underlyings
        }
        try:
            for row in self.iter_dataset_rows("options-day-aggs", day):
                parsed = parse_option_symbol(str(row.get("ticker") or ""))
                if parsed is None or parsed.option_type != option_type:
                    continue
                if parsed.underlying not in normalized_underlyings:
                    continue
                normalized = dict(row)
                normalized.update(
                    {
                        "underlying": parsed.underlying,
                        "expiration": parsed.expiration.isoformat(),
                        "option_type": parsed.option_type,
                        "strike": parsed.strike,
                        "normalized_ticker": normalize_option_symbol(parsed.raw_symbol),
                    }
                )
                chains[parsed.underlying].append(normalized)
        except FlatFileMissing:
            write_json_atomic(
                cache_path,
                {"missing": True, "chains": {}},
                timeout_seconds=self.cache_timeout_seconds,
            )
            self._remember_option_day_rows(
                day,
                normalized_underlyings,
                option_type=option_type,
                chains={},
            )
            return {}
        write_json_atomic(
            cache_path,
            {"missing": False, "chains": chains},
            timeout_seconds=self.cache_timeout_seconds,
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
        key = (day, option_type, tuple(sorted(underlyings)))
        self._option_day_memory_cache[key] = chains

    def iter_dataset_rows(self, dataset: str, day: date) -> Iterable[dict[str, str]]:
        key = key_for_dataset(dataset, day)
        uri = f"s3://{self.bucket}/{key}"
        cmd = [
            self.aws,
            "s3",
            "cp",
            uri,
            "-",
            "--endpoint-url",
            self.endpoint_url,
            "--no-progress",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        assert proc.stderr is not None
        try:
            with gzip.GzipFile(fileobj=proc.stdout) as gz:
                with io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
                    yield from csv.DictReader(text)
        except (EOFError, gzip.BadGzipFile) as exc:
            stderr = proc.stderr.read().decode("utf-8", errors="replace")
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if is_missing_flatfile(stderr):
                raise FlatFileMissing(uri) from exc
            raise BacktestDataError(f"failed to read {uri}: {stderr[:500]}") from exc
        except Exception:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
        if proc.poll() is None:
            proc.wait()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        if proc.returncode not in (0, None):
            if is_missing_flatfile(stderr):
                raise FlatFileMissing(uri)
            raise BacktestDataError(f"aws s3 cp failed for {uri}: {stderr[:500]}")

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
    temp_path = path.with_name(path.name + ".tmp")
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


def read_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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
