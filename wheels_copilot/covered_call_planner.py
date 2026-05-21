from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from typing import Any

from .alpaca import AlpacaMarketDataClient
from .market_data import fetch_call_chain
from .models import OptionQuote


MONEY_EPSILON = 0.005


def build_covered_call_proposals(
    lifecycle: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    as_of: date | None = None,
    client: AlpacaMarketDataClient | None = None,
    option_chain_by_ticker: dict[str, list[OptionQuote]] | None = None,
) -> dict[str, Any]:
    """Build dry-run covered-call proposals for assigned wheel positions."""

    config = config or {}
    as_of = as_of or date.fromisoformat(str(lifecycle.get("as_of") or date.today()))
    generated_at = _now_iso()
    cc_cfg = config.get("cc_selector") or {}
    dte_min = int(cc_cfg.get("dte_min", 1))
    dte_max = int(cc_cfg.get("dte_max", 9))
    proposals = []
    audit = []

    for position in sorted(lifecycle.get("positions") or [], key=lambda row: row["ticker"]):
        if not position.get("covered_call_eligible"):
            audit.append(_audit_position(position))
            continue
        ticker = str(position["ticker"]).upper()
        chain = (
            option_chain_by_ticker.get(ticker)
            if option_chain_by_ticker is not None
            else fetch_call_chain(
                ticker,
                dte_min=dte_min,
                dte_max=dte_max,
                as_of=as_of,
                config=config,
                client=client,
            )
        )
        proposal = _position_proposal(
            position=position,
            options=chain or [],
            scan_date=as_of.isoformat(),
            generated_at=generated_at,
            config=config,
        )
        proposals.append(proposal)

    summary = Counter(proposal["decision"] for proposal in proposals)
    return {
        "scan_date": as_of.isoformat(),
        "generated_at": generated_at,
        "source_lifecycle_generated_at": lifecycle.get("generated_at"),
        "dry_run_only": True,
        "planner": {
            "version": 1,
            "strategy": "covered_call",
            "assignment_state_required": "ASSIGNED",
            "auto_submit": False,
            "dte_min": dte_min,
            "dte_max": dte_max,
        },
        "summary": dict(sorted(summary.items())),
        "proposal_count": len(proposals),
        "audit_count": len(audit),
        "proposals": proposals,
        "non_eligible_audit": audit,
    }


