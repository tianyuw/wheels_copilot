from __future__ import annotations

from typing import Any

from .models import BrokerOrder, CspCandidate, GateResult, PortfolioSnapshot


def evaluate_portfolio_risk(
    ticker: str,
    candidate: CspCandidate | None,
    portfolio: PortfolioSnapshot | None,
    config: dict[str, Any],
    *,
    required: bool = False,
    portfolio_error: str | None = None,
) -> tuple[GateResult | None, dict[str, Any] | None]:
    if candidate is None:
        return None, None
    ticker = ticker.upper()
    if portfolio is None:
        if not required:
            return None, None
        warning = portfolio_error or "portfolio_snapshot_unavailable"
        return (
            GateResult(
                status="WARN",
                reasons=["portfolio_review_required"],
                warnings=[warning],
            ),
            {"portfolio_error": portfolio_error},
        )

    reasons: list[str] = []
    warnings: list[str] = []
    risk_cfg = config.get("risk", {})
    portfolio_cfg = config.get("portfolio", {})
    account = portfolio.account
    equity = account.equity or 0.0
    cash = account.cash or 0.0

    if account.status and account.status.upper() != "ACTIVE":
        reasons.append(f"account_status_{account.status}")
    if account.trading_blocked:
        reasons.append("trading_blocked")
    if account.account_blocked:
        reasons.append("account_blocked")
    if equity <= 0:
        reasons.append("account_equity_unavailable")
    if cash <= 0:
        reasons.append("account_cash_unavailable")

    existing_stock_qty = _stock_qty(portfolio, ticker)
    existing_short_put_cash = _short_put_assignment_cash(portfolio, ticker)
    existing_open_sell_put_cash = _open_sell_put_assignment_cash(portfolio, ticker)
    if existing_stock_qty < 0:
        reasons.append("existing_short_underlying_position")
    elif existing_stock_qty >= 100:
        reasons.append("covered_call_workflow_required_existing_100_shares")
    elif existing_stock_qty > 0:
        warnings.append(f"partial_underlying_position_qty:{existing_stock_qty:g}")
    if existing_short_put_cash > 0:
        reasons.append("existing_short_put_position")
    if existing_open_sell_put_cash > 0:
        reasons.append("duplicate_open_short_put_order")

    active_tickers = _active_tickers(portfolio)
    max_active = _int_or_none(portfolio_cfg.get("max_active_tickers"))
    projected_active_count = len(active_tickers | {ticker})
    if max_active is not None and projected_active_count > max_active:
        reasons.append(f"max_active_tickers_exceeded:{projected_active_count}>{max_active}")

    assignment_cash_required = candidate.assignment_cash_required
    reserved_assignment_cash = _reserved_assignment_cash(portfolio)
    projected_reserved_assignment_cash = reserved_assignment_cash + assignment_cash_required

    max_assignment_cash = (
        equity * float(risk_cfg.get("max_assignment_cash_pct", 1.0))
        if equity > 0
        else None
    )
    if (
        max_assignment_cash is not None
        and projected_reserved_assignment_cash > max_assignment_cash
    ):
        reasons.append(
            "max_assignment_cash_exceeded:"
            f"{projected_reserved_assignment_cash:.2f}>{max_assignment_cash:.2f}"
        )

    max_single_ticker_cash = _max_single_ticker_assignment_cash(config, equity)
    if (
        max_single_ticker_cash is not None
        and assignment_cash_required > max_single_ticker_cash
    ):
        reasons.append(
            "single_ticker_assignment_exceeded:"
            f"{assignment_cash_required:.2f}>{max_single_ticker_cash:.2f}"
        )

    min_cash_buffer = (
        equity * float(risk_cfg.get("min_cash_buffer_pct", 0.0))
        if equity > 0
        else 0.0
    )
    projected_cash_after_reserve = cash - projected_reserved_assignment_cash
    if (
        risk_cfg.get("no_margin_assignment", True)
        and projected_cash_after_reserve < min_cash_buffer
    ):
        reasons.append(
            "cash_buffer_after_assignment_below_min:"
            f"{projected_cash_after_reserve:.2f}<{min_cash_buffer:.2f}"
        )

    diagnostics = {
        "assignment_cash_required": round(assignment_cash_required, 2),
        "reserved_assignment_cash": round(reserved_assignment_cash, 2),
        "projected_reserved_assignment_cash": round(projected_reserved_assignment_cash, 2),
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "min_cash_buffer": round(min_cash_buffer, 2),
        "projected_cash_after_reserve": round(projected_cash_after_reserve, 2),
        "active_ticker_count": len(active_tickers),
        "projected_active_ticker_count": projected_active_count,
        "active_tickers": sorted(active_tickers),
        "existing_underlying_shares": existing_stock_qty,
        "existing_short_put_assignment_cash": round(existing_short_put_cash, 2),
        "existing_open_sell_put_assignment_cash": round(existing_open_sell_put_cash, 2),
    }

    if reasons:
        return GateResult(status="REJECT", reasons=reasons, warnings=warnings), diagnostics
    if warnings:
        return (
            GateResult(
                status="WARN",
                reasons=["portfolio_review_required"],
                warnings=warnings,
            ),
            diagnostics,
        )
    return GateResult(status="PASS", reasons=["portfolio_risk_passed"], warnings=[]), diagnostics


