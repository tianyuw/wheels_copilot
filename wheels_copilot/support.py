from __future__ import annotations

import hashlib
from collections.abc import Mapping
from statistics import fmean
from typing import Any

from .indicators import (
    atr,
    bollinger_bands,
    bollinger_lower,
    find_pivot_lows,
    rolling_low,
    rsi,
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
    context_metrics = _support_context_metrics(bars, support_cfg, atr14)
    preconditions_passed, precondition_reasons, precondition_metrics = (
        _check_preconditions(bars, support_cfg, atr14)
    )

    zones: list[SupportZone] = []
    zones.extend(_pivot_cluster_zones(bars, support_cfg, atr14))
    zones.extend(_range_box_zones(bars, support_cfg, atr14))
    zones.extend(_moving_average_zones(bars, support_cfg, atr14))
    zones.extend(_lowest_low_zones(bars, support_cfg, atr14))
    zones.extend(_bollinger_zones(bars, support_cfg, atr14))

    zones = [z for z in zones if z.bottom < current_price]
    _score_zones(zones, bars, support_cfg, atr14)
    zones.sort(key=lambda z: z.score, reverse=True)
    selected = _select_zone_for_policy(zones, current_price, min_score, support_cfg)

    reasons: list[str] = []
    if not trend.passed:
        reasons.extend(trend.reasons)
    if not preconditions_passed:
        reasons.extend(precondition_reasons)
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
        preconditions_passed=preconditions_passed,
        precondition_metrics=precondition_metrics,
        context_metrics=context_metrics,
        reasons=reasons,
    )


def _select_zone_for_policy(
    zones: list[SupportZone],
    current_price: float,
    min_score: float,
    cfg: Mapping[str, Any],
) -> SupportZone | None:
    if not zones:
        return None
    policy = _normalize_selection_policy(cfg.get("selection_policy"))
    if policy == "nearest_qualified":
        qualified = [zone for zone in zones if zone.score >= min_score]
        if qualified:
            return sorted(
                qualified,
                key=lambda zone: (
                    max(0.0, current_price - zone.top),
                    -zone.score,
                    -zone.top,
                ),
            )[0]
    return zones[0]


def _normalize_selection_policy(value: Any) -> str:
    policy = str(value or "highest_score").strip().lower()
    aliases = {
        "highest": "highest_score",
        "score": "highest_score",
        "nearest": "nearest_qualified",
        "candidate_aware": "candidate_aware_top3",
        "candidate-aware": "candidate_aware_top3",
    }
    return aliases.get(policy, policy)


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


def _support_context_metrics(
    bars: list[PriceBar],
    support_cfg: Mapping[str, Any],
    atr14: float | None,
) -> dict[str, Any]:
    closes = [bar.close for bar in bars]
    current_price = closes[-1]
    scoring = support_cfg.get("scoring", {})
    context_cfg = scoring.get("context_adjustments", {})
    precondition_cfg = support_cfg.get("preconditions", {})
    rsi_period = int(
        context_cfg.get("rsi_period", precondition_cfg.get("rsi_period", 14))
    )
    bollinger_window = int(
        context_cfg.get(
            "bollinger_position_window_days",
            precondition_cfg.get("bollinger_touch_window_days", 20),
        )
    )
    bollinger_stddev = float(
        context_cfg.get(
            "bollinger_position_stddev",
            precondition_cfg.get("bollinger_touch_stddev", 2.0),
        )
    )
    bands = bollinger_bands(closes, bollinger_window, bollinger_stddev)
    bollinger_position = None
    bollinger_lower_value = None
    bollinger_middle_value = None
    bollinger_upper_value = None
    if bands is not None:
        lower, middle, upper = bands
        bollinger_lower_value = lower
        bollinger_middle_value = middle
        bollinger_upper_value = upper
        if upper > lower:
            bollinger_position = (current_price - lower) / (upper - lower)

    sma50 = _sma_last(closes, 50)
    close_vs_sma50_pct = (
        (current_price / sma50 - 1.0) * 100.0 if sma50 and sma50 > 0 else None
    )
    return {
        f"rsi{rsi_period}": rsi(closes, rsi_period),
        "sma50": sma50,
        "close_vs_sma50_pct": close_vs_sma50_pct,
        "return_10d_pct": _lookback_return_pct(closes, 10),
        "return_20d_pct": _lookback_return_pct(closes, 20),
        "bollinger_lower": bollinger_lower_value,
        "bollinger_middle": bollinger_middle_value,
        "bollinger_upper": bollinger_upper_value,
        "bollinger_position": bollinger_position,
        "atr14": atr14,
    }


