from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .models import (
    CspCandidate,
    CspSelectionResult,
    OptionQuote,
    RejectedCspOption,
    SupportAnalysis,
    SupportZone,
)
from .option_math import black_scholes_put_delta


def select_csp_candidate(
    options: list[OptionQuote],
    support: SupportAnalysis,
    config: Mapping[str, Any],
    risk_free_rate: float = 0.04,
) -> CspCandidate | None:
    return evaluate_csp_candidates(options, support, config, risk_free_rate).candidate


def evaluate_csp_candidates(
    options: list[OptionQuote],
    support: SupportAnalysis,
    config: Mapping[str, Any],
    risk_free_rate: float = 0.04,
) -> CspSelectionResult:
    zones = _zones_for_selection_policy(support, config)
    if not zones:
        return CspSelectionResult(candidate=None)

    policy = _support_selection_policy(config)
    combined_rejected: list[RejectedCspOption] = []
    first_watch_result: CspSelectionResult | None = None
    total_zones = len(zones)
    for rank, zone in enumerate(zones, start=1):
        zone_support = _support_with_selected_zone(support, zone)
        result = _evaluate_csp_candidates_for_selected_zone(
            options,
            zone_support,
            config,
            risk_free_rate,
        )
        combined_rejected.extend(result.rejected)
        if result.candidate is None:
            continue
        _annotate_support_selection(
            result.candidate,
            policy=policy,
            rank=rank,
            total_zones=total_zones,
        )
        if result.candidate.auto_trade:
            return result
        if first_watch_result is None:
            first_watch_result = result

    if first_watch_result is not None:
        first_watch_result.rejected = combined_rejected
        return first_watch_result
    return CspSelectionResult(candidate=None, rejected=combined_rejected)


