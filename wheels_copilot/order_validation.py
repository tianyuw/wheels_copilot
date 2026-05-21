from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .alpaca import (
    AlpacaConfigError,
    AlpacaMarketDataClient,
    AlpacaRequestError,
    parse_occ_option_symbol,
)


DEFAULT_MAX_QUOTE_AGE_SECONDS = 30
DEFAULT_MAX_SPREAD_PCT_OF_MID = 0.12
MONEY_EPSILON = 0.005
FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 1.0
VALIDATED_ORDER_METADATA_KEYS = (
    "estimated_premium_credit",
    "share_quantity",
    "available_shares_for_cc",
    "adjusted_cost_basis",
    "min_acceptable_strike",
    "fundamental_snapshot",
    "earnings_gate",
    "ex_dividend_gate",
    "unchecked_risks",
    "warnings",
)


def build_validated_shadow_orders(
    shadow_orders: dict[str, Any],
    config: dict[str, Any] | None = None,
    client: AlpacaMarketDataClient | None = None,
) -> dict[str, Any]:
    config = config or {}
    generated_at = _now_iso()
    orders = shadow_orders.get("orders") or []
    validator_cfg = _validator_config(config)

    try:
        client = client or AlpacaMarketDataClient.from_config(config)
        quotes = _fetch_latest_quotes(client, _order_symbols(orders))
        contracts = _fetch_contracts(client, orders)
        validator_error = None
    except (AlpacaConfigError, AlpacaRequestError) as exc:
        quotes = {}
        contracts = {}
        validator_error = f"{type(exc).__name__}: {exc}"

    validated_orders = [
        _validate_order(
            order,
            quote=quotes.get(_payload_symbol(order)),
            contract=contracts.get(_payload_symbol(order)),
            generated_at=generated_at,
            validator_cfg=validator_cfg,
            validator_error=validator_error,
        )
        for order in orders
    ]
    summary = Counter(
        "SUBMIT_READY" if order["submit_ready"] else "BLOCKED"
        for order in validated_orders
    )
    return {
        "scan_date": shadow_orders.get("scan_date"),
        "generated_at": generated_at,
        "source_shadow_orders_generated_at": shadow_orders.get("generated_at"),
        "dry_run_only": True,
        "broker": shadow_orders.get("broker") or "alpaca",
        "validator": {
            "version": 1,
            "feed": "opra",
            "max_quote_age_seconds": validator_cfg["max_quote_age_seconds"],
            "max_spread_pct_of_mid": validator_cfg["max_spread_pct_of_mid"],
            "contract_validation": True,
            "latest_quote_validation": True,
        },
        "validation_error": validator_error,
        "order_count": len(validated_orders),
        "submit_ready_count": sum(1 for order in validated_orders if order["submit_ready"]),
        "summary": dict(sorted(summary.items())),
        "orders": validated_orders,
    }


def _validate_order(
    order: dict[str, Any],
    *,
    quote: dict[str, Any] | None,
    contract: dict[str, Any] | None,
    generated_at: str,
    validator_cfg: dict[str, float],
    validator_error: str | None,
) -> dict[str, Any]:
    payload = order.get("payload") or {}
    blocking_reasons = _payload_reasons(order, payload)
    if validator_error:
        blocking_reasons.append("alpaca_validation_unavailable")
    blocking_reasons.extend(_contract_reasons(contract))

    quote_summary, quote_reasons = _quote_summary(
        quote,
        max_quote_age_seconds=validator_cfg["max_quote_age_seconds"],
        max_spread_pct_of_mid=validator_cfg["max_spread_pct_of_mid"],
        validated_at=generated_at,
    )
    blocking_reasons.extend(quote_reasons)

    validated_limit_price = quote_summary.get("mid")
    original_limit_price = _num(payload.get("limit_price"))
    if quote_summary.get("bid") is not None and quote_summary.get("ask") is not None:
        bid = float(quote_summary["bid"])
        ask = float(quote_summary["ask"])
        if original_limit_price is None:
            blocking_reasons.append("missing_limit_price")
        elif original_limit_price < bid - MONEY_EPSILON or original_limit_price > ask + MONEY_EPSILON:
            blocking_reasons.append("original_limit_price_outside_latest_quote")

    submit_ready = not blocking_reasons
    validated_payload = dict(payload)
    if submit_ready and validated_limit_price is not None:
        validated_payload["limit_price"] = f"{float(validated_limit_price):.2f}"
    elif "limit_price" in validated_payload:
        validated_payload["limit_price"] = None
    validated_order = {
        "shadow_order_id": order.get("shadow_order_id"),
        "proposal_id": order.get("proposal_id"),
        "validated_at": generated_at,
        "dry_run_only": True,
        "submit_ready": submit_ready,
        "blocking_reasons": blocking_reasons,
        "ticker": order.get("ticker"),
        "strategy": order.get("strategy"),
        "latest_quote": quote_summary,
        "contract": _contract_summary(contract),
        "original_payload": payload,
        "validated_limit_price": (
            round(float(validated_limit_price), 2)
            if validated_limit_price is not None
            else None
        ),
        "validated_payload": validated_payload,
    }
    for key in VALIDATED_ORDER_METADATA_KEYS:
        if key in order:
            validated_order[key] = order.get(key)
    return validated_order


