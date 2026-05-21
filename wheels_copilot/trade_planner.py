from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timezone
from typing import Any


DEFAULT_STARTING_EQUITY = 500_000.0
MONEY_EPSILON = 0.005


def build_trade_proposals(
    scan: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build auditable CSP trade proposals from a dry-run scan result.

    The planner is intentionally dry-run only. It converts AUTO_TRADE scan rows
    into proposed one-contract cash-secured puts, while preserving WATCH and
    rejected candidate rows for review. Cash allocation is applied sequentially
    across the scan order so individually valid candidates cannot over-allocate
    the paper account as a group.
    """

    config = config or {}
    generated_at = _now_iso()
    allocation = _initial_allocation(scan, config)
    quantity = _contract_quantity(config)

    proposals: list[dict[str, Any]] = []
    rejected_audit: list[dict[str, Any]] = []
    for row in sorted(scan.get("results", []), key=_planner_order_key):
        candidate = row.get("candidate")
        if not candidate:
            rejected_audit.append(_audit_row(row))
            continue

        proposal = _candidate_proposal(
            row=row,
            scan_date=str(scan.get("scan_date") or ""),
            generated_at=generated_at,
            allocation=allocation,
            quantity=quantity,
        )
        proposals.append(proposal)
        if proposal["decision"] == "PROPOSED":
            allocation["running_reserved_assignment_cash"] += proposal[
                "assignment_cash_required"
            ]

    summary = Counter(proposal["decision"] for proposal in proposals)
    return {
        "scan_date": scan.get("scan_date"),
        "generated_at": generated_at,
        "source_scan_generated_at": scan.get("generated_at"),
        "dry_run_only": True,
        "planner": {
            "version": 1,
            "strategy": "cash_secured_put",
            "default_contract_quantity": quantity,
            "allocation_mode": "sequential_scan_order",
        },
        "allocation": _allocation_summary(allocation),
        "summary": dict(sorted(summary.items())),
        "proposal_count": len(proposals),
        "rejected_audit_count": len(rejected_audit),
        "proposals": proposals,
        "rejected_audit": rejected_audit,
    }


def build_shadow_orders(
    trade_proposals: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert proposed trades into non-submitting Alpaca shadow orders."""

    config = config or {}
    planner_cfg = config.get("trade_planner") or {}
    order_cfg = planner_cfg.get("shadow_order") or {}
    generated_at = _now_iso()
    broker = str(order_cfg.get("broker", "alpaca"))
    order_type = str(order_cfg.get("type", "limit"))
    time_in_force = str(order_cfg.get("time_in_force", "day"))
    position_intent = str(order_cfg.get("position_intent", "sell_to_open"))

    orders = []
    for proposal in trade_proposals.get("proposals", []):
        if proposal.get("decision") != "PROPOSED":
            continue
        option = proposal.get("option") or {}
        limit_price = proposal.get("limit_price")
        order_id = proposal["proposal_id"]
        orders.append(
            {
                "shadow_order_id": order_id,
                "proposal_id": proposal["proposal_id"],
                "created_at": generated_at,
                "dry_run_only": True,
                "broker": broker,
                "strategy": proposal.get("strategy"),
                "ticker": proposal.get("ticker"),
                "estimated_assignment_cash": proposal.get(
                    "assignment_cash_required"
                ),
                "estimated_premium_credit": proposal.get(
                    "estimated_premium_credit"
                ),
                "payload": {
                    "symbol": option.get("symbol"),
                    "qty": str(proposal.get("quantity", 1)),
                    "side": "sell",
                    "type": order_type,
                    "time_in_force": time_in_force,
                    "limit_price": _money_string(limit_price),
                    "position_intent": position_intent,
                    "client_order_id": order_id,
                },
                "warnings": ["dry_run_only_not_submitted"],
            }
        )

    return {
        "scan_date": trade_proposals.get("scan_date"),
        "generated_at": generated_at,
        "source_proposals_generated_at": trade_proposals.get("generated_at"),
        "dry_run_only": True,
        "broker": broker,
        "order_count": len(orders),
        "orders": orders,
    }


def _candidate_proposal(
    *,
    row: dict[str, Any],
    scan_date: str,
    generated_at: str,
    allocation: dict[str, float | bool | None],
    quantity: int,
) -> dict[str, Any]:
    candidate = row["candidate"]
    option = candidate.get("option") or {}
    assignment_cash = _candidate_assignment_cash(candidate, option) * quantity
    limit_price = _limit_price(option)
    premium = limit_price * 100.0 * quantity
    current_reserved = float(allocation["running_reserved_assignment_cash"] or 0.0)
    projected_reserved = current_reserved + assignment_cash
    cash = float(allocation["cash"] or 0.0)
    projected_cash_after_reserve = cash - projected_reserved
    allocation_reasons = _allocation_reasons(
        allocation=allocation,
        projected_reserved=projected_reserved,
        projected_cash_after_reserve=projected_cash_after_reserve,
    )
    status = str(row.get("status") or "")
    manual_review = bool(row.get("manual_review_required"))
    decision, decision_reasons = _proposal_decision(
        row=row,
        status=status,
        manual_review=manual_review,
        candidate=candidate,
        limit_price=limit_price,
        allocation_reasons=allocation_reasons,
    )

    expiration = str(option.get("expiration") or "")
    strike = _number(option.get("strike")) or 0.0
    proposal_id = _proposal_id(scan_date, row.get("ticker"), expiration, strike)
    return {
        "proposal_id": proposal_id,
        "created_at": generated_at,
        "scan_date": scan_date,
        "ticker": row.get("ticker"),
        "strategy": "cash_secured_put",
        "decision": decision,
        "decision_reasons": decision_reasons,
        "source_status": status,
        "source_status_reason": row.get("status_reason"),
        "requires_manual_review": manual_review or decision == "WATCH",
        "quantity": quantity,
        "assignment_cash_required": round(assignment_cash, 2),
        "estimated_premium_credit": round(premium, 2),
        "limit_price": round(limit_price, 2),
        "option": {
            "symbol": option.get("symbol"),
            "expiration": expiration,
            "strike": strike,
            "put_call": "put",
            "dte": option.get("dte"),
            "bid": option.get("bid"),
            "ask": option.get("ask"),
            "last": option.get("last"),
            "mid": option.get("mid"),
            "executable_mid": option.get("executable_mid"),
            "delta": candidate.get("delta"),
            "open_interest": option.get("open_interest"),
            "volume": option.get("volume"),
            "spread_pct_of_mid": option.get("spread_pct_of_mid"),
        },
        "support": _support_summary(row),
        "gates": {
            "fundamental": _gate_summary(row.get("fundamental_gate")),
            "earnings": _gate_summary(row.get("earnings_gate")),
            "portfolio": _gate_summary(row.get("portfolio_gate")),
        },
        "csp": {
            "delta_bucket": candidate.get("delta_bucket"),
            "auto_trade": candidate.get("auto_trade"),
            "weekly_return_on_strike_pct": candidate.get(
                "weekly_return_on_strike_pct"
            ),
            "candidate_reasons": candidate.get("reasons") or [],
            "diagnostics": candidate.get("diagnostics") or {},
        },
        "allocation": {
            "cash": round(cash, 2),
            "equity": round(float(allocation["equity"] or 0.0), 2),
            "reserved_assignment_cash_before": round(current_reserved, 2),
            "projected_reserved_assignment_cash": round(projected_reserved, 2),
            "max_assignment_cash": _round_or_none(allocation["max_assignment_cash"]),
            "min_cash_buffer": round(float(allocation["min_cash_buffer"] or 0.0), 2),
            "projected_cash_after_reserve": round(projected_cash_after_reserve, 2),
        },
        "shadow_order_ref": proposal_id if decision == "PROPOSED" else None,
    }


def _proposal_decision(
    *,
    row: dict[str, Any],
    status: str,
    manual_review: bool,
    candidate: dict[str, Any],
    limit_price: float,
    allocation_reasons: list[str],
) -> tuple[str, list[str]]:
    gate_decision = _gate_decision(row)
    if gate_decision:
        return gate_decision
    if status == "REJECT":
        return "REJECTED_BY_GATE", ["scan_status_reject"]
    if status != "AUTO_TRADE":
        return "WATCH", [f"scan_status_{status.lower() or 'unknown'}"]
    if manual_review:
        return "WATCH", ["manual_review_required"]
    if not candidate.get("auto_trade"):
        return "WATCH", ["candidate_not_auto_trade"]
    if limit_price <= 0:
        return "WATCH", ["missing_option_price"]
    if allocation_reasons:
        return "REJECTED_BY_ALLOCATION", allocation_reasons
    return "PROPOSED", ["all_auto_trade_gates_passed"]


def _allocation_reasons(
    *,
    allocation: dict[str, float | bool | None],
    projected_reserved: float,
    projected_cash_after_reserve: float,
) -> list[str]:
    reasons = []
    max_assignment_cash = allocation.get("max_assignment_cash")
    if (
        max_assignment_cash is not None
        and projected_reserved - float(max_assignment_cash) > MONEY_EPSILON
    ):
        reasons.append(
            "max_assignment_cash_exceeded:"
            f"{projected_reserved:.2f}>{float(max_assignment_cash):.2f}"
        )
    if (
        allocation.get("no_margin_assignment")
        and float(allocation["min_cash_buffer"] or 0.0)
        - projected_cash_after_reserve
        > MONEY_EPSILON
    ):
        reasons.append(
            "cash_buffer_after_assignment_below_min:"
            f"{projected_cash_after_reserve:.2f}<"
            f"{float(allocation['min_cash_buffer'] or 0.0):.2f}"
        )
    return reasons


def _initial_allocation(
    scan: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, float | bool | None]:
    account_cfg = config.get("account") or {}
    risk_cfg = config.get("risk") or {}
    portfolio = scan.get("portfolio") or {}

    configured_equity = _number(account_cfg.get("starting_equity"))
    if configured_equity is None:
        configured_equity = DEFAULT_STARTING_EQUITY
    portfolio_equity = _number(portfolio.get("equity"))
    portfolio_cash = _number(portfolio.get("cash"))
    portfolio_reserved = _number(portfolio.get("reserved_assignment_cash"))
    equity = portfolio_equity if portfolio_equity is not None else configured_equity
    cash = portfolio_cash if portfolio_cash is not None else equity
    reserved = portfolio_reserved if portfolio_reserved is not None else 0.0

    max_assignment_pct = float(risk_cfg.get("max_assignment_cash_pct", 1.0))
    min_cash_buffer_pct = float(risk_cfg.get("min_cash_buffer_pct", 0.0))
    return {
        "cash": cash,
        "equity": equity,
        "starting_reserved_assignment_cash": reserved,
        "running_reserved_assignment_cash": reserved,
        "max_assignment_cash": equity * max_assignment_pct if equity > 0 else None,
        "min_cash_buffer": equity * min_cash_buffer_pct if equity > 0 else 0.0,
        "no_margin_assignment": bool(risk_cfg.get("no_margin_assignment", True)),
    }


def _allocation_summary(
    allocation: dict[str, float | bool | None],
) -> dict[str, float | bool | None]:
    final_reserved = float(allocation["running_reserved_assignment_cash"] or 0.0)
    cash = float(allocation["cash"] or 0.0)
    return {
        "cash": round(cash, 2),
        "equity": round(float(allocation["equity"] or 0.0), 2),
        "starting_reserved_assignment_cash": round(
            float(allocation["starting_reserved_assignment_cash"] or 0.0), 2
        ),
        "final_reserved_assignment_cash": round(final_reserved, 2),
        "max_assignment_cash": _round_or_none(allocation["max_assignment_cash"]),
        "min_cash_buffer": round(float(allocation["min_cash_buffer"] or 0.0), 2),
        "cash_after_final_reserve": round(cash - final_reserved, 2),
        "no_margin_assignment": allocation["no_margin_assignment"],
    }


def _candidate_assignment_cash(
    candidate: dict[str, Any],
    option: dict[str, Any],
) -> float:
    explicit = _number(candidate.get("assignment_cash_required"))
    if explicit is not None:
        return explicit
    return float(_number(option.get("strike")) or 0.0) * 100.0


def _limit_price(option: dict[str, Any]) -> float:
    return float(
        _number(option.get("executable_mid"))
        or _number(option.get("mid"))
        or _number(option.get("last"))
        or 0.0
    )


def _proposal_id(
    scan_date: str,
    ticker: Any,
    expiration: str,
    strike: float,
) -> str:
    clean_scan_date = scan_date.replace("-", "")[2:]
    clean_expiration = expiration.replace("-", "")[2:]
    clean_ticker = _token(str(ticker or "UNKNOWN"), max_length=6)
    strike_cents = int(round(strike * 100))
    raw = f"wheel-csp-{scan_date}-{ticker}-{expiration}-{strike_cents}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return (
        f"whcsp-{clean_scan_date}-{clean_ticker}-"
        f"{clean_expiration}-{strike_cents}-{digest}"
    )


