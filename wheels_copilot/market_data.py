from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
import logging
import re
from typing import Any

import pandas as pd
import yfinance as yf

from .alpaca import AlpacaMarketDataClient
from .models import FundamentalSnapshot, OptionQuote, PriceBar

logger = logging.getLogger(__name__)


def fetch_daily_bars(
    ticker: str,
    period: str = "1y",
    config: dict[str, Any] | None = None,
    client: AlpacaMarketDataClient | None = None,
) -> list[PriceBar]:
    client = client or AlpacaMarketDataClient.from_config(config or {})
    start = _period_start(period, date.today())
    payload = client.fetch_stock_bars(
        ticker,
        timeframe="1Day",
        start=_rfc3339_start(start),
        adjustment="raw",
    )
    return _bars_from_alpaca(payload)


def fetch_fundamental_snapshot(
    ticker: str,
    bars: list[PriceBar] | None = None,
    as_of: date | None = None,
) -> FundamentalSnapshot:
    as_of = as_of or date.today()
    tk = yf.Ticker(ticker)
    info = _safe_info(tk)
    market_cap = _positive_num(
        info.get("marketCap")
        or info.get("totalAssets")
        or info.get("enterpriseValue")
    )
    pe_ratio = _nullable_num(info.get("trailingPE") or info.get("forwardPE"))
    dividend_yield = _normalize_dividend_yield(
        info.get("dividendYield")
        or info.get("trailingAnnualDividendYield")
    )
    return FundamentalSnapshot(
        ticker=ticker.upper(),
        quote_type=_str_or_none(info.get("quoteType")),
        short_name=_str_or_none(info.get("shortName")),
        long_name=_str_or_none(info.get("longName")),
        sector=_str_or_none(info.get("sector")),
        industry=_str_or_none(info.get("industry")),
        country=_str_or_none(info.get("country")),
        market_cap=market_cap,
        pe_ratio=pe_ratio,
        dividend_yield=dividend_yield,
        quarterly_net_income=_net_income_values(_safe_statement(tk, "quarterly_income_stmt"), limit=5),
        annual_net_income=_net_income_values(_safe_statement(tk, "income_stmt"), limit=5),
        next_earnings_date=_next_earnings_date(tk, as_of),
        recent_move_pct=_recent_move_pct(bars or []),
    )


def fetch_put_chain(
    ticker: str,
    dte_min: int,
    dte_max: int,
    as_of: date | None = None,
    config: dict[str, Any] | None = None,
    client: AlpacaMarketDataClient | None = None,
) -> list[OptionQuote]:
    as_of = as_of or date.today()
    client = client or AlpacaMarketDataClient.from_config(config or {})
    max_quote_age_seconds = _max_option_quote_age_seconds(config)
    contracts = client.fetch_option_contracts(
        ticker,
        option_type="put",
        expiration_date_gte=as_of + timedelta(days=dte_min),
        expiration_date_lte=as_of + timedelta(days=dte_max),
    )
    contracts = [
        contract
        for contract in contracts
        if _str_or_none(contract.get("symbol"))
        and str(contract.get("type") or "").lower() == "put"
        and contract.get("tradable") is not False
    ]
    if not contracts:
        return []
    options: list[OptionQuote] = []
    snapshot_by_symbol: dict[str, dict[str, Any]] = {}
    for chunk in _chunks([str(c["symbol"]) for c in contracts], 100):
        snapshot_by_symbol.update(client.fetch_option_snapshots(chunk))
    for contract in sorted(contracts, key=_contract_sort_key):
        option = _option_from_alpaca_contract(
            contract,
            snapshot_by_symbol.get(str(contract["symbol"])) or {},
            as_of,
            client.option_feed,
            max_quote_age_seconds,
        )
        if option:
            options.append(option)
    return options


def _bars_from_alpaca(payload: list[dict[str, Any]]) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for row in payload:
        bar_date = _date_from_timestamp(row.get("t"))
        if bar_date is None:
            continue
        bars.append(
            PriceBar(
                date=bar_date,
                open=_num(row.get("o")),
                high=_num(row.get("h")),
                low=_num(row.get("l")),
                close=_num(row.get("c")),
                volume=_num(row.get("v")),
            )
        )
    return bars


