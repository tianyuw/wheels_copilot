from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, Sequence

from .historical_data import read_json_if_exists, write_json_atomic
from .historical_fundamentals import (
    MassiveRestClient,
    first_present,
    load_env,
    read_env_file,
)


DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR = Path(
    "/Volumes/Data/wheels_copilot/price_space_break_cache"
)
PRICE_SPACE_BREAK_CLASSIFIER_MODES = {"off", "massive_splits"}
PRICE_SPACE_BREAK_ALLOW_REAL_GAP = "allow_real_gap"
PRICE_SPACE_BREAK_BLOCK = "block"
PRICE_SPACE_BREAK_RESET_LOOKBACK = "reset_lookback"


@dataclass(frozen=True)
class SplitEvent:
    ticker: str
    execution_date: date
    split_from: float
    split_to: float
    adjustment_type: str | None = None
    event_id: str | None = None
    source: str = "massive_splits"
    raw: dict[str, Any] | None = None

    @property
    def price_ratio(self) -> float | None:
        if self.split_to <= 0:
            return None
        return self.split_from / self.split_to

    @property
    def category(self) -> str:
        if self.split_to < self.split_from:
            return "confirmed_reverse_split"
        return "confirmed_split"


@dataclass(frozen=True)
class PriceSpaceBreakClassification:
    ticker: str
    date: date
    category: str
    confidence: str
    action: str
    observed_ratio: float | None
    ratio_basis: str
    reason: str
    expected_ratio: float | None = None
    split_event: SplitEvent | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["date"] = self.date.isoformat()
        if self.split_event is not None:
            payload["split_event"] = split_event_payload(self.split_event)
        return payload


class SplitEventProvider(Protocol):
    def split_events(
        self,
        tickers: Sequence[str],
        start: date,
        end: date,
    ) -> list[SplitEvent]:
        ...


class StaticSplitEventProvider:
    def __init__(self, events: Sequence[SplitEvent]) -> None:
        self.events = list(events)

    def split_events(
        self,
        tickers: Sequence[str],
        start: date,
        end: date,
    ) -> list[SplitEvent]:
        wanted = {ticker.upper() for ticker in tickers}
        return [
            event
            for event in self.events
            if event.ticker.upper() in wanted and start <= event.execution_date <= end
        ]