def _check_preconditions(
    bars: list[PriceBar],
    support_cfg: Mapping[str, Any],
    atr14: float | None,
) -> tuple[bool, list[str], dict[str, Any]]:
    cfg = support_cfg.get("preconditions", {})
    if not cfg.get("enabled", False):
        return True, [], {}

    closes = [bar.close for bar in bars]
    current_price = closes[-1]
    reasons: list[str] = []
    metrics: dict[str, Any] = {}

    rsi_period = int(cfg.get("rsi_period", 14))
    current_rsi = rsi(closes, rsi_period)
    metrics[f"rsi{rsi_period}"] = current_rsi
    max_rsi = float(cfg.get("max_rsi14", 999.0))
    if max_rsi < 100.0:
        if current_rsi is None:
            reasons.append(f"not_enough_bars_for_rsi{rsi_period}")
        elif current_rsi > max_rsi:
            reasons.append(f"rsi{rsi_period}_{current_rsi:.2f}_gt_{max_rsi:.2f}")

    sma50 = _sma_last(closes, 50)
    close_vs_sma50_pct = (
        (current_price / sma50 - 1.0) * 100.0 if sma50 and sma50 > 0 else None
    )
    metrics["sma50"] = sma50
    metrics["close_vs_sma50_pct"] = close_vs_sma50_pct
    max_close_vs_sma50 = float(cfg.get("max_close_vs_sma50_pct", 999.0))
    if max_close_vs_sma50 < 999.0:
        if close_vs_sma50_pct is None:
            reasons.append("not_enough_bars_for_sma50")
        elif close_vs_sma50_pct > max_close_vs_sma50:
            reasons.append(
                f"close_vs_sma50_{close_vs_sma50_pct:.2f}_gt_{max_close_vs_sma50:.2f}"
            )

    for lookback in (10, 20):
        max_return = float(cfg.get(f"max_return_{lookback}d_pct", 999.0))
        return_pct = _lookback_return_pct(closes, lookback)
        metrics[f"return_{lookback}d_pct"] = return_pct
        if max_return < 999.0:
            if return_pct is None:
                reasons.append(f"not_enough_bars_for_return_{lookback}d")
            elif return_pct > max_return:
                reasons.append(
                    f"return_{lookback}d_{return_pct:.2f}_gt_{max_return:.2f}"
                )

    max_bollinger_position = float(cfg.get("max_bollinger_position", 999.0))
    if max_bollinger_position < 999.0:
        window = int(cfg.get("bollinger_position_window_days", 20))
        stddev = float(cfg.get("bollinger_position_stddev", 2.0))
        bands = bollinger_bands(closes, window, stddev)
        metrics["bollinger_position"] = None
        metrics["bollinger_lower"] = None
        metrics["bollinger_middle"] = None
        metrics["bollinger_upper"] = None
        if bands is None:
            reasons.append(f"not_enough_bars_for_bollinger_{window}d")
        else:
            lower, middle, upper = bands
            metrics["bollinger_lower"] = lower
            metrics["bollinger_middle"] = middle
            metrics["bollinger_upper"] = upper
            if upper <= lower:
                reasons.append("invalid_bollinger_band_width")
            else:
                position = (current_price - lower) / (upper - lower)
                metrics["bollinger_position"] = position
                if position > max_bollinger_position:
                    reasons.append(
                        "bollinger_position_"
                        f"{position:.2f}_gt_{max_bollinger_position:.2f}"
                    )

    if cfg.get("require_pullback_from_recent_high", False):
        lookback = int(cfg.get("pullback_lookback_days", 10))
        min_pullback = float(cfg.get("min_pullback_from_high_pct", 0.0))
        recent_high = _recent_high(bars, lookback)
        pullback_pct = (
            (recent_high / current_price - 1.0) * 100.0
            if recent_high and current_price > 0
            else None
        )
        metrics["recent_high"] = recent_high
        metrics["pullback_from_high_pct"] = pullback_pct
        if pullback_pct is None:
            reasons.append(f"not_enough_bars_for_{lookback}d_pullback")
        elif pullback_pct < min_pullback:
            reasons.append(
                f"pullback_{pullback_pct:.2f}_lt_{min_pullback:.2f}"
            )

    if cfg.get("require_recent_bollinger_touch", False):
        touched, touch_metrics = _recent_bollinger_touch(
            bars,
            window=int(cfg.get("bollinger_touch_window_days", 20)),
            stddev=float(cfg.get("bollinger_touch_stddev", 2.0)),
            lookback=int(cfg.get("bollinger_touch_lookback_days", 10)),
            tolerance_atr_multiple=float(
                cfg.get("bollinger_touch_tolerance_atr_multiple", 0.25)
            ),
            tolerance_pct=float(cfg.get("bollinger_touch_tolerance_pct", 1.0)),
            atr14=atr14,
        )
        metrics.update(touch_metrics)
        if not touched:
            reasons.append("no_recent_bollinger_lower_touch")

    return not reasons, reasons, metrics


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
    mode = _normalize_scoring_mode(scoring.get("mode", "legacy"))
    if mode == "binary_override":
        _score_zones_binary_override(zones, scoring)
        return
    if mode == "random_override":
        _score_zones_random_override(zones, bars, scoring)
        return
    if mode != "legacy":
        raise ValueError(f"unsupported support scoring mode: {mode}")

    current = bars[-1].close
    tolerance = _zone_width(current, atr14, 0.5, 1.0)
    min_rejections = int(cfg.get("rejection_count", {}).get("min_rejections_required", 2))

    recent_new_low = _recent_new_low_value(bars, bars_count=5, lookback=50)
    context_delta, context_reasons = _context_score_adjustment(
        _support_context_metrics(bars, cfg, atr14),
        scoring.get("context_adjustments", {}),
    )

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
        if context_delta:
            score += context_delta
            zone.reasons.extend(context_reasons)
        zone.score = max(0.0, score)


