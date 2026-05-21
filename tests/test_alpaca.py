from __future__ import annotations

import json
import unittest
from datetime import date
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from wheels_copilot.alpaca import (
    AlpacaConfigError,
    AlpacaMarketDataClient,
    AlpacaRequestError,
    AlpacaTradingClient,
    parse_occ_option_symbol,
)


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

    def test_market_data_client_requires_realtime_feeds(self):
        with self.assertRaises(AlpacaConfigError):
            AlpacaMarketDataClient("key", "secret", stock_feed="iex", option_feed="opra")
        with self.assertRaises(AlpacaConfigError):
            AlpacaMarketDataClient("key", "secret", stock_feed="sip", option_feed="indicative")

    def test_market_data_client_fetches_sip_bars_and_opra_snapshots(self):
        calls = []

        def fake_open(req, timeout):
            calls.append(req.full_url)
            if "/v2/stocks/bars?" in req.full_url:
                self.assertIn("feed=sip", req.full_url)
                return _Response({"bars": {"AAPL": [{"t": "2026-05-20T04:00:00Z", "c": 1}]}})
            if "/v2/options/contracts?" in req.full_url:
                return _Response(
                    {
                        "option_contracts": [
                            {"symbol": "AAPL260522P00275000", "expiration_date": "2026-05-22"}
                        ]
                    }
                )
            if "/v1beta1/options/snapshots?" in req.full_url:
                self.assertIn("feed=opra", req.full_url)
                return _Response({"snapshots": {"AAPL260522P00275000": {}}})
            raise AssertionError(req.full_url)

        client = AlpacaMarketDataClient("key", "secret", opener=fake_open)

        bars = client.fetch_stock_bars(
            "AAPL",
            timeframe="1Day",
            start="2026-05-20T00:00:00+00:00",
        )
        contracts = client.fetch_option_contracts(
            "AAPL",
            option_type="put",
            expiration_date_gte=date(2026, 5, 21),
            expiration_date_lte=date(2026, 5, 29),
        )
        snapshots = client.fetch_option_snapshots(["AAPL260522P00275000"])

        self.assertEqual(bars, [{"t": "2026-05-20T04:00:00Z", "c": 1}])
        self.assertEqual(contracts[0]["symbol"], "AAPL260522P00275000")
        self.assertEqual(snapshots, {"AAPL260522P00275000": {}})
        self.assertEqual(len(calls), 3)

    def test_market_data_client_paginates_and_preserves_empty_snapshots(self):
        def fake_open(req, timeout):
            parsed = urlparse(req.full_url)
            query = parse_qs(parsed.query)
            page_token = query.get("page_token", [None])[0]
            if parsed.path == "/v2/stocks/bars":
                if page_token is None:
                    return _Response(
                        {
                            "bars": {"AAPL": [{"t": "2026-05-20T04:00:00Z"}]},
                            "next_page_token": "bars-page-2",
                        }
                    )
                return _Response({"bars": {"AAPL": [{"t": "2026-05-21T04:00:00Z"}]}})
            if parsed.path == "/v2/options/contracts":
                if page_token is None:
                    return _Response(
                        {
                            "option_contracts": [{"symbol": "AAPL260522P00275000"}],
                            "next_page_token": "contracts-page-2",
                        }
                    )
                return _Response(
                    {"option_contracts": [{"symbol": "AAPL260529P00270000"}]}
                )
            if parsed.path == "/v1beta1/options/snapshots":
                return _Response({"snapshots": {}})
            raise AssertionError(req.full_url)

        client = AlpacaMarketDataClient("key", "secret", opener=fake_open)

        bars = client.fetch_stock_bars(
            "AAPL",
            timeframe="1Day",
            start="2026-05-20T00:00:00+00:00",
        )
        contracts = client.fetch_option_contracts(
            "AAPL",
            option_type="put",
            expiration_date_gte=date(2026, 5, 21),
            expiration_date_lte=date(2026, 5, 29),
        )
        snapshots = client.fetch_option_snapshots(["AAPL260522P00275000"])

        self.assertEqual(len(bars), 2)
        self.assertEqual([c["symbol"] for c in contracts], ["AAPL260522P00275000", "AAPL260529P00270000"])
        self.assertEqual(snapshots, {})


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
