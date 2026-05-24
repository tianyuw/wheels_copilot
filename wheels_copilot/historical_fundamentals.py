from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib import error, parse, request

from .historical_data import read_json_if_exists, write_json_atomic
from .models import FundamentalFieldProvenance, FundamentalSnapshot, PriceBar


DEFAULT_FUNDAMENTALS_CACHE_DIR = Path("/Volumes/Data/wheels_copilot/fundamentals_cache")
MASSIVE_BASE_URL = "https://api.massive.com"
UNUSUAL_WHALES_BASE_URL = "https://api.unusualwhales.com"

QUALITY_STRICT_PIT = "strict_pit"
QUALITY_STRICT_PIT_PENDING = "strict_pit_pending_validation"
QUALITY_APPROXIMATE = "approximate"
QUALITY_UNAVAILABLE = "unavailable"
QUALITY_PLAN_GATED = "plan_gated"


class HistoricalFundamentalProvider(Protocol):
    name: str

    def preload(self, tickers: Iterable[str], start: date, end: date) -> None:
        ...

    def apply(
        self,
        snapshot: FundamentalSnapshot,
        *,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> None:
        ...

    def diagnostics(self) -> dict[str, Any]:
        ...


class HistoricalFundamentalStore:
    def __init__(self, providers: list[HistoricalFundamentalProvider]) -> None:
        self.providers = providers
        self._preloaded = False

    def preload(self, tickers: Iterable[str], start: date, end: date) -> None:
        normalized = sorted({str(ticker).strip().upper() for ticker in tickers if ticker})
        for provider in self.providers:
            provider.preload(normalized, start, end)
        self._preloaded = True

    def snapshot(
        self,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> FundamentalSnapshot:
        snapshot = FundamentalSnapshot(ticker=ticker.upper())
        for provider in self.providers:
            provider.apply(snapshot, ticker=ticker.upper(), as_of=as_of, bars=bars)
        _mark_missing_core_fields(snapshot, as_of)
        return snapshot

    def diagnostics(self) -> dict[str, Any]:
        return {
            "preloaded": self._preloaded,
            "providers": {
                provider.name: provider.diagnostics() for provider in self.providers
            },
        }


def build_historical_fundamental_store(
    *,
    config: dict[str, Any] | None = None,
    cache_dir: Path = DEFAULT_FUNDAMENTALS_CACHE_DIR,
    env_file: Path | None = None,
    timeout_seconds: float = 30.0,
) -> HistoricalFundamentalStore:
    config = config or {}
    hist_cfg = config.get("historical_fundamentals", {})
    env = load_env(env_file)
    massive_key = first_present(env, "MASIVE_API_KEY", "MASSIVE_API_KEY", "POLYGON_API_KEY")
    uw_key = first_present(env, "UNUSUAL_WHALES_API_KEY", "UW_API_TOKEN", "UNUSUAL_WHALES_TOKEN")

    providers: list[HistoricalFundamentalProvider] = [
        PriceDerivedFundamentalsProvider(),
    ]
    if massive_key:
        massive = MassiveRestClient(api_key=massive_key, timeout=timeout_seconds)
        providers.extend(
            [
                MassiveSecFinancialsProvider(
                    client=massive,
                    cache_dir=cache_dir / "massive_sec_financials",
                ),
                MassiveDividendProvider(
                    client=massive,
                    cache_dir=cache_dir / "massive_dividends",
                ),
                MassiveTickerReferenceProvider(
                    client=massive,
                    cache_dir=cache_dir / "massive_ticker_reference",
                ),
            ]
        )
    if uw_key and hist_cfg.get("enable_unusual_whales_earnings", True):
        providers.append(
            UnusualWhalesEarningsProvider(
                client=UnusualWhalesRestClient(api_key=uw_key, timeout=timeout_seconds),
                cache_dir=cache_dir / "unusual_whales_earnings",
                known_days_before=int(hist_cfg.get("earnings_known_days_before", 21)),
            )
        )
    return HistoricalFundamentalStore(providers)


class PriceDerivedFundamentalsProvider:
    name = "price_derived"

    def preload(self, tickers: Iterable[str], start: date, end: date) -> None:
        return None

    def apply(
        self,
        snapshot: FundamentalSnapshot,
        *,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> None:
        prior_bars = [bar for bar in bars if bar.date < as_of]
        recent_move = recent_move_pct(prior_bars)
        snapshot.recent_move_pct = recent_move
        snapshot.provenance["recent_move_pct"] = FundamentalFieldProvenance(
            value=recent_move,
            source="historical_stock_bars",
            as_of=as_of,
            effective_date=prior_bars[-1].date if prior_bars else None,
            known_at=prior_bars[-1].date if prior_bars else None,
            quality=QUALITY_STRICT_PIT if recent_move is not None else QUALITY_UNAVAILABLE,
            staleness_days=(as_of - prior_bars[-1].date).days if prior_bars else None,
            is_stale=False,
            fallback_used=False,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {"source": "historical_stock_bars"}


class MassiveSecFinancialsProvider:
    name = "massive_sec_financials"

    def __init__(
        self,
        *,
        client: "MassiveRestClient | None" = None,
        cache_dir: Path | None = None,
        seed_rows: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir
        self.rows_by_ticker = {
            ticker.upper(): list(rows) for ticker, rows in (seed_rows or {}).items()
        }
        self.errors: dict[str, str] = {}

    def preload(self, tickers: Iterable[str], start: date, end: date) -> None:
        if self.client is None:
            return
        for ticker in tickers:
            if ticker in self.rows_by_ticker or ticker in self.errors:
                continue
            try:
                self.rows_by_ticker[ticker] = self._load_rows(ticker)
            except RestClientError as exc:
                self.errors[ticker] = str(exc)

    def apply(
        self,
        snapshot: FundamentalSnapshot,
        *,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> None:
        rows = _infer_q4_known_at_from_annual(self.rows_by_ticker.get(ticker, []))
        filings = [
            row
            for row in rows
            if _filing_known_at(row) is not None and _filing_known_at(row) <= as_of
        ]
        if not filings:
            _set_unavailable(snapshot, "quarterly_net_income", as_of, self.name)
            _set_unavailable(snapshot, "annual_net_income", as_of, self.name)
            _set_unavailable(snapshot, "pe_ratio", as_of, self.name)
            return

        quarterly = _latest_distinct_filings(
            [row for row in filings if str(row.get("timeframe") or "").lower() == "quarterly"]
        )
        annual = _latest_distinct_filings(
            [row for row in filings if str(row.get("timeframe") or "").lower() == "annual"]
        )
        snapshot.quarterly_net_income = [
            value
            for row in quarterly[:5]
            if (value := _income_value(row)) is not None
        ]
        snapshot.annual_net_income = [
            value
            for row in annual[:5]
            if (value := _income_value(row)) is not None
        ]
        latest_known = _filing_known_at(quarterly[0]) if quarterly else _filing_known_at(filings[0])
        latest_effective = _parse_date((quarterly[0] if quarterly else filings[0]).get("end_date"))
        quarterly_quality, quarterly_notes = _filing_quality(quarterly[:5])
        annual_quality, annual_notes = _filing_quality(annual[:5])
        snapshot.provenance["quarterly_net_income"] = FundamentalFieldProvenance(
            value=snapshot.quarterly_net_income,
            source=self.name,
            as_of=as_of,
            effective_date=latest_effective,
            known_at=latest_known,
            quality=(
                quarterly_quality
                if snapshot.quarterly_net_income
                else QUALITY_UNAVAILABLE
            ),
            staleness_days=(as_of - latest_known).days if latest_known else None,
            is_stale=((as_of - latest_known).days > 240) if latest_known else True,
            restatement_status=_restatement_status(quarterly[:5], filings),
            notes=quarterly_notes,
        )
        snapshot.provenance["annual_net_income"] = FundamentalFieldProvenance(
            value=snapshot.annual_net_income,
            source=self.name,
            as_of=as_of,
            effective_date=_parse_date(annual[0].get("end_date")) if annual else None,
            known_at=_filing_known_at(annual[0]) if annual else None,
            quality=annual_quality if snapshot.annual_net_income else QUALITY_UNAVAILABLE,
            staleness_days=(
                (as_of - _filing_known_at(annual[0])).days
                if annual and _filing_known_at(annual[0])
                else None
            ),
            is_stale=(
                (as_of - _filing_known_at(annual[0])).days > 540
                if annual and _filing_known_at(annual[0])
                else True
            ),
            restatement_status=_restatement_status(annual[:5], filings),
            notes=annual_notes,
        )

        ttm_eps, ttm_notes = _ttm_eps(quarterly, as_of)
        close = _last_close_before(bars, as_of)
        pe = close / ttm_eps if close is not None and ttm_eps and ttm_eps > 0 else None
        pe_quality = quarterly_quality if pe is not None else QUALITY_UNAVAILABLE
        snapshot.pe_ratio = pe
        snapshot.provenance["pe_ratio"] = FundamentalFieldProvenance(
            value=pe,
            source=f"{self.name}+historical_stock_bars",
            as_of=as_of,
            effective_date=latest_effective,
            known_at=latest_known,
            quality=pe_quality,
            staleness_days=(as_of - latest_known).days if latest_known else None,
            is_stale=((as_of - latest_known).days > 240) if latest_known else True,
            fallback_used=False,
            restatement_status=_restatement_status(quarterly[:4], filings),
            notes=[*quarterly_notes, *ttm_notes],
        )

        shares = _shares_value(quarterly[0]) if quarterly else None
        if shares and close:
            snapshot.market_cap = close * shares
            snapshot.provenance["market_cap"] = FundamentalFieldProvenance(
                value=snapshot.market_cap,
                source=f"{self.name}+historical_stock_bars",
                as_of=as_of,
                effective_date=latest_effective,
                known_at=latest_known,
                quality=QUALITY_APPROXIMATE,
                staleness_days=(as_of - latest_known).days if latest_known else None,
                fallback_used=True,
                notes=["derived_from_diluted_average_shares"],
            )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "cached_tickers": len(self.rows_by_ticker),
            "errors": self.errors,
            "source": "/vX/reference/financials",
        }

    def _load_rows(self, ticker: str) -> list[dict[str, Any]]:
        assert self.client is not None
        cache_path = self._cache_path(ticker)
        cached = read_json_if_exists(cache_path) if cache_path else None
        if isinstance(cached, dict) and isinstance(cached.get("results"), list):
            return list(cached["results"])
        payload = self.client.paged_get(
            "/vX/reference/financials",
            {
                "ticker": ticker,
                "limit": 100,
            },
        )
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if cache_path:
            write_json_atomic(cache_path, {"results": rows})
        return list(rows)

    def _cache_path(self, ticker: str) -> Path | None:
        return self.cache_dir / f"{ticker.upper()}.json" if self.cache_dir else None


class MassiveDividendProvider:
    name = "massive_dividends"

    def __init__(
        self,
        *,
        client: "MassiveRestClient | None" = None,
        cache_dir: Path | None = None,
        seed_rows: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir
        self.rows_by_ticker = {
            ticker.upper(): list(rows) for ticker, rows in (seed_rows or {}).items()
        }
        self.errors: dict[str, str] = {}

    def preload(self, tickers: Iterable[str], start: date, end: date) -> None:
        if self.client is None:
            return
        for ticker in tickers:
            if ticker in self.rows_by_ticker or ticker in self.errors:
                continue
            try:
                self.rows_by_ticker[ticker] = self._load_rows(ticker)
            except RestClientError as exc:
                self.errors[ticker] = str(exc)

    def apply(
        self,
        snapshot: FundamentalSnapshot,
        *,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> None:
        rows = self.rows_by_ticker.get(ticker, [])
        known = [
            row
            for row in rows
            if (declared := _parse_date(row.get("declaration_date"))) is not None
            and declared <= as_of
        ]
        upcoming = sorted(
            [
                row
                for row in known
                if (ex_date := _parse_date(row.get("ex_dividend_date"))) is not None
                and ex_date > as_of
            ],
            key=lambda row: _parse_date(row.get("ex_dividend_date")) or date.max,
        )
        if upcoming:
            row = upcoming[0]
            ex_date = _parse_date(row.get("ex_dividend_date"))
            declared = _parse_date(row.get("declaration_date"))
            snapshot.ex_dividend_date = ex_date
            snapshot.provenance["ex_dividend_date"] = FundamentalFieldProvenance(
                value=ex_date,
                source=self.name,
                as_of=as_of,
                effective_date=ex_date,
                known_at=declared,
                quality=QUALITY_STRICT_PIT if declared is not None else QUALITY_APPROXIMATE,
                staleness_days=(as_of - declared).days if declared else None,
            )
        else:
            _set_unavailable(
                snapshot,
                "ex_dividend_date",
                as_of,
                self.name,
                notes=["no_declared_future_dividend_found"],
            )

        trailing = [
            row
            for row in known
            if (ex_date := _parse_date(row.get("ex_dividend_date"))) is not None
            and as_of - timedelta(days=365) <= ex_date <= as_of
        ]
        annual = sum(_cash_amount(row) for row in trailing)
        close = _last_close_before(bars, as_of)
        if annual > 0:
            snapshot.annual_dividend_rate = annual
            snapshot.dividend_yield = annual / close if close and close > 0 else None
            snapshot.provenance["annual_dividend_rate"] = FundamentalFieldProvenance(
                value=annual,
                source=self.name,
                as_of=as_of,
                effective_date=max(
                    (_parse_date(row.get("ex_dividend_date")) for row in trailing),
                    default=None,
                ),
                known_at=max(
                    (_parse_date(row.get("declaration_date")) for row in trailing),
                    default=None,
                ),
                quality=QUALITY_APPROXIMATE,
                notes=["trailing_365_day_declared_dividends"],
            )
            snapshot.provenance["dividend_yield"] = FundamentalFieldProvenance(
                value=snapshot.dividend_yield,
                source=f"{self.name}+historical_stock_bars",
                as_of=as_of,
                effective_date=as_of,
                known_at=as_of,
                quality=QUALITY_APPROXIMATE if snapshot.dividend_yield is not None else QUALITY_UNAVAILABLE,
                fallback_used=True,
            )
        else:
            _set_unavailable(
                snapshot,
                "annual_dividend_rate",
                as_of,
                self.name,
                notes=["no_trailing_declared_dividends"],
            )
            _set_unavailable(
                snapshot,
                "dividend_yield",
                as_of,
                self.name,
                notes=["no_trailing_declared_dividends"],
            )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "cached_tickers": len(self.rows_by_ticker),
            "errors": self.errors,
            "source": "/stocks/v1/dividends",
        }

    def _load_rows(self, ticker: str) -> list[dict[str, Any]]:
        assert self.client is not None
        cache_path = self._cache_path(ticker)
        cached = read_json_if_exists(cache_path) if cache_path else None
        if isinstance(cached, dict) and isinstance(cached.get("results"), list):
            return list(cached["results"])
        payload = self.client.paged_get(
            "/stocks/v1/dividends",
            {
                "ticker": ticker,
                "limit": 1000,
                "order": "asc",
                "sort": "ex_dividend_date",
            },
        )
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if cache_path:
            write_json_atomic(cache_path, {"results": rows})
        return list(rows)

    def _cache_path(self, ticker: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{ticker.upper()}.json"


class MassiveTickerReferenceProvider:
    name = "massive_ticker_reference"

    def __init__(
        self,
        *,
        client: "MassiveRestClient | None" = None,
        cache_dir: Path | None = None,
        seed_details: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir
        self.details_by_ticker = {
            ticker.upper(): dict(details) for ticker, details in (seed_details or {}).items()
        }
        self.errors: dict[str, str] = {}

    def preload(self, tickers: Iterable[str], start: date, end: date) -> None:
        if self.client is None:
            return
        for ticker in tickers:
            if ticker in self.details_by_ticker or ticker in self.errors:
                continue
            try:
                self.details_by_ticker[ticker] = self._load_details(ticker, end)
            except RestClientError as exc:
                self.errors[ticker] = str(exc)

    def apply(
        self,
        snapshot: FundamentalSnapshot,
        *,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> None:
        details = self.details_by_ticker.get(ticker)
        if not details:
            _set_unavailable(snapshot, "quote_type", as_of, self.name)
            return
        quote_type = _quote_type(details)
        snapshot.quote_type = quote_type
        snapshot.long_name = _str_or_none(details.get("name"))
        snapshot.short_name = _str_or_none(details.get("ticker"))
        snapshot.industry = _str_or_none(details.get("sic_description"))
        snapshot.country = "United States" if details.get("locale") == "us" else None
        snapshot.provenance["quote_type"] = FundamentalFieldProvenance(
            value=quote_type,
            source=self.name,
            as_of=as_of,
            known_at=as_of,
            quality=QUALITY_APPROXIMATE,
            notes=["ticker_reference_date_semantics_not_strict_pit"],
        )
        if snapshot.market_cap is None and _positive(details.get("market_cap")) is not None:
            snapshot.market_cap = _positive(details.get("market_cap"))
            snapshot.provenance["market_cap"] = FundamentalFieldProvenance(
                value=snapshot.market_cap,
                source=self.name,
                as_of=as_of,
                known_at=as_of,
                quality=QUALITY_APPROXIMATE,
                fallback_used=True,
                notes=["ticker_details_market_cap_not_strict_pit"],
            )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "cached_tickers": len(self.details_by_ticker),
            "errors": self.errors,
            "source": "/v3/reference/tickers/{ticker}",
        }

    def _load_details(self, ticker: str, as_of: date) -> dict[str, Any]:
        assert self.client is not None
        cache_path = self._cache_path(ticker, as_of)
        cached = read_json_if_exists(cache_path) if cache_path else None
        if isinstance(cached, dict) and isinstance(cached.get("results"), dict):
            return dict(cached["results"])
        payload = self.client.get(
            f"/v3/reference/tickers/{parse.quote(ticker)}",
            {"date": as_of.isoformat()},
        )
        details = payload.get("results", {}) if isinstance(payload, dict) else {}
        if cache_path:
            write_json_atomic(cache_path, {"results": details})
        return dict(details)

    def _cache_path(self, ticker: str, as_of: date) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{ticker.upper()}_{as_of.isoformat()}.json"


class UnusualWhalesEarningsProvider:
    name = "unusual_whales_earnings"

    def __init__(
        self,
        *,
        client: "UnusualWhalesRestClient | None" = None,
        cache_dir: Path | None = None,
        seed_rows: dict[str, list[dict[str, Any]]] | None = None,
        known_days_before: int = 21,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir
        self.rows_by_ticker = {
            ticker.upper(): list(rows) for ticker, rows in (seed_rows or {}).items()
        }
        self.known_days_before = known_days_before
        self.errors: dict[str, str] = {}

    def preload(self, tickers: Iterable[str], start: date, end: date) -> None:
        if self.client is None:
            return
        for ticker in tickers:
            if ticker in self.rows_by_ticker or ticker in self.errors:
                continue
            try:
                self.rows_by_ticker[ticker] = self._load_rows(ticker)
            except RestClientError as exc:
                self.errors[ticker] = str(exc)

    def apply(
        self,
        snapshot: FundamentalSnapshot,
        *,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> None:
        rows = self.rows_by_ticker.get(ticker, [])
        past_events = sorted(
            [
                row
                for row in rows
                if (report_date := _parse_date(row.get("report_date"))) is not None
                and report_date < as_of
            ],
            key=lambda row: _parse_date(row.get("report_date")) or date.min,
            reverse=True,
        )
        if past_events:
            previous_report_date = _parse_date(past_events[0].get("report_date"))
            snapshot.previous_earnings_date = previous_report_date
            snapshot.provenance["previous_earnings_date"] = FundamentalFieldProvenance(
                value=previous_report_date,
                source=self.name,
                as_of=as_of,
                effective_date=previous_report_date,
                known_at=previous_report_date,
                quality=QUALITY_APPROXIMATE,
                fallback_used=True,
                notes=["historical_report_date_from_earnings_calendar"],
            )
        else:
            _set_unavailable(snapshot, "previous_earnings_date", as_of, self.name)

        events = sorted(
            [
                row
                for row in rows
                if (report_date := _parse_date(row.get("report_date"))) is not None
                and report_date >= as_of
            ],
            key=lambda row: _parse_date(row.get("report_date")) or date.max,
        )
        if not events:
            _set_unavailable(snapshot, "next_earnings_date", as_of, self.name)
            return
        report_date = _parse_date(events[0].get("report_date"))
        assert report_date is not None
        assumed_known_at = report_date - timedelta(days=self.known_days_before)
        if as_of >= assumed_known_at:
            snapshot.next_earnings_date = report_date
            quality = QUALITY_APPROXIMATE
            notes = [f"assumed_known_{self.known_days_before}_days_before_report"]
            effective_date = report_date
            value = report_date
        else:
            quality = QUALITY_UNAVAILABLE
            notes = [
                "next_event_exists_but_before_heuristic_known_window"
            ]
            effective_date = None
            value = None
        snapshot.provenance["next_earnings_date"] = FundamentalFieldProvenance(
            value=value,
            source=self.name,
            as_of=as_of,
            effective_date=effective_date,
            known_at=assumed_known_at if as_of >= assumed_known_at else None,
            quality=quality,
            staleness_days=(as_of - assumed_known_at).days if as_of >= assumed_known_at else None,
            fallback_used=True,
            notes=notes,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "cached_tickers": len(self.rows_by_ticker),
            "errors": self.errors,
            "known_days_before": self.known_days_before,
            "source": "/api/earnings/{ticker}",
        }

    def _load_rows(self, ticker: str) -> list[dict[str, Any]]:
        assert self.client is not None
        cache_path = self._cache_path(ticker)
        cached = read_json_if_exists(cache_path) if cache_path else None
        if isinstance(cached, dict) and isinstance(cached.get("data"), list):
            return list(cached["data"])
        payload = self.client.get(f"/api/earnings/{parse.quote(ticker)}")
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if cache_path:
            write_json_atomic(cache_path, {"data": rows})
        return list(rows)

    def _cache_path(self, ticker: str) -> Path | None:
        return self.cache_dir / f"{ticker.upper()}.json" if self.cache_dir else None


class RestClientError(RuntimeError):
    pass


class MassiveRestClient:
    def __init__(self, *, api_key: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        return self.get_url(MASSIVE_BASE_URL + path, params=params)

    def paged_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = self.get(path, params=params)
        merged = _clone_without_next_url(payload)
        while isinstance(payload, dict) and payload.get("next_url"):
            payload = self.get_url(str(payload["next_url"]))
            merged = _merge_results(merged, payload)
        return merged

    def get_url(self, url: str, params: dict[str, Any] | None = None) -> Any:
        query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        if query:
            separator = "&" if parse.urlparse(url).query else "?"
            url += separator + parse.urlencode(query, doseq=True)
        req = request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        return _open_json(req, self.timeout)


class UnusualWhalesRestClient:
    def __init__(self, *, api_key: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url = UNUSUAL_WHALES_BASE_URL + path
        query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        if query:
            url += "?" + parse.urlencode(query, doseq=True)
        req = request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "UW-CLIENT-API-ID": "100001",
            },
        )
        return _open_json(req, self.timeout)


def load_env(explicit_env_file: Path | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for path in default_env_paths():
        env.update(read_env_file(path))
    env.update(os.environ)
    if explicit_env_file:
        env.update(read_env_file(explicit_env_file))
    return env


def default_env_paths() -> list[Path]:
    paths = [Path.cwd() / ".env"]
    explicit = os.environ.get("WHEELS_COPILOT_ENV_FILE")
    if explicit:
        paths.append(Path(explicit).expanduser())
    home = Path.home()
    for candidate in (
        home / "Projects" / "options-copilot" / ".env",
        home / "Projects" / "Options,Copilot" / ".env",
        home / "Projects" / "day-trade-copilot" / ".env",
        home / "Projects" / "day-trade-copilot" / "backend" / ".env",
    ):
        if candidate not in paths:
            paths.append(candidate)
    return paths


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def first_present(values: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value:
            return value
    return None


def recent_move_pct(bars: list[PriceBar], lookback: int = 60) -> float | None:
    if len(bars) < 2:
        return None
    window = bars[-lookback:] if len(bars) >= lookback else bars
    baseline = window[0].close
    if baseline <= 0:
        return None
    peak = max(bar.close for bar in window)
    return (peak - baseline) / baseline * 100.0


def provenance_payload(snapshot: FundamentalSnapshot) -> dict[str, Any]:
    return {
        field: _json_clean(asdict(provenance))
        for field, provenance in sorted(snapshot.provenance.items())
    }


def _open_json(req: request.Request, timeout: float, attempts: int = 3) -> Any:
    raw = ""
    for attempt in range(attempts):
        try:
            with request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                break
        except error.HTTPError as exc:
            body = _safe_error_body(exc)
            if exc.code in {429, 500, 502, 503, 504} and attempt < attempts - 1:
                time.sleep(0.5 * (2**attempt))
                continue
            raise RestClientError(f"HTTP {exc.code} {body}".strip()) from exc
        except error.URLError as exc:
            if attempt < attempts - 1:
                time.sleep(0.5 * (2**attempt))
                continue
            raise RestClientError(str(exc.reason)) from exc
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RestClientError("invalid JSON response") from exc


def _safe_error_body(exc: error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        return ""


def _clone_without_next_url(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if key != "next_url"}


def _merge_results(base: Any, payload: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(payload, dict):
        return base
    merged = dict(base)
    if isinstance(merged.get("results"), list) and isinstance(payload.get("results"), list):
        merged["results"] = [*merged["results"], *payload["results"]]
    return merged


def _latest_distinct_filings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_period: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("timeframe") or ""),
            str(row.get("fiscal_year") or ""),
            str(row.get("fiscal_period") or row.get("end_date") or ""),
        )
        current = by_period.get(key)
        if current is None or (_filing_known_at(row) or date.min) > (
            _filing_known_at(current) or date.min
        ):
            by_period[key] = row
    return sorted(
        by_period.values(),
        key=lambda row: _parse_date(row.get("end_date")) or date.min,
        reverse=True,
    )


def _infer_q4_known_at_from_annual(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annual_by_year: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            str(row.get("timeframe") or "").lower() == "annual"
            and str(row.get("fiscal_period") or "").upper() == "FY"
            and _filing_known_at(row) is not None
        ):
            annual_by_year[str(row.get("fiscal_year") or "")] = row

    inferred: list[dict[str, Any]] = []
    for row in rows:
        if (
            str(row.get("timeframe") or "").lower() == "quarterly"
            and str(row.get("fiscal_period") or "").upper() == "Q4"
            and _filing_known_at(row) is None
        ):
            annual = annual_by_year.get(str(row.get("fiscal_year") or ""))
            if annual is not None:
                row = dict(row)
                row["filing_date"] = row.get("filing_date") or annual.get("filing_date")
                row["acceptance_datetime"] = row.get("acceptance_datetime") or annual.get(
                    "acceptance_datetime"
                )
                row["_q4_known_at_inferred_from_annual"] = True
        inferred.append(row)
    return inferred


def _ttm_eps(rows: list[dict[str, Any]], as_of: date) -> tuple[float | None, list[str]]:
    notes: list[str] = []
    latest_four = rows[:4]
    if len(latest_four) < 4:
        notes.append("ttm_requires_four_quarters")
        return None, notes
    if not _quarters_are_contiguous(latest_four):
        notes.append("ttm_non_contiguous_quarters")
        return None, notes
    oldest_end = _parse_date(latest_four[-1].get("end_date"))
    newest_known = _filing_known_at(latest_four[0])
    if oldest_end is None or newest_known is None:
        notes.append("ttm_missing_dates")
        return None, notes
    if (as_of - oldest_end).days > 470:
        notes.append("ttm_quarters_stale")
        return None, notes
    values = [_eps_value(row) for row in latest_four]
    if any(value is None for value in values):
        notes.append("ttm_missing_eps")
        return None, notes
    return sum(float(value or 0.0) for value in values), notes


def _filing_known_at(row: dict[str, Any]) -> date | None:
    return _parse_date(row.get("acceptance_datetime")) or _parse_date(row.get("filing_date"))


def _filing_quality(rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if not rows:
        return QUALITY_UNAVAILABLE, []
    notes: list[str] = []
    missing_acceptance = [
        row for row in rows if _parse_date(row.get("acceptance_datetime")) is None
    ]
    if missing_acceptance:
        notes.append("acceptance_datetime_missing")
        quality = QUALITY_APPROXIMATE
    else:
        quality = QUALITY_STRICT_PIT_PENDING
    if any(row.get("_q4_known_at_inferred_from_annual") for row in rows):
        notes.append("q4_known_at_inferred_from_annual")
    return quality, notes



def _quarters_are_contiguous(rows: list[dict[str, Any]]) -> bool:
    indexes: list[int] = []
    for row in rows:
        fiscal_year = str(row.get("fiscal_year") or "")
        fiscal_period = str(row.get("fiscal_period") or "").upper()
        if not fiscal_year.isdigit() or not fiscal_period.startswith("Q"):
            return _quarter_end_dates_have_reasonable_span(rows)
        try:
            quarter = int(fiscal_period[1:])
        except ValueError:
            return _quarter_end_dates_have_reasonable_span(rows)
        if quarter < 1 or quarter > 4:
            return _quarter_end_dates_have_reasonable_span(rows)
        indexes.append(int(fiscal_year) * 4 + quarter)
    return indexes == list(range(indexes[0], indexes[0] - len(indexes), -1))


def _quarter_end_dates_have_reasonable_span(rows: list[dict[str, Any]]) -> bool:
    dates = [_parse_date(row.get("end_date")) for row in rows]
    if any(item is None for item in dates):
        return False
    sorted_dates = sorted(item for item in dates if item is not None)
    span = (sorted_dates[-1] - sorted_dates[0]).days
    return 240 <= span <= 370


def _income_value(row: dict[str, Any]) -> float | None:
    statement = row.get("financials", {}).get("income_statement", {})
    for key in (
        "net_income_loss",
        "net_income_loss_available_to_common_stockholders_basic",
        "net_income_loss_attributable_to_parent",
    ):
        value = _statement_value(statement, key)
        if value is not None:
            return value
    return None


def _eps_value(row: dict[str, Any]) -> float | None:
    statement = row.get("financials", {}).get("income_statement", {})
    return _statement_value(statement, "diluted_earnings_per_share") or _statement_value(
        statement, "basic_earnings_per_share"
    )


def _shares_value(row: dict[str, Any]) -> float | None:
    statement = row.get("financials", {}).get("income_statement", {})
    return _positive(
        _statement_value(statement, "diluted_average_shares")
        or _statement_value(statement, "basic_average_shares")
    )


def _statement_value(statement: dict[str, Any], key: str) -> float | None:
    raw = statement.get(key)
    if isinstance(raw, dict):
        return _nullable(raw.get("value"))
    return _nullable(raw)


def _restatement_status(
    selected_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
) -> str:
    selected_keys = {
        (
            str(row.get("timeframe") or ""),
            str(row.get("fiscal_year") or ""),
            str(row.get("fiscal_period") or row.get("end_date") or ""),
        )
        for row in selected_rows
    }
    for key in selected_keys:
        matches = [
            row
            for row in all_rows
            if (
                str(row.get("timeframe") or ""),
                str(row.get("fiscal_year") or ""),
                str(row.get("fiscal_period") or row.get("end_date") or ""),
            )
            == key
        ]
        if len(matches) > 1:
            return "amended_or_restatement_possible"
    return "as_reported_single_record"


def _last_close_before(bars: list[PriceBar], as_of: date) -> float | None:
    previous: PriceBar | None = None
    for bar in sorted(bars, key=lambda item: item.date):
        if bar.date >= as_of:
            break
        previous = bar
    return previous.close if previous else None


def _cash_amount(row: dict[str, Any]) -> float:
    return _positive(row.get("split_adjusted_cash_amount")) or _positive(row.get("cash_amount")) or 0.0


def _quote_type(details: dict[str, Any]) -> str | None:
    value = _str_or_none(details.get("type"))
    if value and value.upper() in {"ETF", "ETN"}:
        return "ETF"
    if value:
        return value.upper()
    return None


def _mark_missing_core_fields(snapshot: FundamentalSnapshot, as_of: date) -> None:
    for field in (
        "quote_type",
        "market_cap",
        "pe_ratio",
        "dividend_yield",
        "annual_dividend_rate",
        "ex_dividend_date",
        "quarterly_net_income",
        "annual_net_income",
        "next_earnings_date",
        "recent_move_pct",
    ):
        if field not in snapshot.provenance:
            _set_unavailable(snapshot, field, as_of, "historical_fundamental_store")


def _set_unavailable(
    snapshot: FundamentalSnapshot,
    field: str,
    as_of: date,
    source: str,
    *,
    notes: list[str] | None = None,
) -> None:
    snapshot.provenance[field] = FundamentalFieldProvenance(
        value=getattr(snapshot, field, None),
        source=source,
        as_of=as_of,
        quality=QUALITY_UNAVAILABLE,
        notes=notes or [],
    )


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _nullable(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive(value: Any) -> float | None:
    parsed = _nullable(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value
