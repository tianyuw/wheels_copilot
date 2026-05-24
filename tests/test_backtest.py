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
from wheels_copilot.price_space_breaks import SplitEvent, StaticSplitEventProvider


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

    def test_candidate_ledger_records_opened_csp(self):
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

        ledger = result["candidate_ledger"]
        opened_rows = [row for row in ledger if row["decision"] == "OPENED"]
        self.assertEqual(len(opened_rows), 1)
        self.assertEqual(opened_rows[0]["ticker"], "AAPL")
        self.assertTrue(opened_rows[0]["candidate_present"])
        self.assertEqual(
            result["summary"]["candidate_ledger_diagnostics"][
                "mechanical_candidate_count"
            ],
            1,
        )
        self.assertEqual(
            result["summary"]["candidate_ledger_diagnostics"][
                "opened_from_candidate_count"
            ],
            1,
        )

    def test_two_pass_scans_all_candidates_then_executes_top_ranked(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={
                "AAPL": _bars(start, end, close=110),
                "MSFT": _bars(start, end, close=110),
            },
            options={
                ("AAPL", start): [
                    _put(ticker="AAPL", expiration=end, strike=95, bid=1.0, ask=1.0)
                ],
                ("MSFT", start): [
                    _put(ticker="MSFT", expiration=end, strike=95, bid=2.0, ask=2.0)
                ],
            },
            marks={},
        )
        config = _config()

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
        self.assertEqual(result["trades"][0]["ticker"], "MSFT")
        opened_rows = [
            row for row in result["candidate_ledger"] if row["decision"] == "OPENED"
        ]
        unselected_rows = [
            row
            for row in result["candidate_ledger"]
            if row["reason"] == "not_selected_daily_order_limit"
        ]
        self.assertEqual(opened_rows[0]["ticker"], "MSFT")
        self.assertEqual(opened_rows[0]["rank_within_day"], 1)
        self.assertTrue(opened_rows[0]["selected_for_backtest"])
        self.assertEqual(unselected_rows[0]["ticker"], "AAPL")
        self.assertEqual(unselected_rows[0]["rank_within_day"], 2)
        self.assertFalse(unselected_rows[0]["selected_for_backtest"])
        diagnostics = result["summary"]["candidate_ledger_diagnostics"]
        self.assertEqual(
            diagnostics["mechanical_candidate_count"],
            2,
        )
        self.assertEqual(diagnostics["selected_for_backtest_count"], 1)
        self.assertEqual(diagnostics["candidate_to_trade_ratio_pct"], 50.0)
        self.assertEqual(diagnostics["median_unique_candidate_tickers_per_day"], 0.0)
        self.assertEqual(diagnostics["average_unique_candidate_tickers_per_day"], 0.4)
        self.assertEqual(diagnostics["pct_days_with_3plus_unique_tickers"], 0.0)

    def test_two_pass_opens_all_ranked_candidates_when_capacity_non_binding(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={
                "AAPL": _bars(start, end, close=110),
                "MSFT": _bars(start, end, close=110),
            },
            options={
                ("AAPL", start): [
                    _put(ticker="AAPL", expiration=end, strike=95, bid=1.0, ask=1.0)
                ],
                ("MSFT", start): [
                    _put(ticker="MSFT", expiration=end, strike=95, bid=2.0, ask=2.0)
                ],
            },
            marks={},
        )
        config = _config()
        config["execution"]["max_orders_per_run"] = 2

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL", "MSFT"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 2)
        opened_rows = [
            row for row in result["candidate_ledger"] if row["decision"] == "OPENED"
        ]
        self.assertEqual([row["ticker"] for row in opened_rows], ["MSFT", "AAPL"])
        self.assertFalse(
            any(row["selection_stage"] == "capacity" for row in result["candidate_ledger"])
        )
        diagnostics = result["summary"]["candidate_ledger_diagnostics"]
        self.assertEqual(diagnostics["mechanical_candidate_count"], 2)
        self.assertEqual(diagnostics["selected_for_backtest_count"], 2)
        self.assertEqual(diagnostics["candidate_to_trade_ratio_pct"], 100.0)
        self.assertEqual(diagnostics["quality_candidate_count"], 2)
        self.assertEqual(diagnostics["median_quality_candidates_per_day"], 2.0)
        self.assertEqual(diagnostics["average_quality_candidates_per_day"], 2.0)
        self.assertEqual(diagnostics["pct_days_with_3plus_quality_candidates"], 0.0)

    def test_quality_candidate_filter_can_discriminate_after_selector(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={
                "AAPL": _bars(start, end, close=110),
                "MSFT": _bars(start, end, close=110),
            },
            options={
                ("AAPL", start): [
                    _put(ticker="AAPL", expiration=end, strike=95, bid=1.0, ask=1.0)
                ],
                ("MSFT", start): [
                    _put(ticker="MSFT", expiration=end, strike=95, bid=0.8, ask=1.2)
                ],
            },
            marks={},
        )
        config = _config()
        config["execution"]["max_orders_per_run"] = 2
        config["csp_selector"]["max_spread_pct_of_mid"] = 0.50

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL", "MSFT"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        diagnostics = result["summary"]["candidate_ledger_diagnostics"]
        self.assertEqual(diagnostics["mechanical_candidate_count"], 2)
        self.assertEqual(diagnostics["quality_candidate_count"], 1)
        self.assertEqual(diagnostics["quality_candidate_pass_rate_pct"], 50.0)
        self.assertEqual(
            diagnostics["average_daily_quality_candidate_pass_rate_pct"],
            50.0,
        )

    def test_candidate_ledger_schema_records_required_fields(self):
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

        row = next(row for row in result["candidate_ledger"] if row["decision"] == "OPENED")
        required = {
            "schema_version",
            "date",
            "ticker",
            "decision",
            "reason",
            "candidate_present",
            "quality_score",
            "rank_within_day",
            "selected_for_backtest",
            "selection_stage",
            "option_symbol",
            "expiration",
            "dte",
            "strike",
            "delta",
            "weekly_return_on_strike_pct",
            "cash_required",
            "cash_available_at_decision",
        }
        self.assertTrue(required.issubset(row))
        self.assertEqual(row["schema_version"], "candidate_ledger.v2")
        self.assertEqual(row["selection_stage"], "executed")
        self.assertGreater(row["quality_score"], 0)
        self.assertEqual(
            result["summary"]["candidate_ledger_diagnostics"]["schema_version"],
            "candidate_ledger.v2",
        )

    def test_two_pass_candidate_set_and_selection_are_universe_order_invariant(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]
        options = {}
        bars = {}
        for index, ticker in enumerate(tickers):
            bars[ticker] = _bars(start, end, close=110)
            premium = 1.0 + index / 10.0
            options[(ticker, start)] = [
                _put(ticker=ticker, expiration=end, strike=95, bid=premium, ask=premium)
            ]
        data = _FakeData(bars=bars, options=options, marks={})
        config = _config()
        config["execution"]["max_orders_per_run"] = 3

        def run(universe):
            with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
                return run_backtest(
                    config=config,
                    data=data,
                    universe=universe,
                    start=start,
                    end=end,
                    slippage_pct=0.0,
                )

        forward = run(tickers)
        reversed_result = run(list(reversed(tickers)))

        def candidate_fingerprint(result):
            return sorted(
                (
                    row["ticker"],
                    row["option_symbol"],
                    row["rank_within_day"],
                    row["selected_for_backtest"],
                )
                for row in result["candidate_ledger"]
                if row["candidate_present"]
                and row["decision"] in {"OPENED", "CANDIDATE"}
            )

        self.assertEqual(candidate_fingerprint(forward), candidate_fingerprint(reversed_result))
        self.assertEqual(
            [trade["ticker"] for trade in forward["trades"]],
            [trade["ticker"] for trade in reversed_result["trades"]],
        )

    def test_two_pass_equal_score_tie_breaks_by_ticker(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={
                "ZZZ": _bars(start, end, close=110),
                "AAA": _bars(start, end, close=110),
            },
            options={
                ("ZZZ", start): [
                    _put(ticker="ZZZ", expiration=end, strike=95, bid=1.0, ask=1.0)
                ],
                ("AAA", start): [
                    _put(ticker="AAA", expiration=end, strike=95, bid=1.0, ask=1.0)
                ],
            },
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["ZZZ", "AAA"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["trades"][0]["ticker"], "AAA")
        opened_row = next(row for row in result["candidate_ledger"] if row["decision"] == "OPENED")
        self.assertEqual(opened_row["ticker"], "AAA")
        self.assertEqual(opened_row["rank_within_day"], 1)

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
        last_row = result["equity_curve"][-1]
        self.assertEqual(last_row["capital_deployed"], 9500)
        self.assertGreater(last_row["capital_utilization_pct"], 0)
        self.assertEqual(
            last_row["capital_utilization_pct"],
            last_row["reserved_assignment_cash_pct"],
        )
        self.assertEqual(result["summary"]["cash_idle_days"], 0)

    def test_equity_pct_position_sizing_scales_csp_contracts(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 6)
        expiration = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, expiration, close=110)},
            options={("AAPL", start): [_put(expiration=expiration, strike=95)]},
            marks={},
        )
        config = _config()
        config["backtest_position_sizing"] = {
            "mode": "equity_pct",
            "target_equity_pct": 0.05,
            "min_contracts": 1,
        }

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["trades"][0]["contracts"], 2)
        self.assertEqual(result["summary"]["reserved_assignment_cash"], 19000)
        self.assertEqual(result["summary"]["average_csp_contracts_per_trade"], 2.0)
        self.assertEqual(result["summary"]["max_csp_assignment_cash_required"], 19000)

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
        diagnostics = result["summary"]["candidate_ledger_diagnostics"]
        self.assertEqual(
            diagnostics["binding_filter_counts"]["no_fillable_put_options"],
            5,
        )
        self.assertEqual(
            diagnostics["top_binding_filters"][0],
            {"reason": "no_fillable_put_options", "count": 5},
        )
        self.assertEqual(diagnostics["mechanical_candidate_count"], 0)
        self.assertEqual(diagnostics["starvation_days"], 5)
        self.assertEqual(diagnostics["median_candidates_per_day"], 0.0)
        self.assertEqual(diagnostics["pct_days_with_3plus_candidates"], 0.0)

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
        self.assertEqual(result["summary"]["price_space_break_category_counts"], {})
        self.assertEqual(result["summary"]["price_space_break_action_counts"], {})

    def test_price_break_classifier_unblocks_pre_start_real_gap(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        bars = _bars(start, end, close=110)
        bars[10] = PriceBar(date=bars[10].date, open=77, high=78, low=76, close=77)
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
                price_space_break_classifier="massive_splits",
                price_space_break_split_provider=StaticSplitEventProvider([]),
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertGreaterEqual(
            result["summary"]["price_space_break_category_counts"]["real_gap_move"],
            1,
        )
        self.assertEqual(
            result["summary"]["event_counts"]["PRICE_SPACE_BREAK_ALLOWED"],
            result["summary"]["data_issue_counts"]["price_space_break"],
        )

    def test_price_break_classifier_resets_confirmed_split_lookback(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        bars = _bars(start, end, close=110)
        split_day = bars[10].date
        _set_bar_values_from(bars, split_day, value=55)
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
            price_space_break_classifier="massive_splits",
            price_space_break_split_provider=StaticSplitEventProvider(
                [
                    SplitEvent(
                        ticker="AAPL",
                        execution_date=split_day,
                        split_from=1,
                        split_to=2,
                    )
                ]
            ),
        )

        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertEqual(
            result["summary"]["price_space_break_category_counts"]["confirmed_split"],
            1,
        )
        self.assertEqual(
            result["summary"]["price_space_break_action_counts"]["reset_lookback"],
            1,
        )
        self.assertEqual(
            result["summary"]["event_counts"]["PRICE_SPACE_BREAK_RESET"],
            1,
        )
        self.assertEqual(
            result["run_parameters"]["price_space_reset_dates"]["AAPL"],
            split_day.isoformat(),
        )

    def test_pre_start_split_reset_cooldown_blocks_short_history(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        bars = _bars(start, end, close=110)
        pre_start_indices = [index for index, bar in enumerate(bars) if bar.date < start]
        split_day = bars[pre_start_indices[-2]].date
        _set_bar_values_from(bars, split_day, value=55)
        data = _FakeData(
            bars={"AAPL": bars},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support") as support:
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
                price_space_break_classifier="massive_splits",
                price_space_break_split_provider=StaticSplitEventProvider(
                    [
                        SplitEvent(
                            ticker="AAPL",
                            execution_date=split_day,
                            split_from=1,
                            split_to=2,
                        )
                    ]
                ),
            )

        support.assert_not_called()
        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertGreaterEqual(
            result["summary"]["rejected_reason_counts"][
                "post_split_reset_insufficient_support_history"
            ],
            1,
        )

    def test_unknown_break_after_confirmed_split_still_blocks_ticker(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        bars = _bars(start, end, close=110)
        split_day = bars[10].date
        unknown_day = bars[20].date
        _set_bar_values_from(bars, split_day, value=55)
        _set_bar_values_from(bars, unknown_day, value=4)
        data = _FakeData(
            bars={"AAPL": bars},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )

        with patch("wheels_copilot.backtest.analyze_support") as support:
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
                price_space_break_classifier="massive_splits",
                price_space_break_split_provider=StaticSplitEventProvider(
                    [
                        SplitEvent(
                            ticker="AAPL",
                            execution_date=split_day,
                            split_from=1,
                            split_to=2,
                        )
                    ]
                ),
            )

        support.assert_not_called()
        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertEqual(
            result["summary"]["price_space_break_action_counts"]["reset_lookback"],
            1,
        )
        self.assertGreaterEqual(
            result["summary"]["price_space_break_action_counts"]["block"],
            1,
        )
        self.assertGreaterEqual(
            result["summary"]["event_counts"]["PRICE_SPACE_BREAK_BLOCK"],
            1,
        )

    def test_confirmed_split_clears_prior_price_space_block(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 16)
        unknown_day = date(2026, 1, 6)
        split_day = date(2026, 1, 8)
        trade_day = date(2026, 1, 9)
        bars = _bars(start, end, close=100)
        _set_bar_values_from(bars, unknown_day, value=20)
        _set_bar_values_from(bars, split_day, value=10)
        data = _FakeData(
            bars={"AAPL": bars},
            options={("AAPL", trade_day): [_put(expiration=date(2026, 1, 14), strike=8)]},
            marks={},
        )

        zone = SupportZone(
            method="test",
            center=9,
            bottom=8,
            top=10,
            touches=3,
            rejections=3,
            score=90,
        )
        support = SupportAnalysis(
            trend=TrendCheck(passed=True, current_price=10, sma200=9, sma200_slope=1),
            zones=[zone],
            selected_zone=zone,
            atr14=1,
            current_price=10,
            min_score_to_trade=70,
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=support):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
                price_space_break_classifier="massive_splits",
                price_space_break_split_provider=StaticSplitEventProvider(
                    [
                        SplitEvent(
                            ticker="AAPL",
                            execution_date=split_day,
                            split_from=1,
                            split_to=2,
                        )
                    ]
                ),
                price_space_split_reset_min_support_bars=1,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertEqual(result["trades"][0]["entry_date"], trade_day.isoformat())
        self.assertGreaterEqual(
            result["summary"]["price_space_break_action_counts"]["block"],
            1,
        )
        self.assertEqual(
            result["summary"]["price_space_break_action_counts"]["reset_lookback"],
            1,
        )

    def test_confirmed_split_reset_uses_only_post_split_support_bars(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        bars = _bars(start, end, close=110)
        split_day = bars[10].date
        _set_bar_values_from(bars, split_day, value=55)
        data = _FakeData(
            bars={"AAPL": bars},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )
        support_seen = {}

        def fake_support(support_bars, config):
            support_seen["first_date"] = support_bars[0].date
            support_seen["count"] = len(support_bars)
            return _support()

        with patch("wheels_copilot.backtest.analyze_support", side_effect=fake_support):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
                price_space_break_classifier="massive_splits",
                price_space_break_split_provider=StaticSplitEventProvider(
                    [
                        SplitEvent(
                            ticker="AAPL",
                            execution_date=split_day,
                            split_from=1,
                            split_to=2,
                        )
                    ]
                ),
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertEqual(support_seen["first_date"], split_day)
        self.assertLess(support_seen["count"], len([bar for bar in bars if bar.date < start]))

    def test_open_option_through_split_reset_is_flagged_unmodeled(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        split_day = date(2026, 1, 8)
        bars = _bars(start, end, close=110)
        _set_bar_values_from(bars, split_day, value=55)
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
                price_space_break_classifier="massive_splits",
                price_space_break_split_provider=StaticSplitEventProvider(
                    [
                        SplitEvent(
                            ticker="AAPL",
                            execution_date=split_day,
                            split_from=1,
                            split_to=2,
                        )
                    ]
                ),
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertEqual(result["trades"][0]["entry_date"], start.isoformat())
        self.assertEqual(
            result["summary"]["data_issue_counts"][
                "open_option_through_price_space_reset"
            ],
            1,
        )
        self.assertEqual(
            result["summary"]["event_counts"][
                "OPEN_OPTION_THROUGH_PRICE_SPACE_RESET_UNMODELED"
            ],
            1,
        )

    def test_covered_call_respects_post_split_reset_cooldown(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        assignment_day = date(2026, 1, 6)
        split_day = date(2026, 1, 7)
        bars = _bars(
            start,
            end,
            close=110,
            close_overrides={assignment_day: 90},
        )
        _set_bar_values_from(bars, split_day, value=45)
        data = _FakeData(
            bars={"AAPL": bars},
            options={("AAPL", start): [_put(expiration=assignment_day, strike=95)]},
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
                price_space_break_classifier="massive_splits",
                price_space_break_split_provider=StaticSplitEventProvider(
                    [
                        SplitEvent(
                            ticker="AAPL",
                            execution_date=split_day,
                            split_from=1,
                            split_to=2,
                        )
                    ]
                ),
            )

        self.assertEqual(result["summary"]["assigned"], 1)
        self.assertEqual(result["summary"]["opened_covered_calls"], 0)
        self.assertGreaterEqual(
            result["summary"]["rejected_reason_counts"][
                "cc_post_split_reset_insufficient_support_history"
            ],
            1,
        )

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
        self.assertEqual(result["summary"]["assignments_with_cc_opened"], 1)
        self.assertEqual(result["summary"]["assignments_without_cc_opened"], 0)
        self.assertEqual(result["summary"]["uncovered_assigned_days"], 1)
        self.assertEqual(result["summary"]["uncovered_assigned_share_days"], 100)
        self.assertEqual(result["open_positions"]["short_calls"][0]["ticker"], "AAPL")
        recovery = result["summary"]["assignment_recovery_diagnostics"][0]
        self.assertEqual(recovery["assignment_date"], put_expiration.isoformat())
        self.assertEqual(recovery["first_cc_opened_date"], call_scan.isoformat())
        self.assertEqual(recovery["days_to_first_cc_open"], 3)
        last_row = result["equity_curve"][-1]
        self.assertEqual(last_row["assigned_shares"], 100)
        self.assertEqual(last_row["covered_assigned_shares"], 100)
        self.assertEqual(last_row["uncovered_assigned_shares"], 0)
        self.assertGreater(last_row["assigned_stock_utilization_pct"], 0)
        self.assertEqual(last_row["reserved_assignment_cash_pct"], 0.0)
        self.assertEqual(
            last_row["capital_utilization_pct"],
            last_row["assigned_stock_utilization_pct"],
        )
        self.assertGreater(result["summary"]["average_daily_capital_utilization_pct"], 0)
        self.assertGreater(result["summary"]["average_cash_pct"], 50)

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
        reject_events = [
            event
            for event in result["events"]
            if event["type"] == "REJECT" and event["ticker"] == "AAPL"
        ]
        diagnostics = reject_events[-1]["diagnostics"]["cc_selection_diagnostics"]
        self.assertEqual(diagnostics["best_available_call_strike"], 90)
        self.assertGreater(diagnostics["strike_to_adjusted_cost_basis_gap"], 0)

    def test_cc_warn_unknown_dates_opens_call_without_mutating_config(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        config = _config()
        config["cc_risk"]["nested"] = {"preserve": True}
        original_cc_risk = dict(config["cc_risk"])
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
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )
        store = _FakeFundamentalsStore(
            _fundamental(
                next_earnings_date=None,
                dividend_yield=0.01,
                ex_dividend_date=None,
            )
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_strict_all",
                historical_fundamentals=store,
                cc_risk_profile="warn_unknown_dates",
            )

        self.assertEqual(result["summary"]["cc_risk_profile"], "warn_unknown_dates")
        self.assertEqual(result["summary"]["opened_covered_calls"], 1)
        self.assertEqual(config["cc_risk"], original_cc_risk)
        self.assertTrue(config["cc_risk"]["block_unknown_stock_earnings_date"])
        self.assertEqual(config["cc_risk"]["nested"], {"preserve": True})
        call_trade = result["trades"][-1]
        self.assertEqual(call_trade["strategy"], "covered_call")

    def test_cc_warn_unknown_dates_allows_stale_ex_dividend_date(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
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
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )
        store = _FakeFundamentalsStore(
            _fundamental(
                next_earnings_date=date(2026, 3, 1),
                dividend_yield=0.01,
                ex_dividend_date=date(2025, 12, 1),
            )
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_strict_all",
                historical_fundamentals=store,
                cc_risk_profile="warn_unknown_dates",
            )

        self.assertEqual(result["summary"]["opened_covered_calls"], 1)
        call_event = [
            event for event in result["events"] if event["type"] == "OPEN_SHORT_CALL"
        ][0]
        self.assertEqual(
            call_event["diagnostics"]["cc_ex_dividend_gate"]["status"],
            "WARN",
        )

    def test_cc_risk_profile_precedence_explicit_arg_overrides_config(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        config = _config()
        config["backtest"] = {"cc_risk_profile": "warn_unknown_dates"}
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
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )
        store = _FakeFundamentalsStore(_fundamental(next_earnings_date=None))

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_strict_all",
                historical_fundamentals=store,
                cc_risk_profile="strict",
            )

        self.assertEqual(result["summary"]["cc_risk_profile"], "strict")
        self.assertEqual(result["summary"]["opened_covered_calls"], 0)

    def test_cc_risk_profile_defaults_to_strict_without_config(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
        config = _config()
        config.pop("backtest", None)
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
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )
        store = _FakeFundamentalsStore(_fundamental(next_earnings_date=None))

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_strict_all",
                historical_fundamentals=store,
            )

        self.assertEqual(result["summary"]["cc_risk_profile"], "strict")
        self.assertEqual(result["summary"]["opened_covered_calls"], 0)

    def test_cc_warn_unknown_dates_still_blocks_known_earnings_inside_window(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
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
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )
        store = _FakeFundamentalsStore(
            _fundamental(next_earnings_date=call_expiration)
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_strict_all",
                historical_fundamentals=store,
                cc_risk_profile="warn_unknown_dates",
            )

        self.assertEqual(result["summary"]["opened_covered_calls"], 0)
        self.assertTrue(
            any(
                reason.startswith(
                    "covered_call_risk_gate:cc_expiration_on_or_after_earnings"
                )
                for reason in result["summary"]["rejected_reason_counts"]
            )
        )

    def test_cc_warn_unknown_dates_still_blocks_known_ex_dividend_inside_window(self):
        start = date(2026, 1, 5)
        put_expiration = date(2026, 1, 9)
        call_scan = date(2026, 1, 12)
        call_expiration = date(2026, 1, 16)
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
                ("AAPL", call_scan): [
                    _call(expiration=call_expiration, strike=100, dte=4)
                ],
            },
            marks={},
        )
        store = _FakeFundamentalsStore(
            _fundamental(
                next_earnings_date=date(2026, 3, 1),
                dividend_yield=0.01,
                ex_dividend_date=date(2026, 1, 15),
            )
        )

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=_config(),
                data=data,
                universe=["AAPL"],
                start=start,
                end=call_scan,
                slippage_pct=0.0,
                fundamental_profile="fundamentals_strict_all",
                historical_fundamentals=store,
                cc_risk_profile="warn_unknown_dates",
            )

        self.assertEqual(result["summary"]["opened_covered_calls"], 0)
        self.assertTrue(
            any(
                reason.startswith(
                    "covered_call_risk_gate:cc_ex_dividend_within_contract_window"
                )
                for reason in result["summary"]["rejected_reason_counts"]
            )
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

    def test_fundamentals_moderate_blocks_hard_quality_reject(self):
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
                fundamental_profile="fundamentals_moderate",
                historical_fundamentals=store,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertIn(
            "fundamental_gate:pe_ratio_non_positive",
            result["summary"]["rejected_reason_counts"],
        )

    def test_fundamentals_moderate_allows_high_pe_valuation_reject(self):
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
                pe_ratio=80,
                dividend_yield=0.01,
                quarterly_net_income=[1, 1, 1, 1, 1],
                annual_net_income=[1, 1, 1, 1, 1],
                next_earnings_date=date(2026, 8, 1),
                recent_move_pct=5,
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
                fundamental_profile="fundamentals_moderate",
                historical_fundamentals=store,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertIn(
            "fundamental_gate:pe_ratio_at_or_above_50",
            result["fundamental_diagnostics"]["would_reject_reason_counts"],
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

    def test_post_earnings_cooldown_blocks_recent_previous_report(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )
        store = _FakeFundamentalsStore(
            _fundamental(
                next_earnings_date=date(2026, 8, 1),
                previous_earnings_date=date(2026, 1, 2),
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
                historical_fundamentals=store,
                post_earnings_cooldown_days=1,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertEqual(result["summary"]["post_earnings_cooldown_days"], 1)
        self.assertIn(
            "csp_post_earnings_cooldown_gate:post_earnings_cooldown_1_lte_1",
            result["summary"]["rejected_reason_counts"],
        )

    def test_post_earnings_cooldown_allows_after_trading_day_boundary(self):
        start = date(2026, 1, 7)
        end = date(2026, 1, 9)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )
        store = _FakeFundamentalsStore(
            _fundamental(
                next_earnings_date=date(2026, 8, 1),
                previous_earnings_date=date(2026, 1, 2),
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
                historical_fundamentals=store,
                post_earnings_cooldown_days=2,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 1)
        self.assertIn(
            "csp_post_earnings_cooldown_gate:PASS",
            result["fundamental_diagnostics"]["gate_status_counts"],
        )

    def test_csp_profit_target_exit_closes_before_expiration(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        option = _put(expiration=end, strike=95, bid=1.0, ask=1.0)
        mark_day = date(2026, 1, 6)
        data = _FakeData(
            bars={"AAPL": _bars(start, end, close=110)},
            options={("AAPL", start): [option]},
            marks={
                (option.symbol, mark_day): _option_mark(
                    option,
                    as_of=mark_day,
                    price=0.40,
                )
            },
        )
        config = _config()
        config["management"]["csp_exit_model"] = "close_at_50pct_profit_or_expiry"
        config["management"]["profit_take_pct_of_credit"] = 0.50
        config["management"]["profit_take_price_field"] = "low"

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["csp_profit_target_closes"], 1)
        self.assertEqual(result["summary"]["expired_worthless"], 0)
        self.assertEqual(result["trades"][0]["status"], "CLOSED_PROFIT_TARGET")
        self.assertEqual(result["trades"][0]["close_reason"], "profit_target")
        self.assertEqual(result["summary"]["open_short_puts"], 0)

    def test_market_regime_spy_sma200_gate_blocks_new_csp(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        spy_bars = _long_bars(start, end, close=100)
        _set_bar_values_from(spy_bars, start - timedelta(days=5), value=90)
        data = _FakeData(
            bars={
                "AAPL": _bars(start, end, close=110),
                "SPY": spy_bars,
            },
            options={("AAPL", start): [_put(expiration=end, strike=95)]},
            marks={},
        )
        config = _config()
        config["market_regime"]["enabled"] = True
        config["market_regime"]["require_market_price_above_sma200"] = True

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertGreater(
            result["summary"]["market_regime_diagnostics"]["reject_days"],
            0,
        )
        self.assertIn(
            "market_regime_gate:market_price_below_sma200:SPY",
            result["summary"]["rejected_reason_counts"],
        )

    def test_conditional_regime_override_can_disable_above_support_csp(self):
        start = date(2026, 1, 5)
        end = date(2026, 1, 9)
        spy_bars = _long_bars(start, end, close=100)
        _set_bar_values_from(spy_bars, start - timedelta(days=5), value=90)
        data = _FakeData(
            bars={
                "AAPL": _bars(start, end, close=110),
                "SPY": spy_bars,
            },
            options={("AAPL", start): [_put(expiration=end, strike=103)]},
            marks={},
        )
        config = _config()
        config["csp_selector"]["allow_strike_above_support_zone"] = True
        config["csp_selector"]["auto_trade_above_support_zone"] = True
        config["market_regime"]["enabled"] = False
        config["market_regime"]["conditional_csp_overrides"] = {
            "enabled": True,
            "when_market_price_below_sma200": {
                "patch": {
                    "csp_selector.allow_strike_above_support_zone": False,
                    "csp_selector.auto_trade_above_support_zone": False,
                }
            },
        }

        with patch("wheels_copilot.backtest.analyze_support", return_value=_support()):
            result = run_backtest(
                config=config,
                data=data,
                universe=["AAPL"],
                start=start,
                end=end,
                slippage_pct=0.0,
            )

        self.assertEqual(result["summary"]["opened_short_puts"], 0)
        self.assertIn("strike_above_support_zone", result["summary"]["rejected_reason_counts"])
        rejected = next(
            row
            for row in result["candidate_ledger"]
            if row["reason"] == "strike_above_support_zone"
        )
        self.assertTrue(rejected["csp_regime_override"]["applied"])

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


def _fundamental(
    *,
    next_earnings_date: date | None,
    previous_earnings_date: date | None = None,
    dividend_yield: float | None = None,
    ex_dividend_date: date | None = None,
) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        ticker="AAPL",
        quote_type="CS",
        market_cap=10_000_000_000,
        pe_ratio=20,
        dividend_yield=dividend_yield,
        annual_dividend_rate=1.0 if dividend_yield else None,
        ex_dividend_date=ex_dividend_date,
        quarterly_net_income=[1, 1, 1, 1, 1],
        annual_net_income=[1, 1, 1, 1, 1],
        next_earnings_date=next_earnings_date,
        previous_earnings_date=previous_earnings_date,
        recent_move_pct=5,
    )


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


def _option_mark(option: OptionQuote, *, as_of: date, price: float) -> OptionQuote:
    return OptionQuote(
        symbol=option.symbol,
        expiration=option.expiration,
        dte=max((option.expiration - as_of).days, 0),
        strike=option.strike,
        bid=price,
        ask=price,
        last=price,
        implied_volatility=option.implied_volatility,
        open_interest=option.open_interest,
        volume=option.volume,
        delta=option.delta,
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


def _long_bars(start: date, end: date, *, close: float) -> list[PriceBar]:
    first = start - timedelta(days=430)
    bars: list[PriceBar] = []
    current = first
    while current <= end:
        if current.weekday() < 5:
            bars.append(
                PriceBar(
                    date=current,
                    open=close,
                    high=close + 1,
                    low=close - 1,
                    close=close,
                    volume=1_000_000,
                )
            )
        current += timedelta(days=1)
    return bars


def _set_bar_values_from(bars: list[PriceBar], start: date, *, value: float) -> None:
    for index, bar in enumerate(bars):
        if bar.date >= start:
            bars[index] = PriceBar(
                date=bar.date,
                open=value,
                high=value + 1,
                low=value - 1,
                close=value,
                volume=bar.volume,
            )


if __name__ == "__main__":
    unittest.main()