def _support_summary(row: dict[str, Any]) -> dict[str, Any]:
    support = row.get("selected_support") or {}
    return {
        "score": row.get("support_score"),
        "method": support.get("method"),
        "bottom": support.get("bottom"),
        "top": support.get("top"),
        "center": support.get("center"),
        "touches": support.get("touches"),
        "last_touch_date": support.get("last_touch_date"),
        "reasons": support.get("reasons") or row.get("reasons") or [],
    }


def _gate_summary(gate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not gate:
        return None
    return {
        "status": gate.get("status"),
        "reasons": gate.get("reasons") or [],
        "warnings": gate.get("warnings") or [],
    }


def _audit_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "status_reason": row.get("status_reason"),
        "support_score": row.get("support_score"),
        "rejection_summary": row.get("rejection_summary") or {},
        "error": row.get("error"),
    }


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return round(number, 2)


def _money_string(value: Any) -> str:
    return f"{float(value or 0.0):.2f}"


def _contract_quantity(config: dict[str, Any]) -> int:
    raw = (config.get("trade_planner") or {}).get("default_contract_quantity", 1)
    quantity = max(int(raw), 1)
    if quantity != 1:
        raise ValueError(
            "trade_planner only supports one-contract CSP proposals until "
            "position sizing is implemented"
        )
    return quantity


