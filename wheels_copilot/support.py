from __future__ import annotations

from collections.abc import Mapping
from statistics import fmean
from typing import Any

from .indicators import (
    atr,
    bollinger_lower,
    find_pivot_lows,
    rolling_low,
    sma_values,
)
from .models import PivotLow, PriceBar, SupportAnalysis, SupportZone, TrendCheck


def analyze_support(
    bars: list[PriceBar], config: Mapping[str, Any]
) -> SupportAnalysis:
    if len(bars) < 30:
        raise ValueError("support analysis needs at least 30 daily bars")

    support_cfg = config.get("support", {})
    trend = _check_trend(bars, config.get("trend_filter", {}))
    atr14 = atr(bars, 14)
    current_price = bars[-1].close
    min_score = float(support_cfg.get("scoring", {}).get("min_score_to_trade", 70))

    zones: list[SupportZone] = []
    zones.extend(_pivot_cluster_zones(bars, support_cfg, atr14))
    zones.extend(_range_box_zones(bars, support_cfg, atr14))
    zones.extend(_moving_average_zones(bars, support_cfg, atr14))
    zones.extend(_lowest_low_zones(bars, support_cfg, atr14))
    zones.extend(_bollinger_zones(bars, support_cfg, atr14))

    zones = [z for z in zones if z.bottom < current_price]
    _score_zones(zones, bars, support_cfg, atr14)
    zones.sort(key=lambda z: z.score, reverse=True)
    selected = zones[0] if zones else None

    reasons: list[str] = []
    if not trend.passed:
        reasons.extend(trend.reasons)
    if selected is None:
        reasons.append("no support zone below current price")
    elif selected.score < min_score:
        reasons.append(
            f"best support score {selected.score:.1f} below threshold {min_score:.1f}"
        )

    return SupportAnalysis(
        trend=trend,
        zones=zones,
        selected_zone=selected,
        atr14=atr14,
        current_price=current_price,
        min_score_to_trade=min_score,
        reasons=reasons,
    )


def _check_trend(bars: list[PriceBar], cfg: Mapping[str, Any]) -> TrendCheck:
    closes = [b.close for b in bars]
    current_price = closes[-1]
    sma200_series = sma_values(closes, 200)
    sma200 = sma200_series[-1]
    slope_lookback = int(cfg.get("sma200_slope_lookback_days", 20))
    old_sma = (
        sma200_series[-1 - slope_lookback]
        if len(sma200_series) > slope_lookback
        else None
    )
    slope = None if sma200 is None or old_sma is None else sma200 - old_sma

    passed = True
    reasons: list[str] = []
    if cfg.get("require_price_above_sma200", True):
        if sma200 is None:
            passed = False
            reasons.append("not enough bars for SMA200")
        elif current_price <= sma200:
            passed = False
            reasons.append(
                f"price {current_price:.2f} is not above SMA200 {sma200:.2f}"
            )
    if cfg.get("require_sma200_slope_non_negative", True):
        if slope is None:
            passed = False
            reasons.append("not enough bars for SMA200 slope")
        elif slope < 0:
            passed = False
            reasons.append(f"SMA200 slope is negative: {slope:.2f}")

    return TrendCheck(
        passed=passed,
        current_price=current_price,
        sma200=sma200,
        sma200_slope=slope,
        reasons=reasons,
    )


def _pivot_cluster_zones(
    bars: list[PriceBar], cfg: Mapping[str, Any], atr14: float | None
) -> list[SupportZone]:
    pcfg = cfg.get("pivot_cluster", {})
    if not pcfg.get("enabled", True):
        return []

    lookback = int(pcfg.get("lookback_days", 180))
    recent_bars = bars[-lookback:] if len(bars) > lookback else bars
    offset = len(bars) - len(recent_bars)
    pivots = find_pivot_lows(
        recent_bars,
        int(pcfg.get("pivot_left_bars", 4)),
        int(pcfg.get("pivot_right_bars", 4)),
    )
    pivots = [
        type(p)(index=p.index + offset, date=p.date, price=p.price)
        for p in pivots
    ]
    if not pivots:
        return []

    width = _zone_width(
        bars[-1].close,
        atr14,
        float(pcfg.get("zone_width_atr_multiple", 0.5)),
        float(pcfg.get("zone_width_price_pct", 1.0)),
    )
    min_touches = int(pcfg.get("min_touches_required", 2))
    reject_days = int(pcfg.get("reject_if_broken_within_days", 10))

    clusters = _cluster_pivots_by_price(pivots, width)

    zones: list[SupportZone] = []
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
        center = fmean([p.price for p in cluster])
        bottom = center - width
        top = center + width
        rejections = _count_rejections(bars, bottom, top)
        broken_recently = any(b.close < bottom for b in bars[-reject_days:])
        zones.append(
            SupportZone(
                method="pivot_cluster",
                center=center,
                bottom=bottom,
                top=top,
                touches=len(cluster),
                rejections=rejections,
                last_touch_date=max(p.date for p in cluster),
                broken_recently=broken_recently,
                reasons=[f"{len(cluster)} confirmed pivot lows"],
            )
        )
    return zones