def _evaluate_csp_candidates_for_selected_zone(
    options: list[OptionQuote],
    support: SupportAnalysis,
    config: Mapping[str, Any],
    risk_free_rate: float = 0.04,
) -> CspSelectionResult:
    csp_cfg = config.get("csp_selector", {})
    zone = support.selected_zone
    if zone is None:
        return CspSelectionResult(candidate=None)

    policy_name, policy = _delta_policy_for_score(
        zone.score, csp_cfg.get("delta_policy", {})
    )
    min_delta = float(policy.get("target_delta_min", 0.10))
    max_delta = float(policy.get("target_delta_max", 0.25))
    policy_auto_trade = bool(policy.get("auto_trade", False))
    min_distance = max(
        (support.atr14 or 0.0)
        * float(csp_cfg.get("min_strike_distance_atr_multiple", 1.0)),
        support.current_price
        * float(csp_cfg.get("min_strike_distance_pct", 3.0))
        / 100.0,
    )
    max_single_ticker_assignment = _max_single_ticker_assignment(config)

    candidates: list[CspCandidate] = []
    rejected: list[RejectedCspOption] = []
    for option in options:
        reasons: list[str] = []
        delta = _option_delta(option, support.current_price, risk_free_rate)
        if delta is None:
            reasons.append("missing_delta")
        else:
            abs_delta = abs(delta)
            if abs_delta < min_delta:
                reasons.append("delta_too_low")
            if abs_delta > max_delta:
                reasons.append("delta_too_high")

        strike_status = _strike_status(option, support, config)
        above_support_allowed = _allows_above_support_zone(csp_cfg)
        if strike_status == "above_support_zone" and not above_support_allowed:
            reasons.append("strike_above_support_zone")

        if support.current_price - option.strike < min_distance:
            reasons.append("strike_too_close_to_spot")
        if option.bid < float(csp_cfg.get("min_bid", 0.20)):
            reasons.append("bid_below_min")
        executable_mid = option.executable_mid
        if executable_mid is None:
            reasons.append("no_executable_bid_ask")
        if option.open_interest is not None and option.open_interest < int(
            csp_cfg.get("min_open_interest", 100)
        ):
            reasons.append("open_interest_below_min")
        spread_pct = option.spread_pct_of_mid
        if spread_pct is None:
            reasons.append("missing_spread")
        elif spread_pct > float(csp_cfg.get("max_spread_pct_of_mid", 0.12)):
            reasons.append("spread_too_wide")

        raw_return = (
            executable_mid / option.strike * 100.0
            if executable_mid is not None and option.strike > 0
            else 0.0
        )
        weekly_return = raw_return * 7.0 / max(option.dte, 1)
        if weekly_return < float(csp_cfg.get("min_weekly_return_on_strike_pct", 0.25)):
            reasons.append("weekly_return_below_min")

        assignment_cash_required = option.strike * 100.0
        if (
            max_single_ticker_assignment is not None
            and assignment_cash_required > max_single_ticker_assignment
        ):
            reasons.append("assignment_cash_above_single_ticker_limit")

        if reasons:
            rejected.append(RejectedCspOption(option=option, reasons=reasons))
            continue

        assert delta is not None
        inside_zone_auto_trade = (
            strike_status == "inside_support_zone"
            and bool(csp_cfg.get("auto_trade_inside_support_zone", False))
        )
        inside_zone_watch_only = (
            strike_status == "inside_support_zone" and not inside_zone_auto_trade
        )
        above_zone_auto_trade = (
            strike_status == "above_support_zone"
            and above_support_allowed
            and bool(csp_cfg.get("auto_trade_above_support_zone", False))
        )
        above_zone_watch_only = (
            strike_status == "above_support_zone" and not above_zone_auto_trade
        )
        auto_trade = (
            policy_auto_trade
            and support.tradable
            and (
                strike_status == "below_support_zone"
                or inside_zone_auto_trade
                or above_zone_auto_trade
            )
        )

        candidates.append(
            CspCandidate(
                option=option,
                support_zone=zone,
                delta=delta,
                delta_bucket=policy_name,
                auto_trade=auto_trade,
                weekly_return_on_strike_pct=weekly_return,
                assignment_cash_required=assignment_cash_required,
                reasons=[
                    f"support score {zone.score:.1f}",
                    f"delta bucket {policy_name}",
                    f"strike status {strike_status}",
                ],
                diagnostics={
                    "support_top": zone.top,
                    "support_bottom": zone.bottom,
                    "support_method": zone.method,
                    "option_executable_mid": executable_mid,
                    "raw_return_on_strike_pct": raw_return,
                    "spread_pct_of_mid": spread_pct,
                    "inside_zone_watch_only": inside_zone_watch_only,
                    "above_zone_watch_only": above_zone_watch_only,
                    "above_support_allowed": above_support_allowed,
                },
            )
        )

    if not candidates:
        return CspSelectionResult(
            candidate=None,
            rejected=rejected,
            policy_name=policy_name,
            policy=dict(policy),
        )

    # Prefer auto-tradable, more conservative contracts once minimum premium
    # and liquidity gates are met. This avoids always picking the highest
    # delta/highest premium contract inside a bucket.
    candidates.sort(
        key=lambda c: (
            not c.auto_trade,
            abs(c.delta),
            -_distance_below_support(c),
            -c.weekly_return_on_strike_pct,
        )
    )
    return CspSelectionResult(
        candidate=candidates[0],
        rejected=rejected,
        policy_name=policy_name,
        policy=dict(policy),
    )


def _support_selection_policy(config: Mapping[str, Any]) -> str:
    support_cfg = config.get("support", {})
    policy = str(support_cfg.get("selection_policy", "highest_score")).strip().lower()
    aliases = {
        "highest": "highest_score",
        "score": "highest_score",
        "nearest": "nearest_qualified",
        "candidate_aware": "candidate_aware_top3",
        "candidate-aware": "candidate_aware_top3",
    }
    return aliases.get(policy, policy)


def _zones_for_selection_policy(
    support: SupportAnalysis,
    config: Mapping[str, Any],
) -> list[SupportZone]:
    policy = _support_selection_policy(config)
    if policy == "highest_score":
        return [support.selected_zone] if support.selected_zone is not None else []
    if policy == "nearest_qualified":
        zones = _qualified_zones(support) or support.zones
        if not zones:
            return []
        return [
            sorted(
                zones,
                key=lambda zone: (
                    max(0.0, support.current_price - zone.top),
                    -zone.score,
                    -zone.top,
                ),
            )[0]
        ]
    if policy.startswith("candidate_aware"):
        top_k = _candidate_aware_top_k(policy)
        zones = _qualified_zones(support) or support.zones
        return sorted(zones, key=lambda zone: zone.score, reverse=True)[:top_k]
    return [support.selected_zone] if support.selected_zone is not None else []


