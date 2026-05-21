from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from .models import BrokerOrder, BrokerPosition, PortfolioSnapshot


def build_wheel_lifecycle_snapshot(
    portfolio: PortfolioSnapshot,
    config: dict[str, Any] | None = None,
    *,
    ledger_positions: Iterable[Mapping[str, Any]] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Classify broker positions into wheel states.

    Alpaca remains the source of truth for live exposure. OMS rows are used only
    to recover premium/cost-basis context for positions that Wheels Copilot
    opened earlier.
    """

    as_of = as_of or date.today()
    ledger_rows = list(ledger_positions or [])
    tickers = sorted(_active_tickers(portfolio, ledger_rows))
    positions = [
        _ticker_lifecycle(ticker, portfolio, ledger_rows)
        for ticker in tickers
    ]
    summary = Counter(row["state"] for row in positions)
    return {
        "as_of": as_of.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": portfolio.source,
        "account": {
            "status": portfolio.account.status,
            "equity": portfolio.account.equity,
            "cash": portfolio.account.cash,
        },
        "summary": dict(sorted(summary.items())),
        "position_count": len(positions),
        "positions": positions,
    }


def _ticker_lifecycle(
    ticker: str,
    portfolio: PortfolioSnapshot,
    ledger_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    stock_positions = [
        position
        for position in portfolio.positions
        if position.is_long_equity and position.active_underlying == ticker
    ]
    short_put_positions = [
        position
        for position in portfolio.positions
        if position.is_short_put and position.active_underlying == ticker
    ]
    short_call_positions = [
        position
        for position in portfolio.positions
        if position.is_short_call and position.active_underlying == ticker
    ]
    open_sell_put_orders = [
        order
        for order in portfolio.open_orders
        if order.is_sell_put and order.active_underlying == ticker
    ]
    open_sell_call_orders = [
        order
        for order in portfolio.open_orders
        if order.is_sell_call and order.active_underlying == ticker
    ]
    long_shares = sum(position.qty for position in stock_positions)
    share_cost_basis = _share_cost_basis(stock_positions)
    ledger_context = _ledger_context(ticker, ledger_rows)
    adjusted_cost_basis = _adjusted_cost_basis(
        share_cost_basis,
        ledger_context,
    )
    short_call_contracts = _abs_contracts(short_call_positions)
    open_sell_call_contracts = _order_contracts(open_sell_call_orders)
    ledger_open_short_call_contracts = _ledger_open_contracts(
        ticker,
        ledger_rows,
        "call",
    )
    effective_short_call_contracts = max(
        short_call_contracts,
        ledger_open_short_call_contracts,
    )
    covered_contracts = (
        effective_short_call_contracts + open_sell_call_contracts
    )
    available_shares_for_cc = max(0.0, long_shares - covered_contracts * 100.0)
    state = _wheel_state(
        long_shares=long_shares,
        short_put_contracts=_abs_contracts(short_put_positions),
        open_sell_put_contracts=_order_contracts(open_sell_put_orders),
        short_call_contracts=effective_short_call_contracts,
        open_sell_call_contracts=open_sell_call_contracts,
    )
    return {
        "ticker": ticker,
        "state": state,
        "long_shares": round(long_shares, 4),
        "available_shares_for_cc": round(available_shares_for_cc, 4),
        "covered_call_eligible": long_shares >= 100 and available_shares_for_cc >= 100,
        "share_cost_basis": _round_or_none(share_cost_basis),
        "adjusted_cost_basis": _round_or_none(adjusted_cost_basis),
        "premium_context": ledger_context,
        "open_short_put_contracts": _round_or_none(_abs_contracts(short_put_positions)),
        "open_sell_put_order_contracts": _round_or_none(
            _order_contracts(open_sell_put_orders)
        ),
        "open_short_call_contracts": _round_or_none(short_call_contracts),
        "ledger_open_short_call_contracts": _round_or_none(
            ledger_open_short_call_contracts
        ),
        "open_sell_call_order_contracts": _round_or_none(open_sell_call_contracts),
        "stock_positions": [_position_summary(position) for position in stock_positions],
        "short_put_positions": [
            _option_position_summary(position) for position in short_put_positions
        ],
        "short_call_positions": [
            _option_position_summary(position) for position in short_call_positions
        ],
        "open_sell_put_orders": [_order_summary(order) for order in open_sell_put_orders],
        "open_sell_call_orders": [_order_summary(order) for order in open_sell_call_orders],
        "reasons": _state_reasons(
            state,
            long_shares=long_shares,
            adjusted_cost_basis=adjusted_cost_basis,
            available_shares_for_cc=available_shares_for_cc,
            ledger_open_short_call_contracts=ledger_open_short_call_contracts,
            ledger_context=ledger_context,
        ),
    }


def _active_tickers(
    portfolio: PortfolioSnapshot,
    ledger_rows: list[Mapping[str, Any]],
) -> set[str]:
    tickers: set[str] = set()
    for position in portfolio.positions:
        if position.qty:
            tickers.add(position.active_underlying)
    for order in portfolio.open_orders:
        tickers.add(order.active_underlying)
    for row in ledger_rows:
        ticker = _str_or_none(_row_get(row, "ticker"))
        if ticker:
            tickers.add(ticker.upper())
    return tickers


def _wheel_state(
    *,
    long_shares: float,
    short_put_contracts: float,
    open_sell_put_contracts: float,
    short_call_contracts: float,
    open_sell_call_contracts: float,
) -> str:
    if long_shares >= 100 and (short_call_contracts > 0 or open_sell_call_contracts > 0):
        return "CC_OPEN"
    if long_shares >= 100:
        return "ASSIGNED"
    if short_put_contracts > 0 or open_sell_put_contracts > 0:
        return "CSP_OPEN"
    return "CASH"


def _share_cost_basis(stock_positions: list[BrokerPosition]) -> float | None:
    total_shares = sum(abs(position.qty) for position in stock_positions)
    if total_shares <= 0:
        return None
    per_share_values = [
        _position_cost_basis_per_share(position)
        for position in stock_positions
        if _position_cost_basis_per_share(position) is not None
    ]
    if not per_share_values:
        return None
    weighted = 0.0
    weighted_shares = 0.0
    for position in stock_positions:
        per_share = _position_cost_basis_per_share(position)
        if per_share is None:
            continue
        shares = abs(position.qty)
        weighted += per_share * shares
        weighted_shares += shares
    return weighted / weighted_shares if weighted_shares > 0 else None


def _position_cost_basis_per_share(position: BrokerPosition) -> float | None:
    if position.cost_basis is None or position.qty == 0:
        return None
    return abs(float(position.cost_basis)) / abs(float(position.qty))


def _ledger_context(
    ticker: str,
    ledger_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    csp_credit = 0.0
    cc_credit = 0.0
    called_away_cc_credit = 0.0
    unattributed_csp_credit = 0.0
    assignment_strikes = []
    csp_contracts = 0.0
    cc_contracts = 0.0
    for row in ledger_rows:
        row_ticker = _str_or_none(_row_get(row, "ticker"))
        if not row_ticker or row_ticker.upper() != ticker:
            continue
        option_type = (_str_or_none(_row_get(row, "option_type")) or "").lower()
        entry_price = _num(_row_get(row, "entry_price"))
        qty = abs(_num(_row_get(row, "qty")) or 0.0)
        if entry_price is None or qty <= 0:
            continue
        status = (_str_or_none(_row_get(row, "status")) or "").upper()
        if option_type == "put":
            if status == "ASSIGNED":
                csp_credit += entry_price * qty * 100.0
                csp_contracts += qty
                strike = _num(_row_get(row, "strike"))
                if strike is not None:
                    assignment_strikes.append(strike)
            else:
                unattributed_csp_credit += entry_price * qty * 100.0
        elif option_type == "call":
            if status == "CALLED_AWAY":
                called_away_cc_credit += entry_price * qty * 100.0
                continue
            cc_credit += entry_price * qty * 100.0
            cc_contracts += qty
    share_denominator = max(csp_contracts * 100.0, 100.0) if csp_contracts else 100.0
    call_share_denominator = max(cc_contracts * 100.0, 100.0) if cc_contracts else 100.0
    return {
        "csp_credit_total": round(csp_credit, 2),
        "csp_credit_per_share": round(csp_credit / share_denominator, 4)
        if csp_credit
        else 0.0,
        "unattributed_csp_credit_total": round(unattributed_csp_credit, 2),
        "csp_credit_attribution": (
            "assigned_ledger_rows"
            if csp_credit
            else "unavailable"
            if unattributed_csp_credit
            else "none"
        ),
        "cc_credit_total": round(cc_credit, 2),
        "called_away_cc_credit_total": round(called_away_cc_credit, 2),
        "cc_credit_per_share": round(cc_credit / call_share_denominator, 4)
        if cc_credit
        else 0.0,
        "csp_contracts": round(csp_contracts, 4),
        "cc_contracts": round(cc_contracts, 4),
        "last_assignment_strike": assignment_strikes[-1] if assignment_strikes else None,
    }


def _adjusted_cost_basis(
    share_cost_basis: float | None,
    ledger_context: dict[str, Any],
) -> float | None:
    basis = share_cost_basis
    if basis is None and ledger_context.get("last_assignment_strike") is not None:
        basis = float(ledger_context["last_assignment_strike"])
    if basis is None:
        return None
    return (
        basis
        - float(ledger_context.get("csp_credit_per_share") or 0.0)
        - float(ledger_context.get("cc_credit_per_share") or 0.0)
    )


def _ledger_open_contracts(
    ticker: str,
    ledger_rows: list[Mapping[str, Any]],
    option_type: str,
) -> float:
    total = 0.0
    for row in ledger_rows:
        row_ticker = _str_or_none(_row_get(row, "ticker"))
        if not row_ticker or row_ticker.upper() != ticker:
            continue
        row_option_type = (_str_or_none(_row_get(row, "option_type")) or "").lower()
        status = (_str_or_none(_row_get(row, "status")) or "").upper()
        if row_option_type == option_type and status == "OPEN":
            total += abs(_num(_row_get(row, "qty")) or 0.0)
    return total


def _abs_contracts(positions: list[BrokerPosition]) -> float:
    return sum(abs(position.qty) for position in positions)


def _order_contracts(orders: list[BrokerOrder]) -> float:
    return sum(abs(order.qty) for order in orders)


def _state_reasons(
    state: str,
    *,
    long_shares: float,
    adjusted_cost_basis: float | None,
    available_shares_for_cc: float,
    ledger_open_short_call_contracts: float,
    ledger_context: dict[str, Any],
) -> list[str]:
    if state == "ASSIGNED":
        reasons = [f"long_stock_detected:{long_shares:g}_shares"]
        if available_shares_for_cc >= 100:
            reasons.append("covered_call_check_required")
        if adjusted_cost_basis is None:
            reasons.append("adjusted_cost_basis_missing")
        if ledger_context.get("csp_credit_attribution") == "unavailable":
            reasons.append("csp_credit_attribution_unavailable")
        return reasons
    if state == "CC_OPEN":
        reasons = ["long_stock_already_covered_by_call"]
        if ledger_open_short_call_contracts > 0:
            reasons.append("ledger_open_short_call_reconciliation_required")
        if available_shares_for_cc >= 100:
            reasons.append("additional_uncovered_share_lot_detected")
        return reasons
    if state == "CSP_OPEN":
        return ["short_put_exposure_detected"]
    return ["no_active_wheel_exposure"]


def _position_summary(position: BrokerPosition) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "qty": position.qty,
        "asset_class": position.asset_class,
        "market_value": position.market_value,
        "cost_basis": position.cost_basis,
    }


def _option_position_summary(position: BrokerPosition) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "qty": position.qty,
        "option_type": position.option_type,
        "expiration": position.expiration.isoformat() if position.expiration else None,
        "strike": position.strike,
        "market_value": position.market_value,
        "cost_basis": position.cost_basis,
    }


def _order_summary(order: BrokerOrder) -> dict[str, Any]:
    return {
        "id": order.id,
        "symbol": order.symbol,
        "side": order.side,
        "qty": order.qty,
        "status": order.status,
        "position_intent": order.position_intent,
        "expiration": order.expiration.isoformat() if order.expiration else None,
        "strike": order.strike,
        "limit_price": order.limit_price,
    }


def _row_get(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: Any) -> float | None:
    number = _num(value)
    return round(number, 4) if number is not None else None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
