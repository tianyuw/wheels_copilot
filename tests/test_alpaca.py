from __future__ import annotations

import json
import unittest
from datetime import date
from io import BytesIO

from wheels_copilot.alpaca import AlpacaRequestError, AlpacaTradingClient, parse_occ_option_symbol


class AlpacaAdapterTests(unittest.TestCase):
    def test_parse_occ_option_symbol(self):
        parsed = parse_occ_option_symbol("AAPL260522P00275000")

        self.assertEqual(
            parsed,
            {
                "underlying_symbol": "AAPL",
                "expiration": date(2026, 5, 22),
                "option_type": "put",
                "strike": 275.0,
            },
        )

    def test_parse_occ_option_symbol_supports_adjusted_roots_and_rejects_malformed(self):
        adjusted = parse_occ_option_symbol("AAPL1260522P00275000")

        self.assertEqual(adjusted["underlying_symbol"], "AAPL1")
        self.assertEqual(adjusted["expiration"], date(2026, 5, 22))
        self.assertEqual(adjusted["strike"], 275.0)
        self.assertIsNone(parse_occ_option_symbol("AAPL260522P0027500"))
        self.assertIsNone(parse_occ_option_symbol("AAPL260522X00275000"))

    def test_fetch_portfolio_snapshot_maps_account_positions_and_orders(self):
        calls = []

        def fake_open(req, timeout):
            calls.append(req.full_url)
            if req.full_url.endswith("/v2/account"):
                return _Response(
                    {
                        "status": "ACTIVE",
                        "equity": "500000",
                        "cash": "425000",
                        "buying_power": "425000",
                        "options_trading_level": "3",
                    }
                )
            if req.full_url.endswith("/v2/positions"):
                return _Response(
                    [
                        {
                            "symbol": "AAPL260522P00275000",
                            "qty": "1",
                            "asset_class": "us_option",
                            "side": "short",
                            "market_value": "-120",
                            "cost_basis": "-100",
                        },
                        {"symbol": "UPS", "qty": "100", "asset_class": "us_equity"},
                        {
                            "symbol": "TSLA",
                            "qty": "25",
                            "side": "short",
                            "asset_class": "us_equity",
                        },
                    ]
                )
            if "/v2/orders?" in req.full_url:
                return _Response(
                    [
                        {
                            "id": "order-1",
                            "symbol": "MSFT260522P00300000",
                            "side": "sell",
                            "qty": "1",
                            "status": "new",
                            "asset_class": "us_option",
                            "limit_price": "1.25",
                        }
                    ]
                )
            raise AssertionError(req.full_url)

        client = AlpacaTradingClient(
            "key",
            "secret",
            base_url="https://paper-api.alpaca.markets",
            opener=fake_open,
        )

        snapshot = client.fetch_portfolio_snapshot()

        self.assertEqual(len(calls), 3)
        self.assertEqual(snapshot.account.status, "ACTIVE")
        self.assertEqual(snapshot.account.cash, 425000)
        self.assertEqual(snapshot.positions[0].underlying_symbol, "AAPL")
        self.assertEqual(snapshot.positions[0].assignment_cash_required, 27500)
        self.assertEqual(snapshot.positions[1].symbol, "UPS")
        self.assertEqual(snapshot.positions[2].symbol, "TSLA")
        self.assertEqual(snapshot.positions[2].qty, -25)
        self.assertEqual(snapshot.open_orders[0].underlying_symbol, "MSFT")
        self.assertEqual(snapshot.open_orders[0].assignment_cash_required, 30000)

    def test_nested_orders_include_parent_and_children(self):
        def fake_open(req, timeout):
            if req.full_url.endswith("/v2/account"):
                return _Response({"status": "ACTIVE", "equity": "500000", "cash": "500000"})
            if req.full_url.endswith("/v2/positions"):
                return _Response([])
            if "/v2/orders?" in req.full_url:
                return _Response(
                    [
                        {
                            "id": "parent",
                            "symbol": "AAPL260522P00275000",
                            "side": "sell",
                            "qty": "1",
                            "status": "new",
                            "order_class": "oto",
                            "legs": [
                                {
                                    "id": "child",
                                    "symbol": "AAPL260522P00275000",
                                    "side": "buy",
                                    "qty": "1",
                                    "status": "new",
                                }
                            ],
                        }
                    ]
                )
            raise AssertionError(req.full_url)

        client = AlpacaTradingClient("key", "secret", opener=fake_open)

        snapshot = client.fetch_portfolio_snapshot()

        self.assertEqual([order.id for order in snapshot.open_orders], ["parent", "child"])
        self.assertIsNone(snapshot.open_orders[0].parent_order_id)
        self.assertEqual(snapshot.open_orders[1].parent_order_id, "parent")

    def test_open_order_limit_fails_closed(self):
        def fake_open(req, timeout):
            if req.full_url.endswith("/v2/account"):
                return _Response({"status": "ACTIVE", "equity": "500000", "cash": "500000"})
            if req.full_url.endswith("/v2/positions"):
                return _Response([])
            if "/v2/orders?" in req.full_url:
                return _Response([{"id": str(i), "symbol": "AAPL"} for i in range(500)])
            raise AssertionError(req.full_url)

        client = AlpacaTradingClient("key", "secret", opener=fake_open)

        with self.assertRaises(AlpacaRequestError):
            client.fetch_portfolio_snapshot()


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return BytesIO(json.dumps(self.payload).encode("utf-8")).read()


if __name__ == "__main__":
    unittest.main()
