from __future__ import annotations

import unittest
from datetime import datetime, timezone

from wheels_copilot.alpaca import AlpacaRequestError
from wheels_copilot.order_validation import build_validated_shadow_orders


class ShadowOrderValidationTests(unittest.TestCase):
    def test_valid_shadow_order_is_submit_ready_with_latest_opra_quote(self):
        shadow_orders = _shadow_orders([_order()])
        client = _FakeValidationClient()

        validated = build_validated_shadow_orders(
            shadow_orders,
            config=_config(),
            client=client,
        )

        self.assertEqual(validated["summary"], {"SUBMIT_READY": 1})
        order = validated["orders"][0]
        self.assertTrue(order["submit_ready"])
        self.assertEqual(order["blocking_reasons"], [])
        self.assertEqual(order["validated_limit_price"], 1.1)
        self.assertEqual(order["validated_payload"]["limit_price"], "1.10")
        self.assertEqual(client.quote_calls, [["AAPL260522P00275000"]])

    def test_stale_quote_blocks_submit_ready(self):
        client = _FakeValidationClient(quote_timestamp="2026-05-20T14:30:00Z")

        validated = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config=_config(max_quote_age_seconds=10),
            client=client,
        )

        order = validated["orders"][0]
        self.assertFalse(order["submit_ready"])
        self.assertIn("latest_quote_stale", order["blocking_reasons"])

    def test_invalid_quote_and_contract_block_submit_ready(self):
        client = _FakeValidationClient(
            bid=1.2,
            ask=1.0,
            contract={"status": "inactive", "tradable": False},
        )

        validated = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config=_config(),
            client=client,
        )

        reasons = validated["orders"][0]["blocking_reasons"]
        self.assertIn("invalid_bid_ask", reasons)
        self.assertIn("contract_status_inactive", reasons)
        self.assertIn("contract_not_tradable", reasons)
        self.assertIsNone(validated["orders"][0]["validated_payload"]["limit_price"])

    def test_production_default_spread_blocks_wide_quote(self):
        validated = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config={},
            client=_FakeValidationClient(bid=1.0, ask=1.2),
        )

        reasons = validated["orders"][0]["blocking_reasons"]
        self.assertIn("spread_too_wide", reasons)

    def test_original_limit_outside_latest_quote_blocks_submit_ready(self):
        client = _FakeValidationClient(bid=1.2, ask=1.4)

        validated = build_validated_shadow_orders(
            _shadow_orders([_order(limit_price="1.10")]),
            config=_config(),
            client=client,
        )

        order = validated["orders"][0]
        self.assertFalse(order["submit_ready"])
        self.assertIn(
            "original_limit_price_outside_latest_quote",
            order["blocking_reasons"],
        )
        self.assertEqual(order["validated_limit_price"], 1.3)
        self.assertIsNone(order["validated_payload"]["limit_price"])

    def test_missing_naive_and_future_quote_timestamps_block_or_normalize(self):
        missing = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config=_config(),
            client=_FakeValidationClient(quote_timestamp=""),
        )
        self.assertIn(
            "latest_quote_timestamp_missing",
            missing["orders"][0]["blocking_reasons"],
        )

        naive = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config=_config(),
            client=_FakeValidationClient(
                bid=1.0,
                ask=1.1,
                quote_timestamp=datetime.now(timezone.utc)
                .replace(tzinfo=None)
                .isoformat(),
            ),
        )
        self.assertTrue(naive["orders"][0]["submit_ready"])

        future = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config=_config(),
            client=_FakeValidationClient(quote_timestamp="2999-01-01T00:00:00Z"),
        )
        self.assertIn(
            "latest_quote_timestamp_in_future",
            future["orders"][0]["blocking_reasons"],
        )

    def test_missing_client_fails_closed(self):
        validated = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config={
                "alpaca": {
                    "api_key_env": "__MISSING_ALPACA_KEY__",
                    "secret_key_env": "__MISSING_ALPACA_SECRET__",
                }
            },
        )

        order = validated["orders"][0]
        self.assertFalse(order["submit_ready"])
        self.assertIn("alpaca_validation_unavailable", order["blocking_reasons"])
        self.assertEqual(validated["summary"], {"BLOCKED": 1})

    def test_invalid_payload_blocks_submit_ready_before_broker_checks(self):
        bad_order = _order(symbol="", side="buy", qty="0", order_type="market")

        validated = build_validated_shadow_orders(
            _shadow_orders([bad_order]),
            config=_config(),
            client=_FakeValidationClient(),
        )

        reasons = validated["orders"][0]["blocking_reasons"]
        self.assertIn("missing_symbol", reasons)
        self.assertIn("unsupported_side", reasons)
        self.assertIn("unsupported_order_type", reasons)
        self.assertIn("invalid_quantity", reasons)

    def test_contract_lookup_uses_occ_underlying_type_and_expiration(self):
        client = _FakeValidationClient(bid=1.0, ask=1.1)

        validated = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config=_config(),
            client=client,
        )

        self.assertTrue(validated["orders"][0]["submit_ready"])
        self.assertEqual(client.contract_calls[0]["underlying_symbol"], "AAPL")
        self.assertEqual(client.contract_calls[0]["option_type"], "put")
        self.assertEqual(
            client.contract_calls[0]["expiration_date_gte"].isoformat(),
            "2026-05-22",
        )
        self.assertEqual(
            client.contract_calls[0]["expiration_date_lte"].isoformat(),
            "2026-05-22",
        )

    def test_expected_alpaca_errors_fail_closed_but_programming_errors_raise(self):
        http_client = _FakeValidationClient(
            raise_error=AlpacaRequestError("Alpaca GET failed with HTTP 429")
        )

        validated = build_validated_shadow_orders(
            _shadow_orders([_order()]),
            config=_config(),
            client=http_client,
        )

        self.assertIn(
            "alpaca_validation_unavailable",
            validated["orders"][0]["blocking_reasons"],
        )

        with self.assertRaises(RuntimeError):
            build_validated_shadow_orders(
                _shadow_orders([_order()]),
                config=_config(),
                client=_FakeValidationClient(raise_error=RuntimeError("bug")),
            )


