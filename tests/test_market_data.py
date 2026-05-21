from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd

from wheels_copilot.market_data import (
    _dates_from_frame,
    _net_income_values,
    _period_start,
    _recent_move_pct,
    fetch_fundamental_snapshot,
    fetch_daily_bars,
    fetch_put_chain,
)
from wheels_copilot.models import PriceBar


class MarketDataTests(unittest.TestCase):
    def test_fetch_daily_bars_uses_alpaca_sip_client(self):
        client = _FakeAlpacaMarketDataClient()

        bars = fetch_daily_bars("TEST", period="1y", client=client)

        self.assertEqual(client.stock_bar_calls[0]["symbol"], "TEST")
        self.assertEqual(client.stock_bar_calls[0]["timeframe"], "1Day")
        self.assertEqual(bars[0].date, date(2026, 5, 20))
        self.assertEqual(bars[0].close, 101.0)

    def test_fetch_put_chain_returns_alpaca_opra_snapshots_inside_dte_window(self):
        client = _FakeAlpacaMarketDataClient()

        options = fetch_put_chain(
            "TEST",
            dte_min=1,
            dte_max=9,
            as_of=date(2026, 5, 20),
            client=client,
        )

        expirations = {opt.expiration for opt in options}
        self.assertEqual(
            expirations,
            {date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 29)},
        )
        self.assertEqual(len(options), 3)
        self.assertEqual(client.contract_calls[0]["option_type"], "put")
        self.assertEqual(client.contract_calls[0]["expiration_date_gte"], date(2026, 5, 21))
        self.assertEqual(client.contract_calls[0]["expiration_date_lte"], date(2026, 5, 29))
        self.assertEqual(
            client.snapshot_calls[0],
            ["TEST260521P00095000", "TEST260522P00090000", "TEST260529P00085000"],
        )
        self.assertEqual(options[0].symbol, "TEST260521P00095000")
        self.assertEqual(options[0].bid, 1.0)
        self.assertEqual(options[0].ask, 1.2)
        self.assertEqual(options[0].last, 1.1)
        self.assertEqual(options[0].delta, -0.2)
        self.assertEqual(options[0].open_interest, 0)
        self.assertEqual(options[0].volume, 0)
        self.assertEqual(options[0].data_feed, "opra")
        self.assertEqual(
            options[0].quote_timestamp,
            datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        )

    def test_zero_implied_volatility_is_treated_as_missing(self):
        client = _FakeAlpacaMarketDataClient(iv=0.0)

        options = fetch_put_chain(
            "TEST",
            dte_min=1,
            dte_max=2,
            as_of=date(2026, 5, 20),
            client=client,
        )

        self.assertIsNone(options[0].implied_volatility)

    def test_fetch_put_chain_filters_stale_and_invalid_quotes(self):
        client = _FakeAlpacaMarketDataClient(quote_timestamp="2026-05-20T14:30:00Z")

        options = fetch_put_chain(
            "TEST",
            dte_min=1,
            dte_max=2,
            as_of=date(2026, 5, 20),
            config={"market_data": {"max_option_quote_age_seconds": 30}},
            client=client,
        )

        self.assertEqual(options, [])

        invalid_client = _FakeAlpacaMarketDataClient(bid=1.2, ask=1.0)
        options = fetch_put_chain(
            "TEST",
            dte_min=1,
            dte_max=2,
            as_of=date(2026, 5, 20),
            client=invalid_client,
        )

        self.assertEqual(options, [])

    def test_fetch_put_chain_propagates_snapshot_batch_failures(self):
        client = _FakeAlpacaMarketDataClient(fail_snapshots=True)

        with self.assertRaises(RuntimeError):
            fetch_put_chain(
                "TEST",
                dte_min=1,
                dte_max=9,
                as_of=date(2026, 5, 20),
                client=client,
            )

    def test_period_start_uses_calendar_periods(self):
        self.assertEqual(_period_start("5d", date(2026, 5, 20)), date(2026, 5, 15))
        self.assertEqual(_period_start("3mo", date(2026, 5, 31)), date(2026, 2, 28))
        self.assertEqual(_period_start("1y", date(2026, 5, 20)), date(2025, 5, 20))

    def test_dates_from_frame_handles_calendar_list_values(self):
        frame = pd.DataFrame(
            [
                {
                    "Earnings Date": [date(2026, 7, 30)],
                    "Dividend Date": date(2026, 5, 13),
                }
            ]
        )

        self.assertEqual(
            _dates_from_frame(frame),
            [date(2026, 7, 30)],
        )

    def test_dates_from_frame_ignores_partial_date_strings(self):
        frame = pd.DataFrame(
            [
                {
                    "Earnings Date": ["2026", "Q3 2026", "2026-07-30"],
                }
            ]
        )

        self.assertEqual(_dates_from_frame(frame), [date(2026, 7, 30)])

    def test_fetch_fundamental_snapshot_normalizes_dividend_yield_and_keeps_negative_pe(self):
        fake = _FakeFundamentalTicker(
            info={
                "quoteType": "EQUITY",
                "shortName": "Test Corp",
                "dividendYield": 6.63,
                "trailingPE": -4.5,
            }
        )

        with patch("wheels_copilot.market_data.yf.Ticker", return_value=fake):
            snapshot = fetch_fundamental_snapshot("TEST", bars=[], as_of=date(2026, 5, 20))

        self.assertAlmostEqual(snapshot.dividend_yield, 0.0663)
        self.assertEqual(snapshot.pe_ratio, -4.5)

    def test_net_income_values_are_sorted_newest_first(self):
        frame = pd.DataFrame(
            [[10, 30, 20]],
            index=["Net Income"],
            columns=[
                pd.Timestamp("2024-12-31"),
                pd.Timestamp("2026-12-31"),
                pd.Timestamp("2025-12-31"),
            ],
        )

        self.assertEqual(_net_income_values(frame, limit=3), [30, 20, 10])

    def test_recent_move_uses_window_start_not_drawdown_low(self):
        recovery = [
            _bar(date(2026, 1, 1), 100),
            _bar(date(2026, 1, 2), 50),
            _bar(date(2026, 1, 3), 100),
        ]
        runup = [
            _bar(date(2026, 1, 1), 100),
            _bar(date(2026, 1, 2), 150),
            _bar(date(2026, 1, 3), 205),
        ]

        self.assertEqual(_recent_move_pct(recovery), 0)
        self.assertEqual(_recent_move_pct(runup), 105)


