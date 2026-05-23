from __future__ import annotations

import tempfile
import unittest
import csv
import gzip
from datetime import date
from pathlib import Path
from unittest.mock import patch

from wheels_copilot.execution_price import build_backtest_execution_model
from wheels_copilot.historical_data import (
    FlatFilesStore,
    WarmCacheMiss,
    black_scholes_call_price,
    black_scholes_put_price,
    detect_price_space_breaks,
    infer_put_iv,
    infer_call_iv,
    parse_option_symbol,
    read_json_if_exists,
    write_parquet_rows_atomic,
    write_json_atomic,
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

    def test_cache_json_writer_cleans_temp_and_corrupt_reader_quarantines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"

            write_json_atomic(path, {"ok": True})
            self.assertEqual(read_json_if_exists(path), {"ok": True})
            self.assertFalse(list(Path(tmp).glob("cache.json.tmp.*")))

            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_json_if_exists(path))
            self.assertFalse(path.exists())
            self.assertEqual(len(list(Path(tmp).glob("cache.json.corrupt.*"))), 1)

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

    def test_option_chain_applies_synthetic_spread_execution_model(self):
        store = _MemoryFlatFilesStore(
            rows=[
                {
                    "ticker": "O:AAPL260109P00090000",
                    "open": "1.00",
                    "close": "1.10",
                    "volume": "100",
                },
            ]
        )
        execution_model = build_backtest_execution_model(
            {
                "backtest_execution": {
                    "model": "day_agg_synthetic_spread",
                    "fill_policy": "mid",
                    "synthetic_spread": {
                        "min_spread_pct_of_mid": 0.10,
                        "max_spread_pct_of_mid": 0.10,
                    },
                }
            },
            slippage_pct=0.0,
        )

        options = store.option_chain(
            "AAPL",
            date(2026, 1, 5),
            dte_min=1,
            dte_max=9,
            stock_price=110,
            execution_model=execution_model,
        )

        self.assertEqual(len(options), 1)
        self.assertAlmostEqual(options[0].bid, 0.95)
        self.assertAlmostEqual(options[0].ask, 1.05)
        self.assertAlmostEqual(options[0].mid, 1.0)
        self.assertAlmostEqual(options[0].spread_pct_of_mid or 0.0, 0.10)

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

    def test_stock_day_rows_builds_daily_indexed_cache_and_strict_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2026, 1, 5)
            store = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw",
                indexed_cache_dir=root / "indexed",
            )
            _write_raw_csv(
                store.raw_cache_path("stocks-day-aggs", day),
                [
                    {
                        "ticker": "AAPL",
                        "open": "100",
                        "high": "101",
                        "low": "99",
                        "close": "100.50",
                        "volume": "123",
                    },
                    {
                        "ticker": "MSFT",
                        "open": "200",
                        "high": "202",
                        "low": "199",
                        "close": "201",
                        "volume": "456",
                    },
                ],
            )

            rows = store.stock_day_rows(day, {"AAPL"})

            self.assertEqual(rows["AAPL"]["close"], 100.5)
            self.assertTrue(store.indexed_cache_path("stocks-day-aggs", day).exists())
            strict = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw_missing_on_purpose",
                indexed_cache_dir=root / "indexed",
                require_warm_cache=True,
            )
            self.assertEqual(strict.stock_day_rows(day, {"AAPL"})["AAPL"]["open"], 100.0)
            with self.assertRaises(WarmCacheMiss):
                strict.stock_day_rows(day, {"MSFT"})

    def test_option_day_rows_merges_daily_parquet_by_option_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2026, 1, 5)
            store = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw",
                indexed_cache_dir=root / "indexed",
            )
            _write_raw_csv(
                store.raw_cache_path("options-day-aggs", day),
                [
                    {
                        "ticker": "O:AAPL260109P00090000",
                        "open": "1.00",
                        "close": "1.10",
                        "volume": "10",
                    },
                    {
                        "ticker": "O:AAPL260109C00120000",
                        "open": "0.50",
                        "close": "0.55",
                        "volume": "20",
                    },
                    {
                        "ticker": "O:MSFT260109P00200000",
                        "open": "2.00",
                        "close": "2.10",
                        "volume": "30",
                    },
                ],
            )

            puts = store.option_day_rows(day, {"AAPL"}, option_type="put")
            calls = store.option_day_rows(day, {"AAPL"}, option_type="call")

            self.assertEqual(len(puts["AAPL"]), 1)
            self.assertEqual(len(calls["AAPL"]), 1)
            strict = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw_missing_on_purpose",
                indexed_cache_dir=root / "indexed",
                require_warm_cache=True,
            )
            self.assertEqual(
                strict.option_day_rows(day, {"AAPL"}, option_type="put")["AAPL"][0][
                    "strike"
                ],
                90.0,
            )
            with self.assertRaises(WarmCacheMiss):
                strict.option_day_rows(day, {"MSFT"}, option_type="put")

    def test_strict_missing_indexed_cache_does_not_call_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw",
                indexed_cache_dir=root / "indexed",
                require_warm_cache=True,
            )

            with patch("wheels_copilot.historical_data.subprocess.run") as run:
                with self.assertRaises(WarmCacheMiss):
                    store.stock_day_rows(date(2026, 1, 5), {"AAPL"})

            run.assert_not_called()

    def test_option_zero_row_underlying_is_covered_by_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2026, 1, 5)
            store = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw",
                indexed_cache_dir=root / "indexed",
            )
            _write_raw_csv(
                store.raw_cache_path("options-day-aggs", day),
                [
                    {
                        "ticker": "O:AAPL260109P00090000",
                        "open": "1.00",
                        "close": "1.10",
                        "volume": "10",
                    },
                ],
            )

            rows = store.option_day_rows(day, {"MSFT"}, option_type="put")

            self.assertEqual(rows, {"MSFT": []})
            strict = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw_missing_on_purpose",
                indexed_cache_dir=root / "indexed",
                require_warm_cache=True,
            )
            self.assertEqual(strict.option_day_rows(day, {"MSFT"}, option_type="put"), {"MSFT": []})

    def test_indexed_parquet_without_manifest_is_not_treated_as_warm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2026, 1, 5)
            store = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw",
                indexed_cache_dir=root / "indexed",
            )
            write_parquet_rows_atomic(
                store.indexed_cache_path("options-day-aggs", day),
                [
                    {
                        "ticker": "O:AAPL260109P00090000",
                        "underlying": "AAPL",
                        "option_type": "put",
                        "strike": 90.0,
                    }
                ],
                default_columns=["ticker", "underlying", "option_type", "strike"],
            )
            strict = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw_missing_on_purpose",
                indexed_cache_dir=root / "indexed",
                require_warm_cache=True,
            )

            with self.assertRaises(WarmCacheMiss):
                strict.option_day_rows(day, {"AAPL"}, option_type="put")

    def test_indexed_missing_sentinel_returns_empty_option_chains_in_strict_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2026, 1, 5)
            setup = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw",
                indexed_cache_dir=root / "indexed",
            )
            write_json_atomic(
                setup.indexed_missing_path("options-day-aggs", day),
                {"missing": True},
            )
            strict = FlatFilesStore(
                cache_dir=root / "legacy",
                raw_cache_dir=root / "raw_missing_on_purpose",
                indexed_cache_dir=root / "indexed",
                require_warm_cache=True,
            )

            self.assertEqual(
                strict.option_day_rows(day, {"AAPL", "MSFT"}, option_type="put"),
                {"AAPL": [], "MSFT": []},
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


def _write_raw_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = sorted({key for row in rows for key in row})
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
