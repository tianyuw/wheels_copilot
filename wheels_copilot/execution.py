from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .alpaca import (
    AlpacaConfigError,
    AlpacaRequestError,
    AlpacaTradingClient,
    DEFAULT_PAPER_BASE_URL,
    account_identity_reasons,
    parse_occ_option_symbol,
)
from .models import BrokerOrder, CspCandidate, OptionQuote, PortfolioSnapshot, SupportZone
from .oms import OrderLedger, oms_enabled
from .portfolio_risk import evaluate_portfolio_risk


DEFAULT_MAX_ORDERS_PER_RUN = 3
DEFAULT_NO_OPEN_MINUTES_BEFORE_CLOSE = 30
DEFAULT_MAX_VALIDATED_ORDER_AGE_SECONDS = 120
MONEY_EPSILON = 0.005
BROKER_PAYLOAD_KEYS = {
    "symbol",
    "qty",
    "side",
    "type",
    "time_in_force",
    "limit_price",
    "position_intent",
    "client_order_id",
}
BROKER_SUBMITTED_STATUSES = {
    "accepted",
    "new",
    "pending_new",
    "held",
    "partially_filled",
    "filled",
}


def execute_validated_shadow_orders(
    validated_shadow_orders: dict[str, Any],
    config: dict[str, Any],
    *,
    client: AlpacaTradingClient | None = None,
    ledger: OrderLedger | None = None,
    previous_execution_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit validated CSP shadow orders to the Alpaca paper account.

    The function is intentionally narrow: it only submits single-leg,
    sell-to-open cash-secured puts that already passed the fresh OPRA quote
    validator. It still re-checks paper-only config, market clock, portfolio
    risk, and idempotency immediately before each broker POST.
    """

    generated_at = _now_iso()
    orders = validated_shadow_orders.get("orders") or []
    execution_cfg = config.get("execution") or {}
    max_orders = int(execution_cfg.get("max_orders_per_run") or DEFAULT_MAX_ORDERS_PER_RUN)
    previously_submitted = _submitted_client_order_ids(previous_execution_results)
    owns_ledger = ledger is None and oms_enabled(config)
    if owns_ledger:
        ledger = OrderLedger.from_config(config)

    try:
        config_reasons = _execution_config_reasons(config)
        artifact_reasons = _validation_artifact_reasons(
            validated_shadow_orders,
            generated_at=generated_at,
            config=config,
        )
        client_error = None
        if client is None and not config_reasons:
            try:
                client = AlpacaTradingClient.from_config(config)
            except (AlpacaConfigError, AlpacaRequestError) as exc:
                client_error = f"{type(exc).__name__}: {exc}"

        clock = None
        clock_reasons: list[str] = []
        if client is None:
            if client_error:
                clock_reasons.append("alpaca_trading_client_unavailable")
        elif not config_reasons:
            try:
                clock = client.fetch_clock()
                clock_reasons = _clock_blocking_reasons(clock, config)
            except (AlpacaConfigError, AlpacaRequestError) as exc:
                client_error = f"{type(exc).__name__}: {exc}"
                clock_reasons.append("market_clock_unavailable")

        submitted_count = 0
        in_run_open_orders: list[BrokerOrder] = []
        results = []
        for order in orders:
            global_reasons = list(config_reasons) + list(artifact_reasons) + list(clock_reasons)
            if submitted_count >= max_orders:
                global_reasons.append("max_orders_per_run_reached")
            result = _execute_one_order(
                order,
                config,
                client=client,
                ledger=ledger,
                global_blocking_reasons=global_reasons,
                previously_submitted=previously_submitted,
                in_run_open_orders=in_run_open_orders,
            )
            if result["status"] == "SUBMITTED":
                submitted_count += 1
                previously_submitted.add(result["client_order_id"])
                synthetic_order = _broker_order_from_payload(order.get("validated_payload") or {})
                if synthetic_order:
                    in_run_open_orders.append(synthetic_order)
            results.append(result)

        summary = Counter(result["status"] for result in results)
        return {
            "scan_date": validated_shadow_orders.get("scan_date"),
            "generated_at": generated_at,
            "source_validated_shadow_orders_generated_at": validated_shadow_orders.get(
                "generated_at"
            ),
            "broker": "alpaca",
            "account_type": "paper",
            "executor": {
                "version": 1,
                "mode": "paper",
                "strategy": "cash_secured_put",
                "max_orders_per_run": max_orders,
                "max_validated_order_age_seconds": _max_validated_order_age_seconds(config),
                "no_open_minutes_before_close": int(
                    execution_cfg.get("no_open_minutes_before_close")
                    or DEFAULT_NO_OPEN_MINUTES_BEFORE_CLOSE
                ),
                "paper_only_guard": True,
                "portfolio_risk_gate": True,
                "market_clock_gate": True,
                "oms_enabled": ledger is not None,
            },
            "clock": _clock_summary(clock),
            "client_error": client_error,
            "order_count": len(results),
            "submitted_count": submitted_count,
            "summary": dict(sorted(summary.items())),
            "orders": results,
        }
    finally:
        if owns_ledger and ledger is not None:
            ledger.close()


def _execute_one_order(
    order: dict[str, Any],
    config: dict[str, Any],
    *,
    client: AlpacaTradingClient | None,
    ledger: OrderLedger | None,
    global_blocking_reasons: list[str],
    previously_submitted: set[str],
    in_run_open_orders: list[BrokerOrder],
) -> dict[str, Any]:
    payload = order.get("validated_payload") or {}
    client_order_id = str(payload.get("client_order_id") or order.get("shadow_order_id") or "")
    blocking_reasons = list(global_blocking_reasons)

    if not order.get("submit_ready"):
        blocking_reasons.append("validated_shadow_order_not_submit_ready")
        blocking_reasons.extend(
            f"validation:{reason}" for reason in (order.get("blocking_reasons") or [])
        )

    blocking_reasons.extend(_payload_blocking_reasons(payload))
    if client_order_id in previously_submitted:
        blocking_reasons.append("duplicate_client_order_id_previous_execution")

    portfolio_snapshot = None
    portfolio_error = None
    portfolio_risk = None
    if not blocking_reasons and client is not None:
        try:
            portfolio_snapshot = client.fetch_portfolio_snapshot()
            portfolio_snapshot = _with_in_run_open_orders(
                portfolio_snapshot,
                in_run_open_orders,
            )
            identity_reasons = account_identity_reasons(
                config,
                portfolio_snapshot.account,
            )
            blocking_reasons.extend(
                f"account_identity:{reason}" for reason in identity_reasons
            )
            portfolio_gate, portfolio_risk = _portfolio_gate_for_order(
                order, payload, portfolio_snapshot, config
            )
            if portfolio_gate is None:
                blocking_reasons.append("portfolio_gate_unavailable")
            elif portfolio_gate.status != "PASS":
                blocking_reasons.append(f"portfolio_gate_{portfolio_gate.status.lower()}")
                blocking_reasons.extend(
                    f"portfolio:{reason}" for reason in portfolio_gate.reasons
                )
                blocking_reasons.extend(
                    f"portfolio_warning:{warning}" for warning in portfolio_gate.warnings
                )
        except (AlpacaConfigError, AlpacaRequestError) as exc:
            portfolio_error = f"{type(exc).__name__}: {exc}"
            blocking_reasons.append("portfolio_snapshot_unavailable")

    if blocking_reasons:
        return _blocked_result(
            order,
            payload,
            blocking_reasons,
            portfolio_error=portfolio_error,
            portfolio_risk=portfolio_risk,
        )

    broker_payload = _broker_payload(payload)
    ledger_order_id = None
    if ledger is not None:
        begin = ledger.begin_submit(
            client_order_id=client_order_id,
            order=order,
            broker_payload=broker_payload,
        )
        if not begin.inserted:
            return {
                **_base_result(order, payload),
                "status": "DUPLICATE_IN_OMS",
                "blocking_reasons": [],
                "error": f"client_order_id already exists in OMS with status {begin.existing_status}",
                "portfolio_error": portfolio_error,
                "portfolio_risk": portfolio_risk,
                "broker_payload": broker_payload,
                "broker_order": None,
                "oms_order_id": begin.order_id,
            }
        ledger_order_id = begin.order_id

    try:
        broker_order = client.submit_order(broker_payload) if client else None
    except (AlpacaConfigError, AlpacaRequestError) as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        if _is_duplicate_client_order_error(error_text):
            if ledger is not None and ledger_order_id is not None:
                ledger.update_after_submit(
                    order_id=ledger_order_id,
                    status="DUPLICATE_AT_BROKER",
                    error_message=error_text,
                )
            return {
                **_base_result(order, payload),
                "status": "DUPLICATE_AT_BROKER",
                "blocking_reasons": [],
                "error": error_text,
                "portfolio_error": portfolio_error,
                "portfolio_risk": portfolio_risk,
                "broker_payload": broker_payload,
                "broker_order": None,
                "oms_order_id": ledger_order_id,
            }
        if ledger is not None and ledger_order_id is not None:
            ledger.update_after_submit(
                order_id=ledger_order_id,
                status="SUBMIT_ERROR",
                error_message=error_text,
            )
        return {
            **_base_result(order, payload),
            "status": "SUBMIT_ERROR",
            "blocking_reasons": [],
            "error": error_text,
            "portfolio_error": portfolio_error,
            "portfolio_risk": portfolio_risk,
            "broker_payload": broker_payload,
            "broker_order": None,
            "oms_order_id": ledger_order_id,
        }

    broker_status = str((broker_order or {}).get("status") or "").lower()
    if broker_status == "rejected":
        status = "REJECTED"
        error = str((broker_order or {}).get("reject_reason") or "")
    elif broker_status in BROKER_SUBMITTED_STATUSES:
        status = "SUBMITTED"
        error = None
    else:
        status = "ERROR"
        error = f"unexpected_broker_order_status:{broker_status or 'missing'}"
    if ledger is not None and ledger_order_id is not None:
        ledger.update_after_submit(
            order_id=ledger_order_id,
            status=status,
            broker_order=broker_order or {},
            error_message=error,
        )
    return {
        **_base_result(order, payload),
        "status": status,
        "blocking_reasons": [],
        "error": error,
        "portfolio_error": portfolio_error,
        "portfolio_risk": portfolio_risk,
        "broker_payload": broker_payload,
        "broker_order": _broker_order_summary(broker_order or {}),
        "oms_order_id": ledger_order_id,
    }


def _execution_config_reasons(config: dict[str, Any]) -> list[str]:
    reasons = []
    if str(config.get("mode") or "").lower() != "paper":
        reasons.append("config_mode_not_paper")
    if str(config.get("broker") or "").lower() != "alpaca":
        reasons.append("config_broker_not_alpaca")
    account_cfg = config.get("account") or {}
    if str(account_cfg.get("account_type") or "").lower() != "paper":
        reasons.append("account_type_not_paper")
    if bool(account_cfg.get("live_trading_enabled")):
        reasons.append("live_trading_enabled")
    alpaca_cfg = config.get("alpaca") or {}
    base_url = str(alpaca_cfg.get("paper_base_url") or DEFAULT_PAPER_BASE_URL).rstrip("/")
    if base_url != DEFAULT_PAPER_BASE_URL:
        reasons.append("paper_base_url_not_default_paper_api")
    return reasons


def _validation_artifact_reasons(
    validated_shadow_orders: dict[str, Any],
    *,
    generated_at: str,
    config: dict[str, Any],
) -> list[str]:
    reasons = []
    artifact_timestamp = _parse_datetime(validated_shadow_orders.get("generated_at"))
    validation_time = _parse_datetime(generated_at)
    if artifact_timestamp is None or validation_time is None:
        return ["validation_artifact_timestamp_invalid"]
    age_seconds = (validation_time - artifact_timestamp).total_seconds()
    if age_seconds < -1:
        reasons.append("validation_artifact_timestamp_in_future")
    max_age = _max_validated_order_age_seconds(config)
    if age_seconds > max_age:
        reasons.append(f"stale_validation_artifact:{age_seconds:.0f}s>{max_age}s")
    return reasons


def _max_validated_order_age_seconds(config: dict[str, Any]) -> int:
    execution_cfg = config.get("execution") or {}
    try:
        value = int(
            execution_cfg.get("max_validated_order_age_seconds")
            or DEFAULT_MAX_VALIDATED_ORDER_AGE_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_MAX_VALIDATED_ORDER_AGE_SECONDS
    return value if value > 0 else DEFAULT_MAX_VALIDATED_ORDER_AGE_SECONDS


def _clock_blocking_reasons(
    clock: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    reasons = []
    if not bool(clock.get("is_open")):
        reasons.append("market_closed")

    execution_cfg = config.get("execution") or {}
    buffer_minutes = int(
        execution_cfg.get("no_open_minutes_before_close")
        or DEFAULT_NO_OPEN_MINUTES_BEFORE_CLOSE
    )
    timestamp = _parse_datetime(clock.get("timestamp"))
    next_close = _parse_datetime(clock.get("next_close"))
    if timestamp is None or next_close is None:
        reasons.append("market_clock_timestamp_unavailable")
    elif bool(clock.get("is_open")):
        minutes_to_close = (next_close - timestamp).total_seconds() / 60.0
        if minutes_to_close < buffer_minutes:
            reasons.append(
                f"near_close_gate:{minutes_to_close:.1f}m<{buffer_minutes}m"
            )
    return reasons


def _portfolio_gate_for_order(
    order: dict[str, Any],
    payload: dict[str, Any],
    portfolio: PortfolioSnapshot,
    config: dict[str, Any],
):
    candidate = _candidate_from_order(order, payload)
    if candidate is None:
        return None, None
    ticker = str(order.get("ticker") or candidate.option.symbol).upper()
    return evaluate_portfolio_risk(
        ticker,
        candidate,
        portfolio,
        config,
        required=True,
    )


def _candidate_from_order(
    order: dict[str, Any],
    payload: dict[str, Any],
) -> CspCandidate | None:
    symbol = str(payload.get("symbol") or "").upper()
    parsed = parse_occ_option_symbol(symbol)
    if not parsed or parsed.get("option_type") != "put":
        return None
    qty = _number(payload.get("qty")) or 1.0
    latest = order.get("latest_quote") or {}
    bid = _number(latest.get("bid")) or 0.0
    ask = _number(latest.get("ask")) or bid
    limit = _number(payload.get("limit_price")) or _number(order.get("validated_limit_price")) or 0.0
    strike = float(parsed["strike"])
    option = OptionQuote(
        symbol=symbol,
        expiration=parsed["expiration"],
        dte=0,
        strike=strike,
        bid=bid,
        ask=ask,
        last=limit,
        delta=None,
    )
    # This synthetic candidate exists only so portfolio_risk can re-use its
    # exposure math for submitted CSP payloads. Portfolio risk currently reads
    # ticker, option type, strike, quantity-derived assignment cash, positions,
    # and open orders; it does not depend on support score or return fields.
    return CspCandidate(
        option=option,
        support_zone=SupportZone(
            method="execution_payload",
            center=strike,
            bottom=strike,
            top=strike,
            score=0.0,
        ),
        delta=0.0,
        delta_bucket="execution_payload",
        auto_trade=True,
        weekly_return_on_strike_pct=0.0,
        assignment_cash_required=strike * 100.0 * qty,
    )


def _payload_blocking_reasons(payload: dict[str, Any]) -> list[str]:
    reasons = []
    unexpected_keys = sorted(set(payload) - BROKER_PAYLOAD_KEYS)
    if unexpected_keys:
        reasons.append(f"unexpected_payload_keys:{','.join(unexpected_keys)}")
    symbol = str(payload.get("symbol") or "").upper()
    parsed = parse_occ_option_symbol(symbol) if symbol else None
    if not parsed:
        reasons.append("invalid_occ_option_symbol")
    elif parsed.get("option_type") != "put":
        reasons.append("unsupported_option_type")
    if str(payload.get("side") or "").lower() != "sell":
        reasons.append("unsupported_side")
    if str(payload.get("type") or "").lower() != "limit":
        reasons.append("unsupported_order_type")
    if str(payload.get("time_in_force") or "").lower() != "day":
        reasons.append("unsupported_time_in_force")
    if str(payload.get("position_intent") or "").lower() != "sell_to_open":
        reasons.append("unsupported_position_intent")
    qty = _number(payload.get("qty"))
    if qty is None or qty <= 0 or int(qty) != qty:
        reasons.append("invalid_quantity")
    elif int(qty) != 1:
        reasons.append("unsupported_quantity_gt_one")
    limit = _number(payload.get("limit_price"))
    if limit is None or limit <= MONEY_EPSILON:
        reasons.append("invalid_limit_price")
    if not str(payload.get("client_order_id") or ""):
        reasons.append("missing_client_order_id")
    return reasons


def _broker_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(payload[key])
        for key in BROKER_PAYLOAD_KEYS
        if key in payload and payload[key] is not None
    }


def _with_in_run_open_orders(
    portfolio: PortfolioSnapshot,
    in_run_open_orders: list[BrokerOrder],
) -> PortfolioSnapshot:
    if not in_run_open_orders:
        return portfolio
    return PortfolioSnapshot(
        account=portfolio.account,
        positions=portfolio.positions,
        open_orders=[*portfolio.open_orders, *in_run_open_orders],
        source=portfolio.source,
        fetched_at=portfolio.fetched_at,
    )


def _broker_order_from_payload(payload: dict[str, Any]) -> BrokerOrder | None:
    symbol = str(payload.get("symbol") or "").upper()
    parsed = parse_occ_option_symbol(symbol) if symbol else None
    if not parsed:
        return None
    qty = _number(payload.get("qty")) or 0.0
    return BrokerOrder(
        id=str(payload.get("client_order_id") or ""),
        symbol=symbol,
        side=str(payload.get("side") or ""),
        qty=qty,
        status="accepted",
        asset_class="us_option",
        position_intent=str(payload.get("position_intent") or ""),
        limit_price=_number(payload.get("limit_price")),
        underlying_symbol=parsed.get("underlying_symbol"),
        option_type=parsed.get("option_type"),
        expiration=parsed.get("expiration"),
        strike=parsed.get("strike"),
    )


def _is_duplicate_client_order_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return "422" in lowered and "client_order_id" in lowered


def _blocked_result(
    order: dict[str, Any],
    payload: dict[str, Any],
    blocking_reasons: list[str],
    *,
    portfolio_error: str | None,
    portfolio_risk: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        **_base_result(order, payload),
        "status": "BLOCKED",
        "blocking_reasons": blocking_reasons,
        "error": None,
        "portfolio_error": portfolio_error,
        "portfolio_risk": portfolio_risk,
        "broker_payload": None,
        "broker_order": None,
    }


def _base_result(order: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "shadow_order_id": order.get("shadow_order_id"),
        "proposal_id": order.get("proposal_id"),
        "ticker": order.get("ticker"),
        "strategy": order.get("strategy"),
        "client_order_id": str(payload.get("client_order_id") or ""),
        "symbol": payload.get("symbol"),
        "qty": payload.get("qty"),
        "limit_price": payload.get("limit_price"),
    }


def _broker_order_summary(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "client_order_id": order.get("client_order_id"),
        "status": order.get("status"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "qty": order.get("qty"),
        "limit_price": order.get("limit_price"),
        "reject_reason": order.get("reject_reason"),
        "submitted_at": order.get("submitted_at"),
    }


def _submitted_client_order_ids(
    previous_execution_results: dict[str, Any] | None,
) -> set[str]:
    if not previous_execution_results:
        return set()
    return {
        str(order.get("client_order_id"))
        for order in previous_execution_results.get("orders", [])
        if order.get("status") == "SUBMITTED" and order.get("client_order_id")
    }


def _clock_summary(clock: dict[str, Any] | None) -> dict[str, Any] | None:
    if not clock:
        return None
    return {
        "is_open": clock.get("is_open"),
        "timestamp": clock.get("timestamp"),
        "next_open": clock.get("next_open"),
        "next_close": clock.get("next_close"),
    }


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