def _planner_order_key(row: dict[str, Any]) -> tuple[int, float, float, str]:
    status_rank = {
        "AUTO_TRADE": 0,
        "WATCH": 1,
        "REJECT": 2,
        "ERROR": 3,
    }
    candidate = row.get("candidate") or {}
    return (
        status_rank.get(str(row.get("status") or ""), 99),
        -float(_number(row.get("support_score")) or 0.0),
        -float(_number(candidate.get("weekly_return_on_strike_pct")) or 0.0),
        str(row.get("ticker") or ""),
    )


def _gate_decision(row: dict[str, Any]) -> tuple[str, list[str]] | None:
    reasons = []
    for name in ("fundamental_gate", "earnings_gate", "portfolio_gate"):
        gate = row.get(name) or {}
        status = str(gate.get("status") or "").upper()
        if not status or status == "PASS":
            continue
        reason = f"{name}_{status.lower()}"
        if status in {"REJECT", "FAIL", "ERROR"}:
            reasons.append(reason)
        else:
            return "WATCH", [reason]
    if reasons:
        return "REJECTED_BY_GATE", reasons
    return None


def _token(value: str, *, max_length: int) -> str:
    token = "".join(ch for ch in value.upper() if ch.isalnum())
    return (token or "UNK")[:max_length]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