def _safe_info(ticker) -> dict:
    try:
        info = ticker.get_info()
    except Exception:
        try:
            info = ticker.info
        except Exception as exc:
            logger.warning("Failed to fetch ticker info: %s", exc)
            return {}
    return info if isinstance(info, dict) else {}


def _safe_statement(ticker, attr: str) -> pd.DataFrame:
    try:
        value = getattr(ticker, attr)
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", attr, exc)
        return pd.DataFrame()
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _net_income_values(statement: pd.DataFrame, limit: int = 5) -> list[float]:
    if statement.empty:
        return []
    row = _find_statement_row(statement, "Net Income")
    if row is None:
        return []
    row = _sort_statement_row_desc(row.dropna())
    values: list[float] = []
    for value in row.tolist()[:limit]:
        parsed = _nullable_num(value)
        if parsed is not None:
            values.append(parsed)
    return values


def _sort_statement_row_desc(row: pd.Series) -> pd.Series:
    return row.reindex(
        sorted(row.index, key=_statement_column_sort_key, reverse=True)
    )


def _statement_column_sort_key(value) -> datetime:
    try:
        parsed = pd.to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return datetime.min
    if pd.isna(parsed):
        return datetime.min
    if isinstance(parsed, pd.Timestamp):
        return parsed.to_pydatetime().replace(tzinfo=None)
    return datetime.min


def _find_statement_row(statement: pd.DataFrame, row_name: str):
    for idx in statement.index:
        if str(idx).strip().lower() == row_name.lower():
            return statement.loc[idx]
    for idx in statement.index:
        if row_name.lower() in str(idx).strip().lower():
            return statement.loc[idx]
    return None


def _next_earnings_date(ticker, as_of: date) -> date | None:
    frames = []
    try:
        frames.append(ticker.get_earnings_dates(limit=12))
    except Exception:
        pass
    try:
        calendar = ticker.calendar
        if isinstance(calendar, pd.DataFrame):
            frames.append(calendar)
        elif isinstance(calendar, dict):
            frames.append(pd.DataFrame([calendar]))
    except Exception:
        pass

    candidates: list[date] = []
    for frame in frames:
        if frame is None or getattr(frame, "empty", True):
            continue
        candidates.extend(_dates_from_frame(frame))
        if isinstance(frame.index, pd.DatetimeIndex):
            candidates.extend(ts.date() for ts in frame.index)
    future = sorted(d for d in candidates if d >= as_of)
    return future[0] if future else None


def _dates_from_frame(frame: pd.DataFrame) -> list[date]:
    out: list[date] = []
    for column in frame.columns:
        if "earn" not in str(column).lower():
            continue
        for value in frame[column].dropna().tolist():
            out.extend(_dates_from_value(value))
    return out


def _dates_from_value(value) -> list[date]:
    if value is None:
        return []
    if isinstance(value, pd.Timestamp):
        return [] if pd.isna(value) else [value.date()]
    if isinstance(value, datetime):
        return [value.date()]
    if isinstance(value, date):
        return [value]
    if isinstance(value, str) and not _looks_like_full_date_text(value):
        return []
    if isinstance(value, pd.Series):
        return _dates_from_iterable(value.dropna().tolist())
    if isinstance(value, pd.Index):
        return _dates_from_iterable(value.dropna().tolist())
    if isinstance(value, (list, tuple, set)):
        return _dates_from_iterable(value)
    if not isinstance(value, (str, bytes)) and hasattr(value, "tolist"):
        return _dates_from_value(value.tolist())

    try:
        parsed = pd.to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return []
    try:
        if pd.isna(parsed):
            return []
    except ValueError:
        pass
    return _dates_from_value(parsed)


def _dates_from_iterable(values) -> list[date]:
    out: list[date] = []
    for value in values:
        out.extend(_dates_from_value(value))
    return out


