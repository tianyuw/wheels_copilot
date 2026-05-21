from __future__ import annotations

from datetime import date, datetime
import logging

import pandas as pd
import yfinance as yf

from .models import OptionQuote, PriceBar

logger = logging.getLogger(__name__)


def fetch_daily_bars(ticker: str, period: str = "1y") -> list[PriceBar]:
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if df.empty:
        return []
    return _bars_from_frame(df)


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
                implied_volatility=_nullable_num(row.get("impliedVolatility")),
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
        parsed = float(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _nullable_int(value) -> int | None:
    try:
        if pd.isna(value):
            return None
        return int(value)
    except Exception:
        return None
