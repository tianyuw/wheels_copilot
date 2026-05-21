from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.config import load_config
from wheels_copilot.gates import (
    evaluate_covered_call_earnings_gate,
    evaluate_covered_call_ex_dividend_gate,
    evaluate_earnings_gate,
    evaluate_fundamentals,
)
from wheels_copilot.models import FundamentalSnapshot, OptionQuote


class FundamentalGateTests(unittest.TestCase):
    def test_rejects_small_cap_high_pe_stock(self):
        cfg = load_config("config/markus_wheel.yaml")
        snapshot = _snapshot(market_cap=1_000_000_000, pe_ratio=80)

        result = evaluate_fundamentals(snapshot, cfg)

        self.assertEqual(result.status, "REJECT")
        self.assertIn("market_cap_below_2000000000", result.reasons)
        self.assertIn("pe_ratio_at_or_above_50", result.reasons)

    def test_rejects_negative_pe(self):
        cfg = load_config("config/markus_wheel.yaml")
        snapshot = _snapshot(pe_ratio=-5)

        result = evaluate_fundamentals(snapshot, cfg)

        self.assertEqual(result.status, "REJECT")
        self.assertIn("pe_ratio_non_positive", result.reasons)

    def test_four_positive_annual_values_can_pass_yfinance_limit(self):
        cfg = load_config("config/markus_wheel.yaml")
        snapshot = _snapshot(annual_net_income=[1, 1, 1, 1])

        result = evaluate_fundamentals(snapshot, cfg)

        self.assertEqual(result.status, "PASS")

    def test_rejects_biotech_chinese_adr_and_recent_meme_move(self):
        cfg = load_config("config/markus_wheel.yaml")
        snapshot = _snapshot(
            ticker="BABA",
            industry="Biotechnology",
            country="China",
            recent_move_pct=120,
        )

        result = evaluate_fundamentals(snapshot, cfg)

        self.assertEqual(result.status, "REJECT")
        self.assertIn("biotech_or_binary_event_industry", result.reasons)
        self.assertIn("chinese_adr", result.reasons)
        self.assertTrue(any(reason.startswith("recent_move_") for reason in result.reasons))

    def test_etf_skips_stock_profitability_checks_but_rejects_leveraged(self):
        cfg = load_config("config/markus_wheel.yaml")
        normal = _snapshot(
            ticker="IWM",
            quote_type="ETF",
            long_name="iShares Russell 2000 ETF",
            market_cap=None,
            pe_ratio=None,
            quarterly_net_income=[],
            annual_net_income=[],
        )
        leveraged = _snapshot(
            ticker="TQQQ",
            quote_type="ETF",
            long_name="ProShares UltraPro QQQ 3x ETF",
        )

        self.assertNotEqual(evaluate_fundamentals(normal, cfg).status, "REJECT")
        self.assertEqual(evaluate_fundamentals(leveraged, cfg).status, "REJECT")


class EarningsGateTests(unittest.TestCase):
    def test_filters_contracts_on_or_after_earnings(self):
        snapshot = _snapshot(next_earnings_date=date(2026, 5, 29))
        options = [
            _put(date(2026, 5, 22)),
            _put(date(2026, 5, 29)),
            _put(date(2026, 6, 5)),
        ]

        result, allowed = evaluate_earnings_gate(snapshot, options, as_of=date(2026, 5, 20))

        self.assertEqual(result.status, "PASS")
        self.assertFalse(result.manual_review_required)
        self.assertEqual([opt.expiration for opt in allowed], [date(2026, 5, 22)])

    def test_rejects_when_all_contracts_hit_earnings_window(self):
        snapshot = _snapshot(next_earnings_date=date(2026, 5, 22))
        options = [_put(date(2026, 5, 22)), _put(date(2026, 5, 29))]

        result, allowed = evaluate_earnings_gate(snapshot, options, as_of=date(2026, 5, 20))

        self.assertEqual(result.status, "REJECT")
        self.assertEqual(allowed, [])

    def test_unknown_earnings_date_requires_manual_review(self):
        snapshot = _snapshot(next_earnings_date=None)
        options = [_put(date(2026, 5, 22))]

        result, allowed = evaluate_earnings_gate(snapshot, options, as_of=date(2026, 5, 20))

        self.assertEqual(result.status, "WARN")
        self.assertTrue(result.manual_review_required)
        self.assertEqual(allowed, options)