def summarize_portfolio_snapshot(
    portfolio: PortfolioSnapshot | None,
    error: str | None = None,
) -> dict[str, Any] | None:
    if portfolio is None:
        if error is None:
            return None
        return {"source": "alpaca_paper", "error": error}
    return {
        "source": portfolio.source,
        "fetched_at": portfolio.fetched_at,
        "account_status": portfolio.account.status,
        "equity": portfolio.account.equity,
        "cash": portfolio.account.cash,
        "buying_power": portfolio.account.buying_power,
        "options_trading_level": portfolio.account.options_trading_level,
        "position_count": len(portfolio.positions),
        "open_order_count": len(portfolio.open_orders),
        "reserved_assignment_cash": round(_reserved_assignment_cash(portfolio), 2),
        "active_tickers": sorted(_active_tickers(portfolio)),
    }


def _reserved_assignment_cash(portfolio: PortfolioSnapshot) -> float:
    return sum(position.assignment_cash_required for position in portfolio.positions) + sum(
        _order_assignment_cash(portfolio, order) for order in portfolio.open_orders
    )


def _stock_qty(portfolio: PortfolioSnapshot, ticker: str) -> float:
    total = 0.0
    for position in portfolio.positions:
        if position.is_option:
            continue
        if position.symbol.upper() == ticker:
            total += position.qty
    return total


def _short_put_assignment_cash(portfolio: PortfolioSnapshot, ticker: str) -> float:
    return sum(
        position.assignment_cash_required
        for position in portfolio.positions
        if position.active_underlying == ticker and position.is_short_put
    )


def _open_sell_put_assignment_cash(portfolio: PortfolioSnapshot, ticker: str) -> float:
    return sum(
        _order_assignment_cash(portfolio, order)
        for order in portfolio.open_orders
        if order.active_underlying == ticker and order.is_sell_put
    )


def _order_assignment_cash(portfolio: PortfolioSnapshot, order: BrokerOrder) -> float:
    if not order.is_sell_put:
        return 0.0
    if _matches_long_put_position(portfolio, order):
        return 0.0
    return order.assignment_cash_required


def _matches_long_put_position(portfolio: PortfolioSnapshot, order: BrokerOrder) -> bool:
    return any(
        position.symbol == order.symbol
        and position.option_type == "put"
        and position.qty > 0
        for position in portfolio.positions
    )


def _active_tickers(portfolio: PortfolioSnapshot) -> set[str]:
    tickers: set[str] = set()
    for position in portfolio.positions:
        if position.qty != 0:
            tickers.add(position.active_underlying)
    for order in portfolio.open_orders:
        if order.qty != 0:
            tickers.add(order.active_underlying)
    return tickers


def _max_single_ticker_assignment_cash(
    config: dict[str, Any],
    equity: float,
) -> float | None:
    risk = config.get("risk", {})
    limits = []
    if risk.get("max_single_ticker_assignment_dollars") is not None:
        limits.append(float(risk["max_single_ticker_assignment_dollars"]))
    if risk.get("max_single_ticker_assignment_pct") is not None and equity > 0:
        limits.append(equity * float(risk["max_single_ticker_assignment_pct"]))
    return min(limits) if limits else None


def _int_or_none(value) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