def _qualified_zones(support: SupportAnalysis) -> list[SupportZone]:
    return [zone for zone in support.zones if zone.score >= support.min_score_to_trade]


def _candidate_aware_top_k(policy: str) -> int:
    match = re.search(r"top[_-]?(\d+)$", policy)
    if match:
        return max(1, int(match.group(1)))
    return 3


def _support_with_selected_zone(
    support: SupportAnalysis,
    zone: SupportZone,
) -> SupportAnalysis:
    return SupportAnalysis(
        trend=support.trend,
        zones=support.zones,
        selected_zone=zone,
        atr14=support.atr14,
        current_price=support.current_price,
        min_score_to_trade=support.min_score_to_trade,
        preconditions_passed=support.preconditions_passed,
        precondition_metrics=support.precondition_metrics,
        context_metrics=support.context_metrics,
        reasons=support.reasons,
    )


def _annotate_support_selection(
    candidate: CspCandidate,
    *,
    policy: str,
    rank: int,
    total_zones: int,
) -> None:
    candidate.diagnostics["support_selection_policy"] = policy
    candidate.diagnostics["support_zone_rank"] = rank
    candidate.diagnostics["support_zones_considered"] = total_zones


def _delta_policy_for_score(
    score: float, policy_cfg: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]]:
    strong = policy_cfg.get("strong_support", {})
    normal = policy_cfg.get("normal_support", {})
    if score >= float(strong.get("min_support_score", 85)):
        return "strong_support", strong
    if score >= float(normal.get("min_support_score", 70)) and score <= float(
        normal.get("max_support_score", 84)
    ):
        return "normal_support", normal
    return "manual_review", policy_cfg.get("manual_review", {})


def _strike_status(
    option: OptionQuote, support: SupportAnalysis, config: Mapping[str, Any]
) -> str:
    csp_cfg = config.get("csp_selector", {})
    zone = support.selected_zone
    if zone is None:
        return "no_support_zone"
    if option.strike <= zone.bottom:
        return "below_support_zone"
    mode = str(config.get("mode", "paper")).lower()
    if (
        mode == "paper"
        and csp_cfg.get("allow_strike_inside_support_zone_only_in_paper", False)
        and option.strike <= zone.top
    ):
        return "inside_support_zone"
    return "above_support_zone"


def _allows_above_support_zone(csp_cfg: Mapping[str, Any]) -> bool:
    if bool(csp_cfg.get("allow_strike_above_support_zone", False)):
        return True
    return not bool(csp_cfg.get("require_strike_at_or_below_support_zone_bottom", True))


def _distance_below_support(candidate: CspCandidate) -> float:
    return candidate.support_zone.bottom - candidate.option.strike


def _max_single_ticker_assignment(config: Mapping[str, Any]) -> float | None:
    risk = config.get("risk", {})
    account = config.get("account", {})
    limits = []
    if risk.get("max_single_ticker_assignment_dollars") is not None:
        limits.append(float(risk["max_single_ticker_assignment_dollars"]))
    if (
        risk.get("max_single_ticker_assignment_pct") is not None
        and account.get("starting_equity") is not None
    ):
        limits.append(
            float(account["starting_equity"])
            * float(risk["max_single_ticker_assignment_pct"])
        )
    return min(limits) if limits else None


def _option_delta(
    option: OptionQuote, stock_price: float, risk_free_rate: float
) -> float | None:
    if option.delta is not None:
        return option.delta
    if option.implied_volatility is None:
        return None
    return black_scholes_put_delta(
        stock_price=stock_price,
        strike=option.strike,
        dte=option.dte,
        implied_volatility=option.implied_volatility,
        risk_free_rate=risk_free_rate,
    )