class _FakeAlpacaMarketDataClient:
    option_feed = "opra"

    def __init__(
        self,
        iv: float = 0.25,
        fail_snapshots: bool = False,
        bid: float = 1.0,
        ask: float = 1.2,
        quote_timestamp: str = "2026-05-20T14:30:00Z",
    ):
        self.iv = iv
        self.fail_snapshots = fail_snapshots
        self.bid = bid
        self.ask = ask
        self.quote_timestamp = quote_timestamp
        self.stock_bar_calls = []
        self.contract_calls = []
        self.snapshot_calls = []

    def fetch_stock_bars(self, symbol: str, **kwargs):
        self.stock_bar_calls.append({"symbol": symbol, **kwargs})
        return [
            {
                "t": "2026-05-20T04:00:00Z",
                "o": 100,
                "h": 102,
                "l": 99,
                "c": 101,
                "v": 1_000_000,
            }
        ]

    def fetch_option_contracts(self, underlying_symbol: str, **kwargs):
        self.contract_calls.append({"underlying_symbol": underlying_symbol, **kwargs})
        contracts = [
            {
                "symbol": "TEST260521P00095000",
                "expiration_date": "2026-05-21",
                "strike_price": "95",
                "type": "put",
                "tradable": True,
            },
            {
                "symbol": "TEST260522P00090000",
                "expiration_date": "2026-05-22",
                "strike_price": "90",
                "type": "put",
                "tradable": True,
            },
            {
                "symbol": "TEST260529P00085000",
                "expiration_date": "2026-05-29",
                "strike_price": "85",
                "type": "put",
                "tradable": True,
            },
            {
                "symbol": "TEST260619P00080000",
                "expiration_date": "2026-06-19",
                "strike_price": "80",
                "type": "put",
                "tradable": True,
            },
        ]
        start = kwargs["expiration_date_gte"]
        end = kwargs["expiration_date_lte"]
        return [
            contract
            for contract in contracts
            if start <= date.fromisoformat(contract["expiration_date"]) <= end
        ]

    def fetch_option_snapshots(self, symbols: list[str]):
        self.snapshot_calls.append(list(symbols))
        if self.fail_snapshots:
            raise RuntimeError("rate limited")
        return {
            symbol: {
                "latestQuote": {
                    "t": self.quote_timestamp,
                    "bp": self.bid,
                    "ap": self.ask,
                },
                "latestTrade": {
                    "t": "2026-05-20T14:29:59Z",
                    "p": 1.1,
                },
                "impliedVolatility": self.iv,
                "greeks": {"delta": -0.2},
                "dailyBar": {"v": 0},
                "openInterest": 0,
            }
            for symbol in symbols
        }


class _FakeFundamentalTicker:
    quarterly_income_stmt = pd.DataFrame()
    income_stmt = pd.DataFrame()
    calendar = {}

    def __init__(self, info: dict):
        self._info = info

    def get_info(self):
        return self._info

    def get_earnings_dates(self, limit: int):
        return pd.DataFrame()


def _bar(day: date, close: float) -> PriceBar:
    return PriceBar(
        date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000,
    )


if __name__ == "__main__":
    unittest.main()