def _looks_like_full_date_text(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    numeric = r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"
    month_name = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    return bool(
        re.search(numeric, text, re.IGNORECASE)
        or (
            re.search(month_name, text, re.IGNORECASE)
            and re.search(r"\b\d{1,2}\b", text)
            and re.search(r"\b\d{4}\b", text)
        )
    )


def _recent_move_pct(bars: list[PriceBar], lookback: int = 60) -> float | None:
    if len(bars) < 2:
        return None
    window = bars[-lookback:] if len(bars) >= lookback else bars
    baseline = window[0].close
    if baseline <= 0:
        return None
    peak = max(b.close for b in window)
    return (peak - baseline) / baseline * 100.0


def _option_from_alpaca_contract(
    contract: dict[str, Any],
    snapshot: dict[str, Any],
    as_of: date,
    feed: str,
    max_quote_age_seconds: int | None = None,
) -> OptionQuote | None:
    symbol = str(contract.get("symbol") or "").upper()
    expiration = _date_from_iso(contract.get("expiration_date"))
    strike = _positive_num(contract.get("strike_price"))
    if not symbol or expiration is None or strike is None:
        return None
    quote = _snapshot_child(snapshot, "latestQuote")
    trade = _snapshot_child(snapshot, "latestTrade")
    greeks = snapshot.get("greeks") or {}
    daily_bar = _snapshot_child(snapshot, "dailyBar")
    bid = _num(quote.get("bp"))
    ask = _num(quote.get("ap"))
    quote_timestamp = _datetime_from_timestamp(quote.get("t"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    if max_quote_age_seconds is not None:
        if quote_timestamp is None:
            return None
        age_seconds = (datetime.now(timezone.utc) - quote_timestamp).total_seconds()
        if age_seconds > max_quote_age_seconds:
            return None
    return OptionQuote(
        symbol=symbol,
        expiration=expiration,
        dte=(expiration - as_of).days,
        strike=strike,
        bid=bid,
        ask=ask,
        last=_num(trade.get("p")),
        implied_volatility=_positive_num(
            _first_present(
                snapshot.get("impliedVolatility"),
                snapshot.get("implied_volatility"),
            )
        ),
        open_interest=_nullable_int(
            _first_present(contract.get("open_interest"), snapshot.get("openInterest"))
        ),
        volume=_nullable_int(_first_present(daily_bar.get("v"), snapshot.get("volume"))),
        delta=_nullable_num(greeks.get("delta")),
        quote_timestamp=quote_timestamp,
        trade_timestamp=_datetime_from_timestamp(trade.get("t")),
        data_feed=feed,
    )


def _snapshot_child(snapshot: dict[str, Any], camel_name: str) -> dict[str, Any]:
    snake_name = re.sub(r"(?<!^)([A-Z])", r"_\1", camel_name).lower()
    value = snapshot.get(camel_name) or snapshot.get(snake_name) or {}
    return value if isinstance(value, dict) else {}


def _contract_sort_key(contract: dict[str, Any]) -> tuple[date, float, str]:
    expiration = _date_from_iso(contract.get("expiration_date")) or date.max
    return (
        expiration,
        _num(contract.get("strike_price")),
        str(contract.get("symbol") or ""),
    )


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _period_start(period: str, today: date) -> date:
    text = period.strip().lower()
    match = re.fullmatch(r"(\d+)(d|mo|y)", text)
    if not match:
        raise ValueError(f"unsupported period: {period}")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return today - timedelta(days=amount)
    if unit == "mo":
        return _subtract_months(today, amount)
    return _subtract_months(today, amount * 12)


def _subtract_months(day: date, months: int) -> date:
    zero_based_month = day.month - 1 - months
    year = day.year + zero_based_month // 12
    month = zero_based_month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _rfc3339_start(day: date) -> str:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).isoformat()


def _date_from_iso(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_from_timestamp(value) -> date | None:
    parsed = _datetime_from_timestamp(value)
    return parsed.date() if parsed else None


def _datetime_from_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(value) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _nullable_num(value) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _positive_num(value) -> float | None:
    parsed = _nullable_num(value)
    return parsed if parsed is not None and parsed > 0 else None


def _normalize_dividend_yield(value) -> float | None:
    parsed = _positive_num(value)
    if parsed is None:
        return None
    return parsed / 100.0 if parsed > 0.25 else parsed


def _nullable_int(value) -> int | None:
    try:
        if pd.isna(value):
            return None
        return int(value)
    except Exception:
        return None


def _first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _max_option_quote_age_seconds(config: dict[str, Any] | None) -> int | None:
    if not config:
        return None
    market_data_cfg = config.get("market_data") or {}
    value = market_data_cfg.get("max_option_quote_age_seconds")
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
