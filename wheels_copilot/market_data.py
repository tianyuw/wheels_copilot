from __future__ import annotations

from datetime import date, datetime
import logging
import re

import pandas as pd
import yfinance as yf

from .models import FundamentalSnapshot, OptionQuote, PriceBar

logger = logging.getLogger(__name__)


def fetch_daily_bars(ticker: str, period: str = "1y") -> list[PriceBar]:
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if df.empty:
        return []
    return _bars_from_frame(df)


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
) -> list[OptionQuote]:
    as_of = as_of or date.today()
    tk = yf.Ticker(ticker)
    expirations = []
    for raw in tk.options:
        exp = datetime.strptime(raw, "%Y-%m-%d").date()
        dte = (exp - as_of).days
        if dte_min <= dte <= dte_max:
            expirations.append((exp, dte, raw))
    if not expirations:
        return []
    options: list[OptionQuote] = []
    for exp, dte, raw in sorted(expirations, key=lambda item: item[1]):
        try:
            chain = tk.option_chain(raw).puts
        except Exception as exc:
            logger.warning("Failed to fetch %s option chain %s: %s", ticker, raw, exc)
            continue
        options.extend(_puts_from_frame(chain, exp, dte))
    return options


def _bars_from_frame(df: pd.DataFrame) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else datetime.strptime(str(idx)[:10], "%Y-%m-%d").date()
        bars.append(
            PriceBar(
                date=d,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0) or 0),
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


def _puts_from_frame(df: pd.DataFrame, expiration: date, dte: int) -> list[OptionQuote]:
    options: list[OptionQuote] = []
    for _, row in df.iterrows():
        bid = _num(row.get("bid"))
        ask = _num(row.get("ask"))
        last = _num(row.get("lastPrice"))
        strike = _num(row.get("strike"))
        if strike <= 0:
            continue
        options.append(
            OptionQuote(
                symbol=str(row.get("contractSymbol") or ""),
                expiration=expiration,
                dte=dte,
                strike=strike,
                bid=bid,
                ask=ask,
                last=last,
                implied_volatility=_positive_num(row.get("impliedVolatility")),
                open_interest=_nullable_int(row.get("openInterest")),
                volume=_nullable_int(row.get("volume")),
            )
        )
    return options


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


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
