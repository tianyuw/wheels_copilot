from __future__ import annotations

from statistics import fmean

from .models import PivotLow, PriceBar


def sma_values(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(fmean(values[i + 1 - period : i + 1]))
    return out


def sma(values: list[float], period: int) -> float | None:
    series = sma_values(values, period)
    return series[-1] if series else None


def true_ranges(bars: list[PriceBar]) -> list[float]:
    if not bars:
        return []
    out = [bars[0].high - bars[0].low]
    for prev, cur in zip(bars, bars[1:]):
        out.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    return out


def atr(bars: list[PriceBar], period: int = 14) -> float | None:
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    return fmean(trs[-period:])


def rolling_low(bars: list[PriceBar], lookback: int) -> float | None:
    if lookback <= 0 or len(bars) < lookback:
        return None
    return min(b.low for b in bars[-lookback:])


def bollinger_lower(
    values: list[float], window: int = 20, stddev: float = 2.0
) -> float | None:
    if len(values) < window:
        return None
    sample = values[-window:]
    mean = fmean(sample)
    variance = fmean([(x - mean) ** 2 for x in sample])
    return mean - stddev * (variance**0.5)


def find_pivot_lows(
    bars: list[PriceBar], left_bars: int = 4, right_bars: int = 4
) -> list[PivotLow]:
    if left_bars < 1 or right_bars < 1:
        raise ValueError("pivot windows must be positive")
    out: list[PivotLow] = []
    for i in range(left_bars, len(bars) - right_bars):
        low = bars[i].low
        left = [b.low for b in bars[i - left_bars : i]]
        right = [b.low for b in bars[i + 1 : i + 1 + right_bars]]
        if low < min(left) and low <= min(right):
            out.append(PivotLow(index=i, date=bars[i].date, price=low))
    return out