def _fetch_latest_quotes(
    client: AlpacaMarketDataClient,
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(symbols, 100):
        quotes.update(client.fetch_option_latest_quotes(chunk))
    return quotes


def _fetch_contracts(
    client: AlpacaMarketDataClient,
    orders: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    groups: dict[tuple[str, str, Any], set[str]] = defaultdict(set)
    for order in orders:
        symbol = _payload_symbol(order)
        parsed = parse_occ_option_symbol(symbol) if symbol else None
        if not parsed:
            continue
        groups[
            (
                parsed["underlying_symbol"],
                parsed["option_type"],
                parsed["expiration"],
            )
        ].add(symbol)

    contracts: dict[str, dict[str, Any]] = {}
    for (underlying, option_type, expiration), symbols in groups.items():
        payloads = client.fetch_option_contracts(
            underlying,
            option_type=option_type,
            expiration_date_gte=expiration,
            expiration_date_lte=expiration,
        )
        wanted = {symbol.upper() for symbol in symbols}
        for payload in payloads:
            symbol = str(payload.get("symbol") or "").upper()
            if symbol in wanted:
                contracts[symbol] = payload
    return contracts


def _payload_reasons(order: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    reasons = []
    symbol = str(payload.get("symbol") or "").upper()
    parsed = None
    if not symbol:
        reasons.append("missing_symbol")
    else:
        parsed = parse_occ_option_symbol(symbol)
    if symbol and parsed is None:
        reasons.append("invalid_occ_option_symbol")
    expected_option_type = _expected_option_type(order)
    if expected_option_type is None:
        reasons.append(f"unsupported_strategy:{_order_strategy(order) or 'missing'}")
    elif parsed and parsed.get("option_type") != expected_option_type:
        reasons.append(
            "strategy_option_type_mismatch:"
            f"{_order_strategy(order)}:{parsed.get('option_type')}"
        )
    if str(payload.get("side") or "").lower() != "sell":
        reasons.append("unsupported_side")
    if str(payload.get("type") or "").lower() != "limit":
        reasons.append("unsupported_order_type")
    if str(payload.get("position_intent") or "").lower() != "sell_to_open":
        reasons.append("unsupported_position_intent")
    qty = _num(payload.get("qty"))
    if qty is None or qty <= 0:
        reasons.append("invalid_quantity")
    return reasons


def _order_strategy(order: dict[str, Any]) -> str:
    return str(order.get("strategy") or "").strip().lower()


def _expected_option_type(order: dict[str, Any]) -> str | None:
    strategy = _order_strategy(order)
    if strategy in {"cash_secured_put", "csp"}:
        return "put"
    if strategy in {"covered_call", "cc"}:
        return "call"
    return None


def _contract_reasons(contract: dict[str, Any] | None) -> list[str]:
    if not contract:
        return ["contract_not_found"]
    reasons = []
    status = str(contract.get("status") or "").lower()
    if status and status != "active":
        reasons.append(f"contract_status_{status}")
    if contract.get("tradable") is False:
        reasons.append("contract_not_tradable")
    return reasons


def _quote_summary(
    quote: dict[str, Any] | None,
    *,
    max_quote_age_seconds: float,
    max_spread_pct_of_mid: float,
    validated_at: str,
) -> tuple[dict[str, Any], list[str]]:
    if not quote:
        return {}, ["latest_quote_missing"]
    bid = _num(quote.get("bp"))
    ask = _num(quote.get("ap"))
    timestamp = _datetime_from_timestamp(quote.get("t"))
    validation_time = _datetime_from_timestamp(validated_at)
    reasons = []
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        reasons.append("invalid_bid_ask")
    age_seconds = None
    if timestamp is None:
        reasons.append("latest_quote_timestamp_missing")
    elif validation_time is None:
        reasons.append("validation_timestamp_invalid")
    else:
        age_seconds = (validation_time - timestamp).total_seconds()
        if age_seconds < -FUTURE_TIMESTAMP_TOLERANCE_SECONDS:
            reasons.append("latest_quote_timestamp_in_future")
        elif age_seconds > max_quote_age_seconds:
            reasons.append("latest_quote_stale")

    mid = None
    spread_pct_of_mid = None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_pct_of_mid = (ask - bid) / mid if mid > 0 else None
        if (
            spread_pct_of_mid is not None
            and spread_pct_of_mid > max_spread_pct_of_mid
        ):
            reasons.append("spread_too_wide")

    return (
        {
            "timestamp": quote.get("t"),
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "bid": bid,
            "ask": ask,
            "bid_size": _num(quote.get("bs")),
            "ask_size": _num(quote.get("as")),
            "bid_exchange": quote.get("bx"),
            "ask_exchange": quote.get("ax"),
            "mid": round(mid, 2) if mid is not None else None,
            "spread_pct_of_mid": (
                round(spread_pct_of_mid, 4)
                if spread_pct_of_mid is not None
                else None
            ),
        },
        reasons,
    )


def _contract_summary(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    if not contract:
        return None
    return {
        "symbol": contract.get("symbol"),
        "status": contract.get("status"),
        "tradable": contract.get("tradable"),
        "expiration_date": contract.get("expiration_date"),
        "strike_price": contract.get("strike_price"),
        "type": contract.get("type"),
        "underlying_symbol": contract.get("underlying_symbol"),
    }


def _validator_config(config: dict[str, Any]) -> dict[str, float]:
    cfg = config.get("shadow_order_validation") or {}
    return {
        "max_quote_age_seconds": float(
            cfg.get("max_quote_age_seconds", DEFAULT_MAX_QUOTE_AGE_SECONDS)
        ),
        "max_spread_pct_of_mid": float(
            cfg.get("max_spread_pct_of_mid", DEFAULT_MAX_SPREAD_PCT_OF_MID)
        ),
    }


def _order_symbols(orders: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            symbol
            for order in orders
            if (symbol := _payload_symbol(order))
        }
    )


def _payload_symbol(order: dict[str, Any]) -> str:
    return str((order.get("payload") or {}).get("symbol") or "").upper()


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _num(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _datetime_from_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