class CoveredCallRiskGateTests(unittest.TestCase):
    def test_covered_call_earnings_must_be_after_expiration(self):
        option = _call(date(2026, 5, 29))

        passes = evaluate_covered_call_earnings_gate(
            _snapshot(next_earnings_date=date(2026, 8, 1)),
            option,
            as_of=date(2026, 5, 20),
        )
        blocked = evaluate_covered_call_earnings_gate(
            _snapshot(next_earnings_date=date(2026, 5, 29)),
            option,
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(passes.status, "PASS")
        self.assertEqual(blocked.status, "REJECT")
        self.assertIn(
            "cc_expiration_on_or_after_earnings:2026-05-29>=2026-05-29",
            blocked.reasons,
        )

    def test_covered_call_unknown_stock_earnings_blocks_but_etf_skips(self):
        stock = evaluate_covered_call_earnings_gate(
            _snapshot(next_earnings_date=None),
            _call(date(2026, 5, 29)),
            as_of=date(2026, 5, 20),
        )
        etf = evaluate_covered_call_earnings_gate(
            _snapshot(quote_type="ETF", long_name="Test ETF", next_earnings_date=None),
            _call(date(2026, 5, 29)),
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(stock.status, "REJECT")
        self.assertIn("cc_earnings_date_unknown", stock.reasons)
        self.assertEqual(etf.status, "PASS")
        self.assertIn("cc_earnings_not_applicable_etf", etf.reasons)

    def test_covered_call_stale_earnings_date_blocks(self):
        result = evaluate_covered_call_earnings_gate(
            _snapshot(next_earnings_date=date(2026, 5, 1)),
            _call(date(2026, 5, 29)),
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(result.status, "REJECT")
        self.assertIn("cc_earnings_date_stale:2026-05-01", result.reasons)

    def test_covered_call_ex_dividend_window_blocks(self):
        option = _call(date(2026, 5, 29))

        blocked = evaluate_covered_call_ex_dividend_gate(
            _snapshot(ex_dividend_date=date(2026, 5, 28), dividend_yield=0.02),
            option,
            as_of=date(2026, 5, 20),
        )
        passes = evaluate_covered_call_ex_dividend_gate(
            _snapshot(ex_dividend_date=date(2026, 6, 1), dividend_yield=0.02),
            option,
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(blocked.status, "REJECT")
        self.assertIn(
            "cc_ex_dividend_within_contract_window:2026-05-28<=2026-05-29",
            blocked.reasons,
        )
        self.assertEqual(passes.status, "PASS")

    def test_covered_call_dividend_payer_without_ex_date_blocks(self):
        result = evaluate_covered_call_ex_dividend_gate(
            _snapshot(ex_dividend_date=None, dividend_yield=0.02),
            _call(date(2026, 5, 29)),
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(result.status, "REJECT")
        self.assertIn("cc_ex_dividend_date_unknown_for_dividend_payer", result.reasons)

    def test_covered_call_dividend_payer_with_stale_ex_date_blocks(self):
        result = evaluate_covered_call_ex_dividend_gate(
            _snapshot(ex_dividend_date=date(2026, 5, 1), dividend_yield=0.02),
            _call(date(2026, 5, 29)),
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(result.status, "REJECT")
        self.assertIn("cc_ex_dividend_date_stale:2026-05-01", result.reasons)
        self.assertIn("cc_ex_dividend_date_unknown_for_dividend_payer", result.reasons)

    def test_covered_call_annual_dividend_rate_without_yield_still_blocks_unknown_ex_date(self):
        result = evaluate_covered_call_ex_dividend_gate(
            _snapshot(
                dividend_yield=None,
                annual_dividend_rate=2.0,
                ex_dividend_date=None,
            ),
            _call(date(2026, 5, 29)),
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(result.status, "REJECT")
        self.assertIn("cc_ex_dividend_date_unknown_for_dividend_payer", result.reasons)

    def test_covered_call_non_dividend_stock_ex_dividend_gate_passes(self):
        result = evaluate_covered_call_ex_dividend_gate(
            _snapshot(dividend_yield=None, annual_dividend_rate=None, ex_dividend_date=None),
            _call(date(2026, 5, 29)),
            as_of=date(2026, 5, 20),
        )

        self.assertEqual(result.status, "PASS")
        self.assertIn("cc_no_dividend_detected", result.reasons)


def _snapshot(
    ticker: str = "TEST",
    quote_type: str = "EQUITY",
    long_name: str = "Test Corp",
    industry: str = "Software",
    country: str = "United States",
    market_cap: float | None = 10_000_000_000,
    pe_ratio: float | None = 20,
    dividend_yield: float | None = 0.01,
    annual_dividend_rate: float | None = None,
    ex_dividend_date: date | None = date(2026, 6, 1),
    quarterly_net_income: list[float] | None = None,
    annual_net_income: list[float] | None = None,
    next_earnings_date: date | None = date(2026, 8, 1),
    recent_move_pct: float | None = 10,
) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        ticker=ticker,
        quote_type=quote_type,
        long_name=long_name,
        industry=industry,
        country=country,
        market_cap=market_cap,
        pe_ratio=pe_ratio,
        dividend_yield=dividend_yield,
        annual_dividend_rate=annual_dividend_rate,
        ex_dividend_date=ex_dividend_date,
        quarterly_net_income=quarterly_net_income or [1, 1, 1, 1, 1],
        annual_net_income=annual_net_income or [1, 1, 1, 1, 1],
        next_earnings_date=next_earnings_date,
        recent_move_pct=recent_move_pct,
    )


def _put(expiration: date) -> OptionQuote:
    return OptionQuote(
        symbol=f"TEST{expiration.isoformat()}P90",
        expiration=expiration,
        dte=(expiration - date(2026, 5, 20)).days,
        strike=90,
        bid=1,
        ask=1.1,
        last=1,
        implied_volatility=0.25,
        open_interest=100,
    )


def _call(expiration: date) -> OptionQuote:
    return OptionQuote(
        symbol=f"TEST{expiration.isoformat()}C90",
        expiration=expiration,
        dte=(expiration - date(2026, 5, 20)).days,
        strike=90,
        bid=1,
        ask=1.1,
        last=1,
        implied_volatility=0.25,
        open_interest=100,
    )


if __name__ == "__main__":
    unittest.main()
