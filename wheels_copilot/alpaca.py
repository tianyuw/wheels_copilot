from __future__ import annotations

from datetime import date, datetime
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from .models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    PortfolioSnapshot,
)


DEFAULT_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
OPEN_ORDER_LIMIT = 500
OCC_TAIL_RE = re.compile(r"^(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")


class AlpacaConfigError(RuntimeError):
    pass


class AlpacaRequestError(RuntimeError):
    pass


def fetch_alpaca_portfolio_snapshot(
    config: dict[str, Any],
    env: dict[str, str] | None = None,
) -> PortfolioSnapshot:
    client = AlpacaTradingClient.from_config(config, env=env)
    return client.fetch_portfolio_snapshot()


class AlpacaTradingClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = DEFAULT_PAPER_BASE_URL,
        timeout: float = 20.0,
        opener=request.urlopen,
    ) -> None:
        if not api_key or not secret_key:
            raise AlpacaConfigError("missing Alpaca API credentials")
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> AlpacaTradingClient:
        env = _merged_env(env)
        cfg = config.get("alpaca", {})
        api_key_env = str(cfg.get("api_key_env") or "ALPACA_API_KEY")
        secret_key_env = str(cfg.get("secret_key_env") or "ALPACA_SECRET_KEY")
        api_key = env.get(api_key_env) or env.get("APCA_API_KEY_ID")
        secret_key = env.get(secret_key_env) or env.get("APCA_API_SECRET_KEY")
        base_url = str(cfg.get("paper_base_url") or DEFAULT_PAPER_BASE_URL)
        timeout = float(cfg.get("request_timeout_seconds") or 20.0)
        return cls(
            api_key=api_key or "",
            secret_key=secret_key or "",
            base_url=base_url,
            timeout=timeout,
        )

    def fetch_portfolio_snapshot(self) -> PortfolioSnapshot:
        account = _account_from_payload(self._get_json("/v2/account"))
        positions = [
            _position_from_payload(item)
            for item in self._get_json("/v2/positions")
        ]
        order_payloads = self._get_json(
            "/v2/orders",
            {"status": "open", "limit": str(OPEN_ORDER_LIMIT), "nested": "true"},
        )
        if len(order_payloads) >= OPEN_ORDER_LIMIT:
            raise AlpacaRequestError(
                "Alpaca open orders reached limit; pagination required for safe risk snapshot"
            )
        orders = [
            order
            for item in order_payloads
            for order in _orders_from_payload(item)
        ]
        return PortfolioSnapshot(
            account=account,
            positions=positions,
            open_orders=orders,
            source="alpaca_paper",
            fetched_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _get_json(
        self,
        path: str,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + parse.urlencode(query)
        req = request.Request(
            url,
            method="GET",
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Accept": "application/json",
            },
        )
        try:
            with self.opener(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            request_id = exc.headers.get("X-Request-ID") if exc.headers else None
            raise AlpacaRequestError(
                f"Alpaca GET {path} failed with HTTP {exc.code}"
                + (f" request_id={request_id}" if request_id else "")
            ) from exc
        except error.URLError as exc:
            raise AlpacaRequestError(f"Alpaca GET {path} failed: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AlpacaRequestError(f"Alpaca GET {path} returned invalid JSON") from exc


def parse_occ_option_symbol(symbol: str) -> dict[str, Any] | None:
    if len(symbol) < 16:
        return None
    root = symbol[:-15].upper()
    match = OCC_TAIL_RE.match(symbol[-15:].upper())
    if not root or match is None:
        return None
    date_raw = match.group("date")
    option_char = match.group("type")
    strike_raw = match.group("strike")
    try:
        expiration = date(
            2000 + int(date_raw[:2]),
            int(date_raw[2:4]),
            int(date_raw[4:6]),
        )
        strike = int(strike_raw) / 1000.0
    except ValueError:
        return None
    return {
        "underlying_symbol": root,
        "expiration": expiration,
        "option_type": "put" if option_char == "P" else "call",
        "strike": strike,
    }


def _account_from_payload(payload: dict[str, Any]) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        status=_str_or_none(payload.get("status")),
        equity=_num_or_none(payload.get("equity")),
        cash=_num_or_none(payload.get("cash")),
        buying_power=_num_or_none(payload.get("buying_power")),
        options_trading_level=_int_or_none(payload.get("options_trading_level")),
        trading_blocked=bool(payload.get("trading_blocked")),
        account_blocked=bool(payload.get("account_blocked")),
    )


def _position_from_payload(payload: dict[str, Any]) -> BrokerPosition:
    symbol = str(payload.get("symbol") or "").upper()
    parsed = parse_occ_option_symbol(symbol) or {}
    side = _str_or_none(payload.get("side"))
    return BrokerPosition(
        symbol=symbol,
        qty=_signed_position_qty(payload.get("qty"), side),
        asset_class=_str_or_none(payload.get("asset_class")),
        side=side,
        market_value=_num_or_none(payload.get("market_value")),
        cost_basis=_num_or_none(payload.get("cost_basis")),
        underlying_symbol=parsed.get("underlying_symbol"),
        option_type=parsed.get("option_type"),
        expiration=parsed.get("expiration"),
        strike=parsed.get("strike"),
    )


def _orders_from_payload(payload: dict[str, Any]) -> list[BrokerOrder]:
    legs = payload.get("legs")
    if isinstance(legs, list) and legs:
        parent = _order_from_payload(payload)
        return [
            parent,
            *[
                _order_from_payload(leg, parent_order_id=str(payload.get("id") or ""))
                for leg in legs
            ],
        ]
    return [_order_from_payload(payload)]


def _order_from_payload(
    payload: dict[str, Any],
    parent_order_id: str | None = None,
) -> BrokerOrder:
    symbol = str(payload.get("symbol") or "").upper()
    parsed = parse_occ_option_symbol(symbol) or {}
    return BrokerOrder(
        id=str(payload.get("id") or parent_order_id or ""),
        parent_order_id=parent_order_id,
        symbol=symbol,
        side=_str_or_none(payload.get("side")),
        qty=_num_or_none(payload.get("qty")) or 0.0,
        status=_str_or_none(payload.get("status")),
        asset_class=_str_or_none(payload.get("asset_class")),
        order_class=_str_or_none(payload.get("order_class")),
        position_intent=_str_or_none(payload.get("position_intent")),
        limit_price=_num_or_none(payload.get("limit_price")),
        submitted_at=_str_or_none(payload.get("submitted_at")),
        underlying_symbol=parsed.get("underlying_symbol"),
        option_type=parsed.get("option_type"),
        expiration=parsed.get("expiration"),
        strike=parsed.get("strike"),
    )


def _merged_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged = _read_env_file(Path(".env"))
    merged.update(os.environ)
    if env:
        merged.update(env)
    return merged


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _num_or_none(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _signed_position_qty(value, side: str | None) -> float:
    qty = _num_or_none(value) or 0.0
    if (side or "").lower() == "short" and qty > 0:
        return -qty
    return qty


def _int_or_none(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
