from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from wheels_copilot.market_data import (
    _dates_from_frame,
    _net_income_values,
    _recent_move_pct,
    fetch_fundamental_snapshot,
    fetch_put_chain,
)
from wheels_copilot.models import PriceBar


class MarketDataTests(unittest.TestCase):
    def test_fetch_put_chain_returns_all_expirations_inside_dte_window(self):
        fake = _FakeTicker()

        with patch("wheels_copilot.market_data.yf.Ticker", return_value=fake):
            options = fetch_put_chain("TEST", dte_min=1, dte_max=9, as_of=date(2026, 5, 20))

        expirations = {opt.expiration for opt in options}
        self.assertEqual(expirations, {date(2026, 5, 22), date(2026, 5, 29)})
        self.assertEqual(len(options), 2)

    def test_fetch_put_chain_continues_when_one_expiration_fails(self):
        fake = _FakeTicker(fail_expiration="2026-05-22")

        with patch("wheels_copilot.market_data.yf.Ticker", return_value=fake):
            with self.assertLogs("wheels_copilot.market_data", level="WARNING"):
                options = fetch_put_chain("TEST", dte_min=1, dte_max=9, as_of=date(2026, 5, 20))

        self.assertEqual([opt.expiration for opt in options], [date(2026, 5, 29)])

    def test_zero_implied_volatility_is_treated_as_missing(self):
        fake = _FakeTicker(iv=0.0)

        with patch("wheels_copilot.market_data.yf.Ticker", return_value=fake):
            options = fetch_put_chain("TEST", dte_min=1, dte_max=2, as_of=date(2026, 5, 20))

        self.assertIsNone(options[0].implied_volatility)

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


class _FakeTicker:
    options = ("2026-05-22", "2026-05-29", "2026-06-19")

    def __init__(self, fail_expiration: str | None = None, iv: float = 0.25):
        self.fail_expiration = fail_expiration
        self.iv = iv

    def option_chain(self, raw: str):
        if raw == self.fail_expiration:
            raise RuntimeError("rate limited")
        strike = {"2026-05-22": 90, "2026-05-29": 85, "2026-06-19": 80}[raw]
        return SimpleNamespace(
            puts=pd.DataFrame(
                [
                    {
                        "contractSymbol": f"TEST{raw}P{strike}",
                        "strike": strike,
                        "bid": 1.0,
                        "ask": 1.2,
                        "lastPrice": 1.1,
                        "impliedVolatility": self.iv,
                        "openInterest": 100,
                        "volume": 10,
                    }
                ]
            )
        )


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