def _range_box_zones(
    bars: list[PriceBar], cfg: Mapping[str, Any], atr14: float | None
) -> list[SupportZone]:
    rcfg = cfg.get("range_box", {})
    if not rcfg.get("enabled", True):
        return []
    lookback = int(rcfg.get("lookback_days", 20))
    min_bars = int(rcfg.get("min_box_bars", 10))
    if len(bars) < max(lookback + 1, min_bars + 1):
        return []
    window = bars[-lookback - 1 : -1]
    current = bars[-1]
    values = [b.close for b in window] if rcfg.get("use_close_for_floor_ceiling", True) else [b.low for b in window]
    floor = min(values)
    ceiling = max(values)
    height = ceiling - floor
    max_height = (atr14 or 0.0) * float(rcfg.get("max_box_height_atr_multiple", 4.0))
    if max_height > 0 and height > max_height:
        return []
    if rcfg.get("require_price_inside_box", True) and not (floor <= current.close <= ceiling):
        return []
    width = _zone_width(floor, atr14, 0.35, 0.75)
    broken = rcfg.get("reject_if_close_below_floor", True) and current.close < floor
    touches = sum(1 for b in window if abs(b.close - floor) <= width)
    return [
        SupportZone(
            method="range_box_floor",
            center=floor,
            bottom=floor - width,
            top=floor + width,
            touches=touches,
            rejections=_count_rejections(bars, floor - width, floor + width),
            last_touch_date=_last_touch_date(bars, floor - width, floor + width),
            broken_recently=broken,
            reasons=[f"{lookback}d consolidation floor"],
        )
    ]


def _moving_average_zones(
    bars: list[PriceBar], cfg: Mapping[str, Any], atr14: float | None
) -> list[SupportZone]:
    macfg = cfg.get("moving_average_supports", {})
    if not macfg.get("enabled", True):
        return []
    closes = [b.close for b in bars]
    width_mult = float(macfg.get("zone_width_atr_multiple", 0.35))
    zones: list[SupportZone] = []
    for period in macfg.get("periods", [50, 200]):
        period = int(period)
        series = sma_values(closes, period)
        value = series[-1] if series else None
        if value is None or value >= bars[-1].close:
            continue
        width = _zone_width(value, atr14, width_mult, 0.5)
        zones.append(
            SupportZone(
                method=f"sma{period}",
                center=value,
                bottom=value - width,
                top=value + width,
            touches=_count_touches(bars, value - width, value + width),
            rejections=_count_rejections(bars, value - width, value + width),
            last_touch_date=_last_touch_date(bars, value - width, value + width),
                broken_recently=False,
                reasons=[f"SMA{period} support"],
            )
        )
    return zones


def _lowest_low_zones(
    bars: list[PriceBar], cfg: Mapping[str, Any], atr14: float | None
) -> list[SupportZone]:
    lcfg = cfg.get("lowest_low_reference", {})
    if not lcfg.get("enabled", True):
        return []
    zones: list[SupportZone] = []
    for lookback in lcfg.get("lookbacks", [20, 50, 100]):
        lookback = int(lookback)
        value = rolling_low(bars, lookback)
        if value is None or value >= bars[-1].close:
            continue
        width = _zone_width(value, atr14, 0.25, 0.5)
        zones.append(
            SupportZone(
                method=f"lowest_low_{lookback}d",
                center=value,
                bottom=value - width,
                top=value + width,
                touches=_count_touches(bars[-lookback:], value - width, value + width),
                rejections=_count_rejections(bars[-lookback:], value - width, value + width),
                last_touch_date=_last_touch_date(bars[-lookback:], value - width, value + width),
                broken_recently=False,
                reasons=[f"{lookback}d lowest-low reference"],
            )
        )
    return zones


