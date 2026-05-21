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


class _FakeTicker:
    options = ("2026-05-22", "2026-05-29", "2026-06-19")

    def option_chain(self, raw: str):
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
                        "impliedVolatility": 0.25,
                        "openInterest": 100,
                        "volume": 10,
                    }
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
