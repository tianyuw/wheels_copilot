from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from wheels_copilot.historical_data import (
    FlatFilesStore,
    black_scholes_call_price,
    black_scholes_put_price,
    detect_price_space_breaks,
    infer_put_iv,
    infer_call_iv,
    parse_option_symbol,
)
from wheels_copilot.models import PriceBar


class HistoricalDataTests(unittest.TestCase):
    def test_parse_option_symbol_handles_massive_format(self):
        parsed = parse_option_symbol("O:GDXJ260109P00046500")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.underlying, "GDXJ")
        self.assertEqual(parsed.expiration, date(2026, 1, 9))
        self.assertEqual(parsed.option_type, "put")
        self.assertEqual(parsed.strike, 46.5)

    def test_detect_price_space_breaks_flags_split_like_jump(self):
        bars = [
            PriceBar(date(2026, 1, 5), open=100, high=101, low=99, close=100),
            PriceBar(date(2026, 1, 6), open=50, high=51, low=49, close=50),
        ]

        issues = detect_price_space_breaks(bars)

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["date"], "2026-01-06")

    def test_cache_preflight_writes_reads_and_deletes_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FlatFilesStore(cache_dir=Path(tmp))

            result = store.preflight_cache()

            self.assertTrue(result.ok, result.reason)
            self.assertFalse((Path(tmp) / ".preflight" / "write_probe.txt").exists())

    def test_option_chain_filters_zero_volume_contracts(self):
        store = _MemoryFlatFilesStore(
            rows=[
                {
                    "ticker": "O:AAPL260109P00095000",
                    "open": "1.00",
                    "close": "1.10",
                    "volume": "0",
                },
                {
                    "ticker": "O:AAPL260109P00090000",
                    "open": "0.75",
                    "close": "0.80",
                    "volume": "10",
                },
            ]
        )

        options = store.option_chain(
            "AAPL",
            date(2026, 1, 5),
            dte_min=1,
            dte_max=9,
            stock_price=110,
        )

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].symbol, "AAPL260109P00090000")
        self.assertEqual(options[0].volume, 10)

    def test_option_day_memory_cache_keeps_only_current_day(self):
        store = FlatFilesStore(cache_dir=Path(tempfile.gettempdir()))

        store._remember_option_day_rows(
            date(2026, 1, 5),
            {"AAPL"},
            option_type="put",
            chains={"AAPL": [{"ticker": "O:AAPL260109P00095000"}]},
        )
        store._remember_option_day_rows(
            date(2026, 1, 6),
            {"AAPL"},
            option_type="put",
            chains={"AAPL": [{"ticker": "O:AAPL260109P00094000"}]},
        )

        self.assertEqual(
            {key[0] for key in store._option_day_memory_cache},
            {date(2026, 1, 6)},
        )
        self.assertIsNotNone(
            store._option_day_memory_hit(
                date(2026, 1, 6),
                {"AAPL"},
                option_type="put",
            )
        )

    def test_infer_put_iv_rejects_prices_below_lower_bound(self):
        minimum_price = black_scholes_put_price(
            stock_price=50,
            strike=100,
            dte=7,
            implied_volatility=0.0001,
            risk_free_rate=0.04,
        )
        self.assertIsNotNone(minimum_price)
        assert minimum_price is not None

        iv = infer_put_iv(
            price=minimum_price - 0.01,
            stock_price=50,
            strike=100,
            dte=7,
            risk_free_rate=0.04,
        )

        self.assertIsNone(iv)

    def test_infer_call_iv_returns_delta_usable_value(self):
        price = black_scholes_call_price(
            stock_price=100,
            strike=105,
            dte=7,
            implied_volatility=0.35,
            risk_free_rate=0.04,
        )
        self.assertIsNotNone(price)
        assert price is not None

        iv = infer_call_iv(
            price=price,
            stock_price=100,
            strike=105,
            dte=7,
            risk_free_rate=0.04,
        )

        self.assertIsNotNone(iv)
        assert iv is not None
        self.assertAlmostEqual(iv, 0.35, places=3)


class _MemoryFlatFilesStore(FlatFilesStore):
    def __init__(self, rows: list[dict[str, str]]):
        super().__init__(cache_dir=Path(tempfile.gettempdir()))
        self.rows = rows

    def option_day_rows(self, day, underlyings, *, option_type="put"):
        return {"AAPL": self.rows}


if __name__ == "__main__":
    unittest.main()