class MassiveSplitEventProvider:
    def __init__(
        self,
        *,
        api_key: str,
        cache_dir: Path = DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR,
        timeout_seconds: float = 30.0,
        chunk_size: int = 200,
    ) -> None:
        if not api_key:
            raise ValueError("missing Massive API key for price-space break classifier")
        self.client = MassiveRestClient(api_key=api_key, timeout=timeout_seconds)
        self.cache_dir = Path(cache_dir)
        self.chunk_size = max(1, chunk_size)

    def split_events(
        self,
        tickers: Sequence[str],
        start: date,
        end: date,
    ) -> list[SplitEvent]:
        normalized = sorted(
            {str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()}
        )
        if not normalized:
            return []

        events_by_ticker: dict[str, list[SplitEvent]] = {}
        missing: list[str] = []
        for ticker in normalized:
            cached = self._read_cached_ticker(ticker, start, end)
            if cached is None:
                missing.append(ticker)
            else:
                events_by_ticker[ticker] = cached

        for chunk in _chunks(missing, self.chunk_size):
            fetched = self._fetch_chunk(chunk, start, end)
            grouped: dict[str, list[SplitEvent]] = defaultdict(list)
            for event in fetched:
                grouped[event.ticker.upper()].append(event)
            for ticker in chunk:
                rows = sorted(grouped.get(ticker, []), key=lambda item: item.execution_date)
                events_by_ticker[ticker] = rows
                self._write_cached_ticker(ticker, start, end, rows)

        events: list[SplitEvent] = []
        for ticker in normalized:
            events.extend(events_by_ticker.get(ticker, []))
        return events

    def _fetch_chunk(
        self,
        tickers: Sequence[str],
        start: date,
        end: date,
    ) -> list[SplitEvent]:
        payload = self.client.paged_get(
            "/stocks/v1/splits",
            params={
                "ticker.any_of": ",".join(tickers),
                "execution_date.gte": start.isoformat(),
                "execution_date.lte": end.isoformat(),
                "limit": 5000,
                "sort": "execution_date.asc",
            },
        )
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        return [event for row in rows if (event := split_event_from_row(row)) is not None]

    def _cache_path(self, ticker: str, start: date, end: date) -> Path:
        cache_key = f"{ticker}:{start.isoformat()}:{end.isoformat()}"
        key = hashlib.sha256(cache_key.encode()).hexdigest()[:16]
        return self.cache_dir / "massive_splits_v1" / ticker / f"{key}.json"

    def _read_cached_ticker(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[SplitEvent] | None:
        payload = read_json_if_exists(self._cache_path(ticker, start, end))
        if not isinstance(payload, dict):
            return None
        rows = payload.get("events")
        if not isinstance(rows, list):
            return None
        events: list[SplitEvent] = []
        for row in rows:
            event = split_event_from_row(row)
            if event is not None:
                events.append(event)
        return events

    def _write_cached_ticker(
        self,
        ticker: str,
        start: date,
        end: date,
        events: list[SplitEvent],
    ) -> None:
        write_json_atomic(
            self._cache_path(ticker, start, end),
            {
                "schema_version": "massive_splits_cache.v1",
                "ticker": ticker,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "events": [split_event_payload(event) for event in events],
            },
        )


class PriceSpaceBreakClassifier:
    def __init__(
        self,
        *,
        split_provider: SplitEventProvider,
        split_ratio_tolerance_pct: float = 0.04,
        real_gap_ratio_low: float = 0.50,
        real_gap_ratio_high: float = 2.00,
    ) -> None:
        self.split_provider = split_provider
        self.split_ratio_tolerance_pct = max(0.0, split_ratio_tolerance_pct)
        self.real_gap_ratio_low = real_gap_ratio_low
        self.real_gap_ratio_high = real_gap_ratio_high
        self._splits_by_ticker_date: dict[tuple[str, date], list[SplitEvent]] = defaultdict(list)
        self._preloaded = False
        self._diagnostics: Counter[str] = Counter()

    def preload(self, tickers: Sequence[str], start: date, end: date) -> None:
        self._splits_by_ticker_date.clear()
        events = self.split_provider.split_events(tickers, start, end)
        for event in events:
            self._splits_by_ticker_date[(event.ticker.upper(), event.execution_date)].append(event)
        self._preloaded = True
        self._diagnostics["split_events_loaded"] = len(events)

    def classify(
        self,
        *,
        ticker: str,
        issue: dict[str, Any],
    ) -> PriceSpaceBreakClassification:
        break_date = parse_issue_date(issue)
        ratio = parse_float(issue.get("ratio"))
        ratio_basis = str(issue.get("ratio_basis") or "close_to_previous_close")
        normalized_ticker = str(ticker).strip().upper()
        if break_date is None:
            self._diagnostics["unknown_price_break"] += 1
            return PriceSpaceBreakClassification(
                ticker=normalized_ticker,
                date=date.min,
                category="unknown_price_break",
                confidence="low",
                action=PRICE_SPACE_BREAK_BLOCK,
                observed_ratio=ratio,
                ratio_basis=ratio_basis,
                reason="missing_break_date",
            )

        split = self._matching_split(normalized_ticker, break_date, ratio)
        if split is not None:
            category = split.category
            self._diagnostics[category] += 1
            return PriceSpaceBreakClassification(
                ticker=normalized_ticker,
                date=break_date,
                category=category,
                confidence="high",
                action=PRICE_SPACE_BREAK_RESET_LOOKBACK,
                observed_ratio=ratio,
                ratio_basis=ratio_basis,
                expected_ratio=split.price_ratio,
                split_event=split,
                reason="matched_massive_split_execution_date_and_ratio_reset_lookback",
            )

        same_day_splits = self._splits_by_ticker_date.get((normalized_ticker, break_date), [])
        if same_day_splits:
            self._diagnostics["unknown_price_break"] += 1
            return PriceSpaceBreakClassification(
                ticker=normalized_ticker,
                date=break_date,
                category="unknown_price_break",
                confidence="medium",
                action=PRICE_SPACE_BREAK_BLOCK,
                observed_ratio=ratio,
                ratio_basis=ratio_basis,
                expected_ratio=same_day_splits[0].price_ratio,
                split_event=same_day_splits[0],
                reason="split_event_found_but_observed_ratio_mismatch",
            )

        if ratio is not None and self._looks_like_real_gap(ratio):
            self._diagnostics["real_gap_move"] += 1
            return PriceSpaceBreakClassification(
                ticker=normalized_ticker,
                date=break_date,
                category="real_gap_move",
                confidence="medium",
                action=PRICE_SPACE_BREAK_ALLOW_REAL_GAP,
                observed_ratio=ratio,
                ratio_basis=ratio_basis,
                reason="no_matching_split_and_ratio_within_real_gap_bounds",
            )

        self._diagnostics["unknown_price_break"] += 1
        return PriceSpaceBreakClassification(
            ticker=normalized_ticker,
            date=break_date,
            category="unknown_price_break",
            confidence="low",
            action=PRICE_SPACE_BREAK_BLOCK,
            observed_ratio=ratio,
            ratio_basis=ratio_basis,
            reason="no_matching_split_and_ratio_not_safe_to_unblock",
        )

    def diagnostics(self) -> dict[str, Any]:
        return dict(sorted(self._diagnostics.items()))

    def _matching_split(
        self,
        ticker: str,
        break_date: date,
        observed_ratio: float | None,
    ) -> SplitEvent | None:
        if observed_ratio is None:
            return None
        for event in self._splits_by_ticker_date.get((ticker, break_date), []):
            expected = event.price_ratio
            if expected is not None and ratio_close(
                observed_ratio,
                expected,
                tolerance_pct=self.split_ratio_tolerance_pct,
            ):
                return event
        return None

    def _looks_like_real_gap(self, ratio: float) -> bool:
        if not self.real_gap_ratio_low <= ratio <= self.real_gap_ratio_high:
            return False
        return not looks_like_common_split_ratio(
            ratio,
            tolerance_pct=self.split_ratio_tolerance_pct,
        )


def build_price_space_break_classifier(
    *,
    mode: str,
    env_file: Path | None = None,
    cache_dir: Path = DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR,
    timeout_seconds: float = 30.0,
    split_provider: SplitEventProvider | None = None,
) -> PriceSpaceBreakClassifier | None:
    normalized = normalize_classifier_mode(mode)
    if normalized == "off":
        return None
    if split_provider is None:
        env = load_massive_env(env_file)
        api_key = first_present(env, "MASIVE_API_KEY", "MASSIVE_API_KEY", "POLYGON_API_KEY")
        if not api_key:
            raise ValueError(
                "missing Massive API key; set MASIVE_API_KEY, MASSIVE_API_KEY, or POLYGON_API_KEY"
            )
        split_provider = MassiveSplitEventProvider(
            api_key=api_key,
            cache_dir=cache_dir,
            timeout_seconds=timeout_seconds,
        )
    return PriceSpaceBreakClassifier(split_provider=split_provider)


def load_massive_env(explicit_env_file: Path | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for path in (
        Path("/Users/tianyuwang/Projects/day-trade-copilot/backend/.env"),
        Path("/Users/tianyuwang/Projects/options-copilot/.env"),
        Path("/Users/tianyuwang/Projects/kinfo_trader/.env"),
    ):
        env.update(read_env_file(path))
    env.update(load_env(explicit_env_file))
    return env


def normalize_classifier_mode(mode: str | None) -> str:
    normalized = str(mode or "off").strip().lower()
    aliases = {
        "none": "off",
        "disabled": "off",
        "massive": "massive_splits",
        "splits": "massive_splits",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in PRICE_SPACE_BREAK_CLASSIFIER_MODES:
        raise ValueError(f"unsupported price-space break classifier mode: {mode}")
    return normalized


def classify_price_space_issues(
    *,
    issues: Sequence[dict[str, Any]],
    classifier: PriceSpaceBreakClassifier,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("type") != "price_space_break":
            continue
        ticker = str(issue.get("ticker") or "").strip().upper()
        details = issue.get("details") if isinstance(issue.get("details"), dict) else issue
        if not ticker:
            continue
        classification = classifier.classify(ticker=ticker, issue=details)
        row = {
            "date": issue.get("date") or details.get("date"),
            "ticker": ticker,
            "type": "price_space_break",
            "details": details,
            "classification": classification.to_payload(),
        }
        rows.append(row)
    return rows


def parse_issue_date(issue: dict[str, Any]) -> date | None:
    value = issue.get("date")
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ratio_close(value: float, expected: float, *, tolerance_pct: float) -> bool:
    if expected == 0:
        return False
    return abs(value - expected) / abs(expected) <= tolerance_pct


def looks_like_common_split_ratio(ratio: float, *, tolerance_pct: float = 0.04) -> bool:
    common = (
        0.1,
        0.125,
        0.2,
        0.25,
        1 / 3,
        0.4,
        0.5,
        2 / 3,
        1.5,
        2.0,
        2.5,
        3.0,
        4.0,
        5.0,
        8.0,
        10.0,
    )
    return any(ratio_close(ratio, expected, tolerance_pct=tolerance_pct) for expected in common)


def split_event_from_row(row: dict[str, Any]) -> SplitEvent | None:
    ticker = str(row.get("ticker") or "").strip().upper()
    execution_date = parse_date_value(row.get("execution_date"))
    split_from = parse_float(row.get("split_from"))
    split_to = parse_float(row.get("split_to"))
    if not ticker or execution_date is None or split_from is None or split_to is None:
        return None
    if split_from <= 0 or split_to <= 0:
        return None
    return SplitEvent(
        ticker=ticker,
        execution_date=execution_date,
        split_from=split_from,
        split_to=split_to,
        adjustment_type=(
            str(row.get("adjustment_type")).strip()
            if row.get("adjustment_type") not in (None, "")
            else None
        ),
        event_id=str(row.get("id")) if row.get("id") not in (None, "") else None,
        source=str(row.get("source") or "massive_splits"),
        raw=dict(row),
    )


def split_event_payload(event: SplitEvent) -> dict[str, Any]:
    return {
        "ticker": event.ticker,
        "execution_date": event.execution_date.isoformat(),
        "split_from": event.split_from,
        "split_to": event.split_to,
        "adjustment_type": event.adjustment_type,
        "event_id": event.event_id,
        "source": event.source,
        "price_ratio": event.price_ratio,
    }


def parse_date_value(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def issue_date_bounds(issues: Sequence[dict[str, Any]]) -> tuple[date, date] | None:
    dates = [
        parsed
        for issue in issues
        for parsed in [
            parse_issue_date(
                issue.get("details") if isinstance(issue.get("details"), dict) else issue
            )
        ]
        if parsed is not None
    ]
    if not dates:
        return None
    return min(dates), max(dates)


def expanded_date_bounds(start: date, end: date, *, padding_days: int = 0) -> tuple[date, date]:
    return start - timedelta(days=padding_days), end + timedelta(days=padding_days)


def summarize_classifications(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    category_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    for row in rows:
        classification = row.get("classification") if isinstance(row, dict) else None
        if not isinstance(classification, dict):
            continue
        category_counts[str(classification.get("category") or "unknown")] += 1
        action_counts[str(classification.get("action") or "unknown")] += 1
        confidence_counts[str(classification.get("confidence") or "unknown")] += 1
    return {
        "count": len(rows),
        "category_counts": dict(category_counts.most_common()),
        "action_counts": dict(action_counts.most_common()),
        "confidence_counts": dict(confidence_counts.most_common()),
    }


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]
