from __future__ import annotations

from datetime import date
from typing import Any

from .models import FundamentalSnapshot, GateResult, OptionQuote


CHINESE_ADR_TICKERS = {
    "BABA",
    "BIDU",
    "GOTU",
    "JD",
    "LI",
    "NIO",
    "PDD",
    "TAL",
    "TME",
    "XPEV",
}


def evaluate_fundamentals(
    snapshot: FundamentalSnapshot,
    config: dict[str, Any],
) -> GateResult:
    cfg = config.get("fundamental_filters", {})
    reasons: list[str] = []
    warnings: list[str] = []

    if cfg.get("reject_leveraged_etf", True) and _looks_leveraged_etf(snapshot):
        reasons.append("leveraged_etf")

    if cfg.get("reject_biotech", True) and _looks_biotech(snapshot):
        reasons.append("biotech_or_binary_event_industry")

    if cfg.get("reject_chinese_adr", True) and _looks_chinese_adr(snapshot):
        reasons.append("chinese_adr")

    if cfg.get("reject_recent_100pct_movers", True):
        if snapshot.recent_move_pct is None:
            warnings.append("recent_move_unavailable")
        elif snapshot.recent_move_pct >= 100:
            reasons.append(f"recent_move_{snapshot.recent_move_pct:.1f}%")

    if not snapshot.is_etf:
        _evaluate_stock_quality(snapshot, cfg, reasons, warnings)
    else:
        if snapshot.market_cap is not None and snapshot.market_cap < float(
            cfg.get("market_cap_min_hard", 2_000_000_000)
        ):
            warnings.append("etf_assets_below_stock_market_cap_hard_floor")

    if reasons:
        return GateResult(status="REJECT", reasons=reasons, warnings=warnings)
    if warnings:
        return GateResult(status="WARN", reasons=["fundamental_review_required"], warnings=warnings)
    return GateResult(status="PASS", reasons=["fundamentals_passed"], warnings=[])


def evaluate_earnings_gate(
    snapshot: FundamentalSnapshot,
    options: list[OptionQuote],
    as_of: date,
) -> tuple[GateResult, list[OptionQuote]]:
    if snapshot.next_earnings_date is None:
        return (
            GateResult(
                status="WARN",
                reasons=["earnings_date_unknown"],
                warnings=["cannot_confirm_expiration_before_earnings"],
            ),
            options,
        )

    if snapshot.next_earnings_date < as_of:
        return (
            GateResult(
                status="WARN",
                reasons=["earnings_date_stale"],
                warnings=[f"next_earnings_date_before_scan_date:{snapshot.next_earnings_date}"],
            ),
            options,
        )

    allowed = [opt for opt in options if opt.expiration < snapshot.next_earnings_date]
    excluded = len(options) - len(allowed)
    if not options:
        return (
            GateResult(status="WARN", reasons=["no_options_to_check_for_earnings"], warnings=[]),
            [],
        )
    if not allowed:
        return (
            GateResult(
                status="REJECT",
                reasons=[f"all_expirations_on_or_after_earnings:{snapshot.next_earnings_date}"],
                warnings=[],
            ),
            [],
        )
    if excluded:
        return (
            GateResult(
                status="PASS",
                reasons=[
                    "some_expirations_excluded_for_earnings",
                    f"excluded_contracts:{excluded}",
                    f"next_earnings_date:{snapshot.next_earnings_date}",
                ],
                warnings=[],
            ),
            allowed,
        )
    return (
        GateResult(
            status="PASS",
            reasons=[f"earnings_after_candidate_expirations:{snapshot.next_earnings_date}"],
            warnings=[],
        ),
        allowed,
    )


def _evaluate_stock_quality(
    snapshot: FundamentalSnapshot,
    cfg: dict[str, Any],
    reasons: list[str],
    warnings: list[str],
) -> None:
    market_cap_min = float(cfg.get("market_cap_min_hard", 2_000_000_000))
    market_cap_preferred = float(cfg.get("market_cap_preferred", 5_000_000_000))
    pe_max = float(cfg.get("pe_max", 50))

    if snapshot.market_cap is None:
        warnings.append("market_cap_unavailable")
    elif snapshot.market_cap < market_cap_min:
        reasons.append(f"market_cap_below_{int(market_cap_min)}")
    elif snapshot.market_cap < market_cap_preferred:
        warnings.append(f"market_cap_below_preferred_{int(market_cap_preferred)}")

    if snapshot.pe_ratio is None:
        warnings.append("pe_ratio_unavailable")
    elif snapshot.pe_ratio <= 0:
        reasons.append("pe_ratio_non_positive")
    elif snapshot.pe_ratio >= pe_max:
        reasons.append(f"pe_ratio_at_or_above_{pe_max:g}")

    min_quarters = int(cfg.get("min_positive_quarters_out_of_5", 4))
    min_years = int(cfg.get("min_positive_years_out_of_5", 4))
    _evaluate_positive_income(
        values=snapshot.quarterly_net_income,
        required=min_quarters,
        label="positive_quarters",
        reasons=reasons,
        warnings=warnings,
    )
    _evaluate_positive_income(
        values=snapshot.annual_net_income,
        required=min_years,
        label="positive_years",
        reasons=reasons,
        warnings=warnings,
    )

    if cfg.get("prefer_dividend", True) and not snapshot.dividend_yield:
        warnings.append("no_dividend")


def _evaluate_positive_income(
    values: list[float],
    required: int,
    label: str,
    reasons: list[str],
    warnings: list[str],
) -> None:
    if len(values) < required:
        warnings.append(f"{label}_data_insufficient")
        return
    sample_size = min(5, len(values))
    positives = sum(1 for value in values[:sample_size] if value > 0)
    if positives < required:
        reasons.append(f"{label}_{positives}_of_{sample_size}_below_{required}")


def _looks_biotech(snapshot: FundamentalSnapshot) -> bool:
    text = _snapshot_text(snapshot)
    return "biotech" in text or "biotechnology" in text


def _looks_chinese_adr(snapshot: FundamentalSnapshot) -> bool:
    text = _snapshot_text(snapshot)
    if snapshot.ticker.upper() in CHINESE_ADR_TICKERS:
        return True
    if snapshot.country and snapshot.country.strip().lower() in {"china", "hong kong"}:
        return True
    return "adr" in text and ("china" in text or "chinese" in text)


def _looks_leveraged_etf(snapshot: FundamentalSnapshot) -> bool:
    if not snapshot.is_etf:
        return False
    text = _snapshot_text(snapshot)
    markers = [
        "2x",
        "3x",
        "leveraged",
        "ultra",
        "ultrapro",
        "bear 2x",
        "bull 2x",
        "bear 3x",
        "bull 3x",
        "daily bull",
        "daily bear",
    ]
    return any(marker in text for marker in markers)


def _snapshot_text(snapshot: FundamentalSnapshot) -> str:
    return " ".join(
        str(value or "")
        for value in (
            snapshot.ticker,
            snapshot.quote_type,
            snapshot.short_name,
            snapshot.long_name,
            snapshot.sector,
            snapshot.industry,
            snapshot.country,
        )
    ).lower()
