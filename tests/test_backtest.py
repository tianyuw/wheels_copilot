from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from wheels_copilot.backtest import run_backtest
from wheels_copilot.config import load_config
from wheels_copilot.models import (
    OptionQuote,
    PriceBar,
    SupportAnalysis,
    SupportZone,
    TrendCheck,
)


class BacktestRunnerTests(unittest.TestCase):
    def test_short_put_expires_worthless(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertEqual(result["summary"]["expired_worthless"], 1)
        self.assertEqual(result["summary"]["assigned"], 0)
        self.assertGreater(result["summary"]["ending_equity"], 500000)
        self.assertEqual(result["trades"][0]["status"], "EXPIRED_WORTHLESS")

    def test_short_put_assignment_creates_stock_position(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110, close_overrides={end: 90})},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["assigned"], 1)
        self.assertEqual(result["open_positions"]["stocks"][0]["ticker"], "AAPL")
        self.assertEqual(result["open_positions"]["stocks"][0]["shares"], 100)
        self.assertEqual(result["trades"][0]["status"], "ASSIGNED")

    def test_open_short_put_reserves_assignment_cash_before_expiration(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 6)
        expiration = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, expiration, close=110)},
            options={("AAPL", start): [_put(expiration=expiration, strike=95)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["open_short_puts"], 1)
        self.assertEqual(result["summary"]["reserved_assignment_cash"], 9500)

    def test_zero_volume_options_do_not_open_trade(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [_put(expiration=end, strike=95, volume=0)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertEqual(result["summary"]["rejected_reason_counts"]["no_fillable_put_options"], 5)

    def test_split_guard_blocks_affected_ticker(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        bars = _bars(start, end, close=110)
        bars[10] = PriceBar(date=bars[10].date, open=55, high=56, low=54, close=55)
        data = _FakeData(
            bars={"AAPL": bars},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )

        result = run_backtest(
            config=_config(),
            data=data,
            universe=["AAPL"],
            start=start,
            end=end,
            slippage_pct=0.0,
        )

        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertGreaterEqual(result["summary"]["data_issue_count"], 1)
        self.assertEqual(result["data_issues"][0]["type"], "price_space_break")

    def test_future_split_guard_does_not_block_entries_before_break(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        split_day = date(2026, 1, 8)
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    end,
                    close=110,
                    close_overrides={split_day: 55, end: 55},
                )
            },
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertGreaterEqual(
            result["summary"]["data_issue_counts"]["price_space_break"], 1
        )

    def test_cash_secured_capacity_blocks_second_put(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 6)
        expiration = date(2026, 1, 9)
        config = _config()
        config["account"]["starting_equity"] = 15000
        config["execution"]["max_orders_per_run"] = 3
        config["risk"]["max_assignment_cash_pct"] = 1.0
        config["risk"]["min_cash_buffer_pct"] = 0.0
        config["risk"]["max_single_ticker_assignment_pct"] = 1.0
        data = _FakeData(
            bars={
                "AAPL": _bars(start, expiration, close=110),
                "MSFT": _bars(start, expiration, close=110),
            },
            options={
                ("AAPL", start): [_put(ticker="AAPL", expiration=expiration, strike=95)],
                ("MSFT", start): [_put(ticker="MSFT", expiration=expiration, strike=95)],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL", "MSFT"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertIn(
            "insufficient_cash_secured_capacity",
            result["summary"]["rejected_reason_counts"],
        )

    def test_missing_expiration_close_does_not_settle_from_prior_close(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        bars = [bar for bar in _bars(start, end, close=110) if bar.date != end]
        data = _FakeData(
            bars={"AAPL": bars},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["expired_worthless"], 0)
        self.assertEqual(result["summary"]["assigned"], 0)
        self.assertEqual(result["summary"]["open_short_puts"], 1)
        self.assertTrue(
            any(
                event["type"] == "EXPIRATION_MISSING_UNDERLYING_CLOSE"
                for event in result["events"]
            )
        )


class _FakeData:
    def __init__(
        self,
        *,
        bars: dict[str, list[PriceBar]],
        options: dict[tuple[str, date], list[OptionQuote]],
        marks: dict[tuple[str, date], OptionQuote],
    ) -> None:
        self.bars = bars
        self.options = options
        self.marks = marks

    def trading_days(self, start: date, end: date) -> list[date]:
        days = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        return days

    def load_stock_bars(
        self, tickers: list[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        return {
            ticker: [bar for bar in self.bars.get(ticker, []) if start <= bar.date <= end]
            for ticker in tickers
        }

    def option_chain(
        self,
        underlying: str,
        as_of: date,
        *,
        dte_min: int,
        dte_max: int,
        option_type: str = "put",
        price_field: str = "open",
        slippage_pct: float = 0.0,
        risk_free_rate: float = 0.04,
        stock_price: float | None = None,
    ) -> list[OptionQuote]:
        options = self.options.get((underlying, as_of), [])
        return [
            option
            for option in options
            if dte_min <= option.dte <= dte_max and (option.volume or 0) > 0
        ]

    def option_mark(
        self,
        symbol: str,
        as_of: date,
        *,
        price_field: str = "close",
        stock_price: float | None = None,
        risk_free_rate: float = 0.04,
    ) -> OptionQuote | None:
        return self.marks.get((symbol, as_of))


def _config() -> dict:
    config = load_config("config/markus_wheel.yaml")
    config["execution"]["max_orders_per_run"] = 1
    return config


def _support() -> SupportAnalysis:
    zone = SupportZone(
        method="test",
        center=100,
        bottom=100,
        top=102,
        touches=3,
        rejections=3,
        score=90,
    )
    return SupportAnalysis(
        trend=TrendCheck(passed=True, current_price=110, sma200=100, sma200_slope=1),
        zones=[zone],
        selected_zone=zone,
        atr14=2,
        current_price=110,
        min_score_to_trade=70,
    )


def _put(
    *,
    ticker: str = "AAPL",
    expiration: date,
    strike: float,
    volume: int = 100,
    bid: float = 1.0,
    ask: float = 1.0,
) -> OptionQuote:
    return OptionQuote(
        symbol=f"{ticker}{expiration:%y%m%d}P{int(strike * 1000):08d}",
        expiration=expiration,
        dte=max((expiration - date(2026, 1, 5)).days, 1),
        strike=strike,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        implied_volatility=0.25,
        open_interest=500,
        volume=volume,
        delta=-0.2,
    )


def _bars(
    start: date,
    end: date,
    *,
    close: float,
    close_overrides: dict[date, float] | None = None,
) -> list[PriceBar]:
    close_overrides = close_overrides or {}
    first = start - timedelta(days=80)
    bars: list[PriceBar] = []
    current = first
    while current <= end:
        if current.weekday() < 5:
            value = close_overrides.get(current, close)
            bars.append(
                PriceBar(
                    date=current,
                    open=value,
                    high=value + 1,
                    low=value - 1,
                    close=value,
                    volume=1_000_000,
                )
            )
        current += timedelta(days=1)
    return bars


if __name__ == "__main__":
    unittest.main()