def _normalize_scoring_mode(value: Any) -> str:
    mode = str(value or "legacy").strip().lower().replace("-", "_")
    aliases = {
        "default": "legacy",
        "weighted": "legacy",
        "confluence_weighted": "legacy",
        "binary": "binary_override",
        "binary_ablation": "binary_override",
        "random": "random_override",
        "random_ablation": "random_override",
        "randomized": "random_override",
    }
    return aliases.get(mode, mode)


def _score_zones_binary_override(
    zones: list[SupportZone],
    scoring: Mapping[str, Any],
) -> None:
    score = float(scoring.get("binary_override_score", 100.0))
    for zone in zones:
        zone.score = max(0.0, score)
        zone.reasons.append("binary override score")


def _score_zones_random_override(
    zones: list[SupportZone],
    bars: list[PriceBar],
    scoring: Mapping[str, Any],
) -> None:
    low = float(scoring.get("random_score_min", 0.0))
    high = float(scoring.get("random_score_max", 100.0))
    if high < low:
        raise ValueError("support.scoring.random_score_max must be >= random_score_min")
    seed = int(scoring.get("random_seed", 0))
    as_of = bars[-1].date.isoformat()
    for zone in zones:
        payload = "|".join(
            [
                str(seed),
                as_of,
                zone.method,
                f"{zone.center:.6f}",
                f"{zone.bottom:.6f}",
                f"{zone.top:.6f}",
                str(zone.touches),
                str(zone.rejections),
            ]
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        unit = int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        zone.score = low + (high - low) * unit
        zone.reasons.append("random override score")


def _context_score_adjustment(
    metrics: Mapping[str, Any],
    cfg: Mapping[str, Any],
) -> tuple[float, list[str]]:
    if not cfg.get("enabled", False):
        return 0.0, []
    delta = 0.0
    reasons: list[str] = []

    def add_if_lte(metric_name: str, threshold_key: str, weight_key: str, label: str) -> None:
        nonlocal delta
        weight = float(cfg.get(weight_key, 0.0))
        if not weight:
            return
        threshold = cfg.get(threshold_key)
        value = metrics.get(metric_name)
        if threshold is None or value is None:
            return
        if float(value) <= float(threshold):
            delta += weight
            reasons.append(f"{label} bonus")

    def subtract_if_gt(metric_name: str, threshold_key: str, weight_key: str, label: str) -> None:
        nonlocal delta
        penalty = float(cfg.get(weight_key, 0.0))
        if not penalty:
            return
        threshold = cfg.get(threshold_key)
        value = metrics.get(metric_name)
        if threshold is None or value is None:
            return
        if float(value) > float(threshold):
            delta -= penalty
            reasons.append(f"{label} penalty")

    rsi_period = int(cfg.get("rsi_period", 14))
    rsi_key = f"rsi{rsi_period}"
    add_if_lte(rsi_key, "rsi_bonus_max", "rsi_bonus", "rsi-not-overbought")
    subtract_if_gt(rsi_key, "rsi_penalty_min", "rsi_penalty", "rsi-overbought")
    add_if_lte(
        "close_vs_sma50_pct",
        "close_vs_sma50_bonus_max_pct",
        "close_vs_sma50_bonus",
        "sma50-extension-contained",
    )
    subtract_if_gt(
        "close_vs_sma50_pct",
        "close_vs_sma50_penalty_min_pct",
        "close_vs_sma50_penalty",
        "sma50-extension-overheated",
    )
    add_if_lte(
        "return_10d_pct",
        "return_10d_bonus_max_pct",
        "return_10d_bonus",
        "10d-return-contained",
    )
    subtract_if_gt(
        "return_10d_pct",
        "return_10d_penalty_min_pct",
        "return_10d_penalty",
        "10d-return-overheated",
    )
    add_if_lte(
        "return_20d_pct",
        "return_20d_bonus_max_pct",
        "return_20d_bonus",
        "20d-return-contained",
    )
    subtract_if_gt(
        "return_20d_pct",
        "return_20d_penalty_min_pct",
        "return_20d_penalty",
        "20d-return-overheated",
    )
    add_if_lte(
        "bollinger_position",
        "bollinger_position_bonus_max",
        "bollinger_position_bonus",
        "bollinger-lower-position",
    )
    subtract_if_gt(
        "bollinger_position",
        "bollinger_position_penalty_min",
        "bollinger_position_penalty",
        "bollinger-upper-position",
    )
    return delta, reasons


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


def _sma_last(values: list[float], period: int) -> float | None:
    series = sma_values(values, period)
    return series[-1] if series else None


def _lookback_return_pct(values: list[float], lookback: int) -> float | None:
    if lookback <= 0 or len(values) <= lookback:
        return None
    previous = values[-1 - lookback]
    if previous <= 0:
        return None
    return (values[-1] / previous - 1.0) * 100.0


def _recent_high(bars: list[PriceBar], lookback: int) -> float | None:
    if lookback <= 0 or len(bars) < lookback:
        return None
    return max(bar.high for bar in bars[-lookback:])


def _recent_bollinger_touch(
    bars: list[PriceBar],
    *,
    window: int,
    stddev: float,
    lookback: int,
    tolerance_atr_multiple: float,
    tolerance_pct: float,
    atr14: float | None,
) -> tuple[bool, dict[str, Any]]:
    if lookback <= 0 or len(bars) < window:
        return False, {
            "recent_bollinger_touch": False,
            "recent_bollinger_touch_date": None,
            "bollinger_lower": None,
        }
    closes = [bar.close for bar in bars]
    start = max(window - 1, len(bars) - lookback)
    latest_lower = bollinger_lower(closes, window, stddev)
    for index in range(len(bars) - 1, start - 1, -1):
        lower = bollinger_lower(closes[: index + 1], window, stddev)
        if lower is None:
            continue
        bar = bars[index]
        tolerance = max(
            (atr14 or 0.0) * tolerance_atr_multiple,
            bar.close * tolerance_pct / 100.0,
            0.0,
        )
        if bar.low <= lower + tolerance:
            return True, {
                "recent_bollinger_touch": True,
                "recent_bollinger_touch_date": bar.date.isoformat(),
                "bollinger_lower": latest_lower,
                "bollinger_touch_lower": lower,
                "bollinger_touch_tolerance": tolerance,
            }
    return False, {
        "recent_bollinger_touch": False,
        "recent_bollinger_touch_date": None,
        "bollinger_lower": latest_lower,
    }
