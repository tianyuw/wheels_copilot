from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import (
    CspCandidate,
    CspSelectionResult,
    OptionQuote,
    RejectedCspOption,
    SupportAnalysis,
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
        if strike_status == "above_support_zone":
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
        inside_zone_watch_only = strike_status == "inside_support_zone"
        auto_trade = (
            policy_auto_trade
            and support.tradable
            and strike_status == "below_support_zone"
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
