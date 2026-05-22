from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from wheels_copilot.backtest import StockPosition, _min_covered_call_strike, run_backtest
from wheels_copilot.config import load_config
from wheels_copilot.historical_data import parse_option_symbol
from wheels_copilot.models import (
    FundamentalFieldProvenance,
    FundamentalSnapshot,
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

    def test_backtest_records_execution_model_and_mid_fill(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [_put(expiration=end, strike=95, bid=0.90, ask=1.10)]},
            marks={},
        )
        config = _config()
        config["csp_selector"]["max_spread_pct_of_mid"] = 0.25

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        trade = result["trades"][0]
        self.assertEqual(result["summary"]["execution_model"], "day_agg_synthetic_spread")
        self.assertEqual(result["summary"]["execution_fill_policy"], "mid")
        self.assertAlmostEqual(trade["entry_price"], 1.0)
        self.assertAlmostEqual(trade["entry_market_bid"], 0.90)
        self.assertAlmostEqual(trade["entry_market_ask"], 1.10)
        self.assertAlmostEqual(trade["entry_spread_pct_of_mid"], 0.20)

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

    def test_assignment_opens_covered_call_next_trading_day(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    call_expiration,
                    close=110,
                    close_overrides={put_expiration: 90},
                )
            },
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["assigned"], 1)
        self.assertEqual(result["summary"]["opened_covered_calls"], 1)
        self.assertEqual(result["summary"]["open_short_calls"], 1)
        self.assertEqual(result["open_positions"]["short_calls"][0]["ticker"], "AAPL")

    def test_covered_call_expires_worthless_and_keeps_stock(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    call_expiration,
                    close=98,
                    close_overrides={put_expiration: 90, call_scan: 96},
                )
            },
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_expiration,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["expired_covered_calls"], 1)
        self.assertEqual(result["summary"]["called_away"], 0)
        self.assertEqual(result["open_positions"]["stocks"][0]["shares"], 100)
        self.assertEqual(result["open_positions"]["short_calls"], [])

    def test_covered_call_called_away_removes_stock_and_realizes_stock_pnl(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    call_expiration,
                    close=105,
                    close_overrides={put_expiration: 90, call_scan: 96},
                )
            },
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_expiration,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["called_away"], 1)
        self.assertEqual(result["summary"]["long_stock_positions"], 0)
        self.assertAlmostEqual(result["summary"]["realized_stock_pnl"], 500.0)
        self.assertEqual(result["open_positions"]["stocks"], [])

    def test_covered_call_rejects_strike_below_adjusted_cost_basis(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    call_scan,
                    close=96,
                    close_overrides={put_expiration: 90},
                )
            },
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", call_scan): [
                    _call(expiration=date(2026, 1, 16), strike=90, dte=4)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_covered_calls"], 0)
        self.assertIn(
            "strike_below_adjusted_cost_basis",
            result["summary"]["rejected_reason_counts"],
        )

    def test_covered_call_missing_expiration_close_does_not_use_prior_close(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        bars = [
            bar
            for bar in _bars(
                start,
                call_expiration,
                close=98,
                close_overrides={put_expiration: 90, call_scan: 96},
            )
            if bar.date != call_expiration
        ]
        data = _FakeData(
            bars={"AAPL": bars},
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_expiration,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["expired_covered_calls"], 0)
        self.assertEqual(result["summary"]["called_away"], 0)
        self.assertEqual(result["summary"]["open_short_calls"], 1)
        self.assertTrue(
            any(
                event["type"] == "CALL_EXPIRATION_MISSING_UNDERLYING_CLOSE"
                for event in result["events"]
            )
        )

    def test_open_covered_call_intrinsic_fallback_marks_liability(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    call_expiration,
                    close=105,
                    close_overrides={put_expiration: 90, call_scan: 105},
                )
            },
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["open_short_calls"], 1)
        self.assertGreaterEqual(result["summary"]["data_issue_counts"]["missing_option_mark"], 1)
        self.assertLess(result["summary"]["ending_equity"], 501100)

    def test_partial_coverage_can_open_second_call_for_remaining_lot(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        first_call_scan = date(2026, 1, 12)
        second_call_scan = date(2026, 1, 13)
        call_expiration = date(2026, 1, 16)
        config = _config()
        config["trade_planner"]["default_contract_quantity"] = 2
        config["cc_selector"]["default_contract_quantity"] = 1
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    call_expiration,
                    close=96,
                    close_overrides={put_expiration: 90},
                )
            },
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", first_call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
                ("AAPL", second_call_scan): [
                    _call(expiration=call_expiration, strike=101, dte=3)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=second_call_scan,
                slippage_pct=0.0,
            )

        self.assertEqual(result["open_positions"]["stocks"][0]["shares"], 200)
        self.assertEqual(result["summary"]["opened_covered_calls"], 2)
        self.assertEqual(result["summary"]["open_short_calls"], 2)

    def test_covered_call_filter_rejections_are_counted(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        data = _FakeData(
            bars={
                "AAPL": _bars(
                    start,
                    call_scan,
                    close=96,
                    close_overrides={put_expiration: 90},
                )
            },
            options={
                ("AAPL", start): [_put(expiration=put_expiration, strike=95)],
                ("AAPL", call_scan): [
                    _call(
                        expiration=date(2026, 1, 16),
                        strike=100,
                        dte=4,
                        bid=0.05,
                    ),
                    _call(
                        expiration=date(2026, 1, 16),
                        strike=101,
                        dte=4,
                        bid=1.0,
                        ask=1.5,
                    ),
                    _call(
                        expiration=date(2026, 1, 16),
                        strike=102,
                        dte=4,
                        delta=0.5,
                    ),
                    _call(
                        expiration=date(2026, 1, 16),
                        strike=103,
                        dte=4,
                        open_interest=1,
                    ),
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_covered_calls"], 0)
        reject_events = [
            event
            for event in result["events"]
            if event["type"] == "REJECT" and event["ticker"] == "AAPL"
        ]
        cc_summary = reject_events[-1]["diagnostics"]["cc_rejection_summary"]
        self.assertIn("bid_below_min", cc_summary)
        self.assertIn("spread_too_wide", cc_summary)
        self.assertIn("delta_outside_target", cc_summary)
        self.assertIn("open_interest_below_min", cc_summary)

    def test_adjusted_cost_basis_floor_prevents_negative_min_strike(self):
        stock = StockPosition(
            ticker="AAPL",
            shares=100,
            cost_basis_total=9500,
            premium_credit_total=20000,
        )

        min_strike = _min_covered_call_strike(stock, _config())

        self.assertEqual(min_strike, 95.0)

    def test_fundamentals_warn_records_would_reject_without_blocking_trade(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )
        store = _FakeFundamentalsStore(
            FundamentalSnapshot(
                ticker="AAPL",
                quote_type="CS",
                market_cap=10_000_000_000,
                pe_ratio=-5,
                dividend_yield=0.01,
                quarterly_net_income=[1, 1, 1, 1, 1],
                annual_net_income=[1, 1, 1, 1, 1],
                next_earnings_date=date(2026, 8, 1),
                recent_move_pct=5,
                provenance={
                    "pe_ratio": FundamentalFieldProvenance(
                        value=-5,
                        source="test",
                        as_of=start,
                        known_at=start,
                        quality="strict_pit_pending_validation",
                    )
                },
            )
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_warn",
                historical_fundamentals=store,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertGreater(result["summary"]["fundamental_would_reject_count"], 0)
        self.assertIn(
            "fundamental_gate:pe_ratio_non_positive",
            result["fundamental_diagnostics"]["would_reject_reason_counts"],
        )
        self.assertTrue(
            any(event["type"] == "FUNDAMENTAL_DIAGNOSTIC" for event in result["events"])
        )

    def test_technical_only_does_not_call_fundamental_store(self):
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
                historical_fundamentals=_ExplodingFundamentalsStore(),
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertEqual(result["summary"]["fundamental_profile"], "technical_only")

    def test_strict_financials_does_not_block_pending_pit_financials(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )
        store = _FakeFundamentalsStore(
            FundamentalSnapshot(
                ticker="AAPL",
                quote_type="CS",
                market_cap=10_000_000_000,
                pe_ratio=-5,
                dividend_yield=0.01,
                quarterly_net_income=[1, 1, 1, 1, 1],
                annual_net_income=[1, 1, 1, 1, 1],
                next_earnings_date=date(2026, 8, 1),
                recent_move_pct=5,
                provenance={
                    "pe_ratio": FundamentalFieldProvenance(
                        value=-5,
                        source="test",
                        as_of=start,
                        known_at=start,
                        quality="strict_pit_pending_validation",
                    )
                },
            )
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_strict_financials",
                historical_fundamentals=store,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertGreater(result["summary"]["fundamental_would_reject_count"], 0)


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
        execution_model=None,
    ) -> list[OptionQuote]:
        options = self.options.get((underlying, as_of), [])
        return [
            option
            for option in options
            if dte_min <= option.dte <= dte_max
            and (option.volume or 0) > 0
            and _option_type(option.symbol) == option_type
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


class _FakeFundamentalsStore:
    def __init__(self, snapshot: FundamentalSnapshot) -> None:
        self.snapshot_value = snapshot
        self.preloaded = False

    def preload(self, tickers, start: date, end: date) -> None:
        self.preloaded = True

    def snapshot(
        self,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> FundamentalSnapshot:
        return self.snapshot_value

    def diagnostics(self) -> dict:
        return {"preloaded": self.preloaded, "providers": {"fake": {}}}


class _ExplodingFundamentalsStore:
    def preload(self, tickers, start: date, end: date) -> None:
        raise AssertionError("fundamental store should not be preloaded in technical_only")

    def snapshot(
        self,
        ticker: str,
        as_of: date,
        bars: list[PriceBar],
    ) -> FundamentalSnapshot:
        raise AssertionError("fundamental store should not be called in technical_only")

    def diagnostics(self) -> dict:
        raise AssertionError("fundamental diagnostics should not be called in technical_only")


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


def _call(
    *,
    ticker: str = "AAPL",
    expiration: date,
    strike: float,
    dte: int,
    volume: int = 100,
    bid: float = 1.0,
    ask: float = 1.0,
    delta: float = 0.2,
    open_interest: int = 500,
) -> OptionQuote:
    return OptionQuote(
        symbol=f"{ticker}{expiration:%y%m%d}C{int(strike * 1000):08d}",
        expiration=expiration,
        dte=dte,
        strike=strike,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        implied_volatility=0.25,
        open_interest=open_interest,
        volume=volume,
        delta=delta,
    )


def _option_type(symbol: str) -> str:
    parsed = parse_option_symbol(symbol)
    assert parsed is not None
    return parsed.option_type


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
