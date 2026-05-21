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
    account_identity_reasons,
    fetch_alpaca_portfolio_snapshot,
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
                        "id": "acct-1",
                        "account_number": "PA123",
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
        self.assertEqual(snapshot.account.account_id, "acct-1")
        self.assertEqual(snapshot.account.account_number, "PA123")
        self.assertEqual(snapshot.account.status, "ACTIVE")
        self.assertEqual(snapshot.account.cash, 425000)
        self.assertEqual(snapshot.positions[0].underlying_symbol, "AAPL")
        self.assertEqual(snapshot.positions[0].assignment_cash_required, 27500)
        self.assertEqual(snapshot.positions[1].symbol, "UPS")
        self.assertEqual(snapshot.positions[2].symbol, "TSLA")
        self.assertEqual(snapshot.positions[2].qty, -25)
        self.assertEqual(snapshot.open_orders[0].underlying_symbol, "MSFT")
        self.assertEqual(snapshot.open_orders[0].assignment_cash_required, 30000)

    def test_trading_client_fetches_clock_and_submits_order(self):
        calls = []

        def fake_open(req, timeout):
            calls.append((req.get_method(), req.full_url, req.data))
            if req.full_url.endswith("/v2/clock"):
                return _Response(
                    {
                        "is_open": True,
                        "timestamp": "2026-05-20T17:00:00Z",
                        "next_close": "2026-05-20T20:00:00Z",
                    }
                )
            if req.full_url.endswith("/v2/orders/alpaca-1"):
                return _Response(
                    {
                        "id": "alpaca-1",
                        "status": "filled",
                        "filled_avg_price": "1.05",
                    }
                )
            if "/v2/orders:by_client_order_id?" in req.full_url:
                parsed = urlparse(req.full_url)
                query = parse_qs(parsed.query)
                self.assertEqual(query["client_order_id"], ["client-1"])
                return _Response(
                    {
                        "id": "alpaca-1",
                        "status": "accepted",
                        "client_order_id": "client-1",
                    }
                )
            if req.full_url.endswith("/v2/orders"):
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body["symbol"], "AAPL260529P00090000")
                self.assertEqual(body["client_order_id"], "client-1")
                self.assertEqual(req.headers["Content-type"], "application/json")
                return _Response(
                    {
                        "id": "alpaca-1",
                        "status": "accepted",
                        "client_order_id": body["client_order_id"],
                    }
                )
            raise AssertionError(req.full_url)

        client = AlpacaTradingClient("key", "secret", opener=fake_open)

        clock = client.fetch_clock()
        order = client.submit_order(
            {
                "symbol": "AAPL260529P00090000",
                "qty": "1",
                "side": "sell",
                "type": "limit",
                "time_in_force": "day",
                "limit_price": "1.10",
                "position_intent": "sell_to_open",
                "client_order_id": "client-1",
            }
        )
        fetched = client.fetch_order("alpaca-1")
        fetched_by_client_id = client.fetch_order_by_client_order_id("client-1")

        self.assertTrue(clock["is_open"])
        self.assertEqual(order["id"], "alpaca-1")
        self.assertEqual(fetched["status"], "filled")
        self.assertEqual(fetched_by_client_id["id"], "alpaca-1")
        self.assertEqual([call[0] for call in calls], ["GET", "POST", "GET", "GET"])

    def test_account_identity_reasons_use_expected_env_values(self):
        account = AlpacaTradingClient(
            "key",
            "secret",
            opener=lambda req, timeout: _Response(
                {
                    "id": "acct-1",
                    "account_number": "PA123",
                    "status": "ACTIVE",
                    "equity": "500000",
                    "cash": "500000",
                }
            ),
        ).fetch_account_snapshot()
        cfg = {
            "alpaca": {
                "expected_account_id_env": "EXPECTED_ACCOUNT_ID",
                "expected_account_number_env": "EXPECTED_ACCOUNT_NUMBER",
            }
        }

        self.assertEqual(
            account_identity_reasons(
                cfg,
                account,
                env={
                    "EXPECTED_ACCOUNT_ID": "acct-1",
                    "EXPECTED_ACCOUNT_NUMBER": "PA123",
                },
            ),
            [],
        )
        reasons = account_identity_reasons(
            cfg,
            account,
            env={
                "EXPECTED_ACCOUNT_ID": "other",
                "EXPECTED_ACCOUNT_NUMBER": "PA999",
            },
        )
        self.assertIn("account_id_mismatch:actual=acct-1", reasons)
        self.assertIn("account_number_mismatch:actual=PA123", reasons)

    def test_fetch_portfolio_snapshot_fails_on_account_identity_mismatch(self):
        def fake_open(req, timeout):
            if req.full_url.endswith("/v2/account"):
                return _Response(
                    {
                        "id": "acct-1",
                        "account_number": "PA123",
                        "status": "ACTIVE",
                        "equity": "500000",
                        "cash": "500000",
                    }
                )
            if req.full_url.endswith("/v2/positions"):
                return _Response([])
            if "/v2/orders?" in req.full_url:
                return _Response([])
            raise AssertionError(req.full_url)

        cfg = {
            "alpaca": {
                "api_key_env": "K",
                "secret_key_env": "S",
                "expected_account_id_env": "EXPECTED_ACCOUNT_ID",
            }
        }
        client = AlpacaTradingClient("key", "secret", opener=fake_open)
        with self.assertRaises(AlpacaConfigError):
            fetch_alpaca_portfolio_snapshot(
                cfg,
                env={
                    "K": "key",
                    "S": "secret",
                    "EXPECTED_ACCOUNT_ID": "wrong",
                },
                client=client,
            )

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
            if "/v1beta1/options/quotes/latest?" in req.full_url:
                self.assertIn("feed=opra", req.full_url)
                return _Response(
                    {"quotes": {"AAPL260522P00275000": {"bp": 1, "ap": 1.1}}}
                )
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
        quotes = client.fetch_option_latest_quotes(["AAPL260522P00275000"])

        self.assertEqual(bars, [{"t": "2026-05-20T04:00:00Z", "c": 1}])
        self.assertEqual(contracts[0]["symbol"], "AAPL260522P00275000")
        self.assertEqual(snapshots, {"AAPL260522P00275000": {}})
        self.assertEqual(quotes, {"AAPL260522P00275000": {"bp": 1, "ap": 1.1}})
        self.assertEqual(len(calls), 4)

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