def _bollinger_zones(
    bars: list[PriceBar], cfg: Mapping[str, Any], atr14: float | None
) -> list[SupportZone]:
    bcfg = cfg.get("bollinger_lower_band", {})
    if not bcfg.get("enabled", True):
        return []
    window = int(bcfg.get("bollinger_window_days", 20))
    stddev = float(bcfg.get("bollinger_stddev", 2))
    value = bollinger_lower([b.close for b in bars], window, stddev)
    if value is None or value >= bars[-1].close:
        return []
    width = _zone_width(value, atr14, 0.25, 0.5)
    return [
        SupportZone(
            method="bollinger_lower_band",
            center=value,
            bottom=value - width,
            top=value + width,
            touches=_count_touches(bars[-window:], value - width, value + width),
            rejections=_count_rejections(bars[-window:], value - width, value + width),
            last_touch_date=_last_touch_date(bars[-window:], value - width, value + width),
            reasons=["20d Bollinger lower band"],
        )
    ]


def _score_zones(
    zones: list[SupportZone],
    bars: list[PriceBar],
    cfg: Mapping[str, Any],
    atr14: float | None,
) -> None:
    scoring = cfg.get("scoring", {})
    current = bars[-1].close
    tolerance = _zone_width(current, atr14, 0.5, 1.0)
    min_rejections = int(cfg.get("rejection_count", {}).get("min_rejections_required", 2))

    recent_new_low = _recent_new_low_value(bars, bars_count=5, lookback=50)

    for zone in zones:
        score = 0.0
        if zone.method == "pivot_cluster":
            score += float(scoring.get("pivot_cluster_weight", 35))
        if _has_confluence(zone, zones, "range_box_floor", tolerance):
            score += float(scoring.get("range_floor_weight", 20))
            zone.reasons.append("range-floor confluence")
        if any(
            _zones_near(zone, other, tolerance)
            for other in zones
            if other is not zone and other.method in {"sma50", "sma200"}
        ):
            score += float(scoring.get("ma_confluence_weight", 15))
            zone.reasons.append("moving-average confluence")
        if zone.rejections >= min_rejections:
            score += float(scoring.get("rejection_count_weight", 15))
        if any(
            _zones_near(zone, other, tolerance)
            for other in zones
            if other is not zone and other.method.startswith("lowest_low_")
        ):
            score += float(scoring.get("lowest_low_reference_weight", 5))
            zone.reasons.append("lowest-low reference confluence")
        if zone.last_touch_date is not None:
            days_since_touch = (bars[-1].date - zone.last_touch_date).days
            if days_since_touch <= 60:
                score += float(scoring.get("recency_weight", 10))
        if zone.broken_recently:
            score += float(scoring.get("penalty_recent_break", -50))
            zone.reasons.append("recent close below zone")
        if recent_new_low is not None and zone.bottom > recent_new_low:
            score += float(scoring.get("penalty_new_low_within_days", -30))
            zone.reasons.append("recent new low")
        zone.score = max(0.0, score)


def _cluster_pivots_by_price(pivots: list[PivotLow], width: float) -> list[list[PivotLow]]:
    clusters: list[list[PivotLow]] = []
    current: list[PivotLow] = []
    for pivot in sorted(pivots, key=lambda p: p.price):
        if not current:
            current = [pivot]
            continue
        center = fmean([p.price for p in current])
        if abs(pivot.price - center) <= width:
            current.append(pivot)
        else:
            clusters.append(current)
            current = [pivot]
    if current:
        clusters.append(current)
    return clusters


def _zone_width(
    price: float,
    atr14: float | None,
    atr_multiple: float,
    price_pct: float,
) -> float:
    atr_width = (atr14 or 0.0) * atr_multiple
    pct_width = price * price_pct / 100.0
    return max(atr_width, pct_width, 0.01)


def _count_touches(bars: list[PriceBar], bottom: float, top: float) -> int:
    return sum(1 for b in bars if bottom <= b.low <= top)


def _count_rejections(bars: list[PriceBar], bottom: float, top: float) -> int:
    return sum(1 for b in bars if bottom <= b.low <= top and b.close > top)


def _last_touch_date(bars: list[PriceBar], bottom: float, top: float):
    for bar in reversed(bars):
        if bottom <= bar.low <= top:
            return bar.date
    return None


def _zones_near(a: SupportZone, b: SupportZone, tolerance: float) -> bool:
    return abs(a.center - b.center) <= tolerance or not (
        a.top < b.bottom - tolerance or b.top < a.bottom - tolerance
    )


def _has_confluence(
    zone: SupportZone, zones: list[SupportZone], method: str, tolerance: float
) -> bool:
    return any(
        other is not zone and other.method == method and _zones_near(zone, other, tolerance)
        for other in zones
    )


def _recent_new_low_value(
    bars: list[PriceBar], bars_count: int = 5, lookback: int = 50
) -> float | None:
    if len(bars) < max(bars_count, lookback):
        return None
    recent_low = min(b.low for b in bars[-bars_count:])
    prior_low = min(b.low for b in bars[-lookback:-bars_count])
    return recent_low if recent_low < prior_low else None
