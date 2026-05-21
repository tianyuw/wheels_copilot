from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from wheels_copilot.market_data import fetch_put_chain


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


if __name__ == "__main__":
    unittest.main()