def _config(max_quote_age_seconds: int = 10) -> dict:
    return {
        "shadow_order_validation": {
            "max_quote_age_seconds": max_quote_age_seconds,
            "max_spread_pct_of_mid": 0.30,
        }
    }


def _shadow_orders(orders: list[dict]) -> dict:
    return {
        "scan_date": "2026-05-20",
        "generated_at": "2026-05-20T10:00:00+00:00",
        "dry_run_only": True,
        "broker": "alpaca",
        "order_count": len(orders),
        "orders": orders,
    }


def _order(
    *,
    symbol: str = "AAPL260522P00275000",
    side: str = "sell",
    qty: str = "1",
    order_type: str = "limit",
    limit_price: str = "1.10",
) -> dict:
    return {
        "shadow_order_id": "order-1",
        "proposal_id": "proposal-1",
        "created_at": "2026-05-20T10:00:00+00:00",
        "dry_run_only": True,
        "broker": "alpaca",
        "strategy": "cash_secured_put",
        "ticker": "AAPL",
        "payload": {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": order_type,
            "time_in_force": "day",
            "limit_price": limit_price,
            "position_intent": "sell_to_open",
            "client_order_id": "client-order-1",
        },
    }


class _FakeValidationClient:
    option_feed = "opra"

    def __init__(
        self,
        *,
        bid: float = 1.0,
        ask: float = 1.2,
        quote_timestamp: str | None = None,
        contract: dict | None = None,
        raise_error: Exception | None = None,
    ):
        self.bid = bid
        self.ask = ask
        self.quote_timestamp = (
            datetime.now(timezone.utc).isoformat()
            if quote_timestamp is None
            else quote_timestamp
        )
        self.contract = contract or {"status": "active", "tradable": True}
        self.raise_error = raise_error
        self.quote_calls = []
        self.contract_calls = []

    def fetch_option_latest_quotes(self, symbols: list[str]):
        if self.raise_error:
            raise self.raise_error
        self.quote_calls.append(list(symbols))
        return {
            symbol: {
                "t": self.quote_timestamp or "",
                "bp": self.bid,
                "ap": self.ask,
                "bs": 10,
                "as": 12,
                "bx": "Q",
                "ax": "U",
            }
            for symbol in symbols
        }

    def fetch_option_contracts(self, underlying_symbol: str, **kwargs):
        self.contract_calls.append({"underlying_symbol": underlying_symbol, **kwargs})
        return [
            {
                "symbol": "AAPL260522P00275000",
                "expiration_date": "2026-05-22",
                "strike_price": "275",
                "type": "put",
                "underlying_symbol": "AAPL",
                **self.contract,
            }
        ]


if __name__ == "__main__":
    unittest.main()