def build_covered_call_shadow_orders(
    covered_call_proposals: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or {}
    order_cfg = (config.get("trade_planner") or {}).get("shadow_order") or {}
    broker = str(order_cfg.get("broker", "alpaca"))
    order_type = str(order_cfg.get("type", "limit"))
    time_in_force = str(order_cfg.get("time_in_force", "day"))
    generated_at = _now_iso()
    orders = []
    for proposal in covered_call_proposals.get("proposals") or []:
        if proposal.get("decision") != "PROPOSED":
            continue
        option = proposal.get("option") or {}
        proposal_id = proposal["proposal_id"]
        orders.append(
            {
                "shadow_order_id": proposal_id,
                "proposal_id": proposal_id,
                "created_at": generated_at,
                "dry_run_only": True,
                "broker": broker,
                "strategy": "covered_call",
                "ticker": proposal.get("ticker"),
                "estimated_premium_credit": proposal.get("estimated_premium_credit"),
                "share_quantity": proposal.get("share_quantity"),
                "available_shares_for_cc": proposal.get("available_shares_for_cc"),
                "adjusted_cost_basis": proposal.get("adjusted_cost_basis"),
                "min_acceptable_strike": proposal.get("min_acceptable_strike"),
                "unchecked_risks": proposal.get("unchecked_risks") or [],
                "payload": {
                    "symbol": option.get("symbol"),
                    "qty": str(proposal.get("quantity", 1)),
                    "side": "sell",
                    "type": order_type,
                    "time_in_force": time_in_force,
                    "limit_price": _money_string(proposal.get("limit_price")),
                    "position_intent": "sell_to_open",
                    "client_order_id": proposal_id,
                },
                "warnings": ["dry_run_only_not_submitted"],
            }
        )
    return {
        "scan_date": covered_call_proposals.get("scan_date"),
        "generated_at": generated_at,
        "source_proposals_generated_at": covered_call_proposals.get("generated_at"),
        "dry_run_only": True,
        "broker": broker,
        "order_count": len(orders),
        "orders": orders,
    }


def render_covered_call_report(
    lifecycle: dict[str, Any],
    proposals: dict[str, Any],
) -> str:
    lines = [
        f"# Covered Call Planner - {proposals['scan_date']}",
        "",
        f"- Generated: `{proposals['generated_at']}`",
        f"- Lifecycle summary: `{lifecycle.get('summary')}`",
        f"- Proposal summary: `{proposals.get('summary')}`",
        "",
        "| Decision | Ticker | State | Shares | Cost Basis | Call Candidate | Premium | Reasons |",
        "|---|---:|---|---:|---:|---|---:|---|",
    ]
    position_by_ticker = {
        str(position["ticker"]).upper(): position
        for position in lifecycle.get("positions") or []
    }
    for proposal in proposals.get("proposals") or []:
        ticker = str(proposal.get("ticker") or "").upper()
        position = position_by_ticker.get(ticker) or {}
        option = proposal.get("option") or {}
        if option:
            candidate = (
                f"{option.get('expiration')} {option.get('strike')}C "
                f"delta {_fmt(option.get('delta'), 3)}"
            )
        else:
            candidate = "-"
        lines.append(
            f"| {_md(proposal.get('decision'))} | {_md(ticker)} | {_md(position.get('state'))} | "
            f"{_fmt(position.get('long_shares'), 0)} | {_fmt(position.get('adjusted_cost_basis'))} | "
            f"{_md(candidate)} | {_fmt(proposal.get('estimated_premium_credit'), 0)} | "
            f"{_md('; '.join(proposal.get('decision_reasons') or []))} |"
        )
    if not proposals.get("proposals"):
        lines.append("| - | - | - | - | - | - | - | no assigned positions eligible |")
    return "\n".join(lines).rstrip() + "\n"


def _position_proposal(
    *,
    position: dict[str, Any],
    options: list[OptionQuote],
    scan_date: str,
    generated_at: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    ticker = str(position["ticker"]).upper()
    cc_cfg = config.get("cc_selector") or {}
    quantity = _covered_call_quantity(position, config)
    rejected = []
    eligible = []
    for option in options:
        reasons = _option_rejection_reasons(option, position, config)
        if reasons:
            rejected.append(_rejected_option(option, reasons))
        else:
            eligible.append(option)
    if quantity <= 0:
        return _watch_proposal(
            ticker,
            position,
            scan_date,
            generated_at,
            ["no_uncovered_100_share_lot"],
            rejected,
        )
    if not eligible:
        return _watch_proposal(
            ticker,
            position,
            scan_date,
            generated_at,
            _top_rejection_reasons(rejected) or ["no_eligible_call_contract"],
            rejected,
        )
    selected = sorted(eligible, key=lambda option: _option_rank(option, cc_cfg))[0]
    limit_price = _limit_price(selected)
    proposal_id = _proposal_id(scan_date, ticker, selected.expiration.isoformat(), selected.strike)
    premium = limit_price * 100.0 * quantity
    return {
        "proposal_id": proposal_id,
        "created_at": generated_at,
        "scan_date": scan_date,
        "ticker": ticker,
        "strategy": "covered_call",
        "decision": "PROPOSED",
        "decision_reasons": ["assigned_stock_has_eligible_covered_call"],
        "requires_manual_review": False,
        "quantity": quantity,
        "share_quantity": position.get("long_shares"),
        "available_shares_for_cc": position.get("available_shares_for_cc"),
        "adjusted_cost_basis": position.get("adjusted_cost_basis"),
        "min_acceptable_strike": _round_or_none(_min_acceptable_strike(position, config)),
        "estimated_premium_credit": round(premium, 2),
        "limit_price": round(limit_price, 2),
        "option": _option_payload(selected),
        "unchecked_risks": ["earnings_not_checked", "ex_dividend_not_checked"],
        "rejected_option_count": len(rejected),
        "rejection_summary": _rejection_summary(rejected),
        "shadow_order_ref": proposal_id,
    }


def _watch_proposal(
    ticker: str,
    position: dict[str, Any],
    scan_date: str,
    generated_at: str,
    reasons: list[str],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    proposal_id = _proposal_id(
        scan_date,
        ticker,
        "no-expiration",
        0,
        extra={"position": position, "reasons": reasons},
    )
    return {
        "proposal_id": proposal_id,
        "created_at": generated_at,
        "scan_date": scan_date,
        "ticker": ticker,
        "strategy": "covered_call",
        "decision": "WATCH",
        "decision_reasons": reasons,
        "requires_manual_review": True,
        "quantity": 0,
        "share_quantity": position.get("long_shares"),
        "available_shares_for_cc": position.get("available_shares_for_cc"),
        "adjusted_cost_basis": position.get("adjusted_cost_basis"),
        "min_acceptable_strike": None,
        "estimated_premium_credit": 0.0,
        "limit_price": None,
        "option": None,
        "unchecked_risks": ["earnings_not_checked", "ex_dividend_not_checked"],
        "rejected_option_count": len(rejected),
        "rejection_summary": _rejection_summary(rejected),
        "shadow_order_ref": None,
    }


def _option_rejection_reasons(
    option: OptionQuote,
    position: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    cc_cfg = config.get("cc_selector") or {}
    reasons = []
    min_strike = _min_acceptable_strike(position, config)
    if min_strike is None:
        reasons.append("adjusted_cost_basis_missing")
    elif option.strike + MONEY_EPSILON < min_strike:
        reasons.append(f"strike_below_adjusted_cost_basis:{option.strike:.2f}<{min_strike:.2f}")
    min_bid = float(cc_cfg.get("min_bid", 0.0))
    if option.bid + MONEY_EPSILON < min_bid:
        reasons.append(f"bid_below_min:{option.bid:.2f}<{min_bid:.2f}")
    max_spread = cc_cfg.get("max_spread_pct_of_mid")
    spread = option.spread_pct_of_mid
    if spread is None:
        reasons.append("spread_unavailable")
    elif max_spread is not None and spread - float(max_spread) > MONEY_EPSILON:
        reasons.append(f"spread_too_wide:{spread:.4f}>{float(max_spread):.4f}")
    min_oi = int(cc_cfg.get("min_open_interest", 0))
    if option.open_interest is None:
        reasons.append("open_interest_missing")
    elif option.open_interest < min_oi:
        reasons.append(f"open_interest_below_min:{option.open_interest}<{min_oi}")
    delta = option.delta
    min_delta = float(cc_cfg.get("target_delta_min", 0.0))
    max_delta = float(cc_cfg.get("target_delta_max", 1.0))
    if delta is None:
        reasons.append("delta_missing")
    elif delta < min_delta - MONEY_EPSILON or delta > max_delta + MONEY_EPSILON:
        reasons.append(f"delta_outside_target:{delta:.4f}")
    return reasons


def _min_acceptable_strike(position: dict[str, Any], config: dict[str, Any]) -> float | None:
    basis = _number(position.get("adjusted_cost_basis"))
    if basis is None:
        return None
    pct = float((config.get("cc_selector") or {}).get("min_strike_vs_cost_basis_pct", 0.0))
    return basis * (1.0 + pct / 100.0)


def _covered_call_quantity(position: dict[str, Any], config: dict[str, Any]) -> int:
    available = int(float(position.get("available_shares_for_cc") or 0.0) // 100)
    configured = int((config.get("cc_selector") or {}).get("default_contract_quantity", 1))
    return max(0, min(available, max(configured, 1)))


def _option_rank(option: OptionQuote, cc_cfg: dict[str, Any]) -> tuple[int, float, float, float]:
    midpoint = (
        float(cc_cfg.get("target_delta_min", 0.0))
        + float(cc_cfg.get("target_delta_max", 1.0))
    ) / 2.0
    delta_distance = abs(float(option.delta or 0.0) - midpoint)
    return (option.dte, delta_distance, -option.bid, option.strike)


def _limit_price(option: OptionQuote) -> float:
    return float(option.executable_mid or 0.0)


def _option_payload(option: OptionQuote) -> dict[str, Any]:
    return {
        "symbol": option.symbol,
        "expiration": option.expiration.isoformat(),
        "strike": option.strike,
        "put_call": "call",
        "dte": option.dte,
        "bid": option.bid,
        "ask": option.ask,
        "last": option.last,
        "mid": option.mid,
        "executable_mid": option.executable_mid,
        "delta": option.delta,
        "open_interest": option.open_interest,
        "volume": option.volume,
        "spread_pct_of_mid": option.spread_pct_of_mid,
        "quote_timestamp": option.quote_timestamp.isoformat()
        if option.quote_timestamp
        else None,
        "data_feed": option.data_feed,
    }


def _rejected_option(option: OptionQuote, reasons: list[str]) -> dict[str, Any]:
    return {
        "symbol": option.symbol,
        "expiration": option.expiration.isoformat(),
        "strike": option.strike,
        "bid": option.bid,
        "ask": option.ask,
        "delta": option.delta,
        "open_interest": option.open_interest,
        "spread_pct_of_mid": option.spread_pct_of_mid,
        "reasons": reasons,
    }


def _rejection_summary(rejected: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for option in rejected:
        for reason in option.get("reasons") or []:
            counts[str(reason).split(":", 1)[0]] += 1
    return dict(sorted(counts.items()))


def _top_rejection_reasons(rejected: list[dict[str, Any]], limit: int = 3) -> list[str]:
    return list(_rejection_summary(rejected).keys())[:limit]


def _audit_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": position.get("ticker"),
        "state": position.get("state"),
        "covered_call_eligible": position.get("covered_call_eligible"),
        "long_shares": position.get("long_shares"),
        "available_shares_for_cc": position.get("available_shares_for_cc"),
        "reasons": position.get("reasons") or [],
    }


def _proposal_id(
    scan_date: str,
    ticker: str,
    expiration: str,
    strike: float,
    *,
    extra: dict[str, Any] | None = None,
) -> str:
    clean_scan_date = scan_date.replace("-", "")[2:]
    clean_expiration = expiration.replace("-", "")[2:]
    strike_cents = int(round(strike * 100))
    extra_text = json.dumps(extra or {}, sort_keys=True, default=str)
    raw = f"wheel-cc-{scan_date}-{ticker}-{expiration}-{strike_cents}-{extra_text}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"whcc-{clean_scan_date}-{ticker[:6]}-{clean_expiration}-{strike_cents}-{digest}"


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: Any) -> float | None:
    parsed = _number(value)
    return round(parsed, 2) if parsed is not None else None


def _money_string(value: Any) -> str:
    return f"{float(value or 0.0):.2f}"


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _md(value: Any) -> str:
    return _fmt(value).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
