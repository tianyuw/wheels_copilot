from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.config import load_config
from wheels_copilot.csp_selector import evaluate_csp_candidates, select_csp_candidate
from wheels_copilot.models import (
    OptionQuote,
    SupportAnalysis,
    SupportZone,
    TrendCheck,
)


class CspSelectorTests(unittest.TestCase):
    def test_strong_support_uses_higher_delta_bucket(self):
        cfg = load_config("config/markus_wheel.yaml")
        support = _support(score=88)
        options = [
            _put(strike=90, delta=-0.20, bid=1.00, ask=1.10),
            _put(strike=91, delta=-0.35, bid=1.50, ask=1.65),
        ]

        candidate = select_csp_candidate(options, support, cfg)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.delta_bucket, "strong_support")
        self.assertAlmostEqual(candidate.option.strike, 90)
        self.assertTrue(candidate.auto_trade)

    def test_strong_support_prefers_lower_delta_after_premium_floor(self):
        cfg = load_config("config/markus_wheel.yaml")
        support = _support(score=88)
        options = [
            _put(strike=89, delta=-0.28, bid=2.00, ask=2.10),
            _put(strike=88, delta=-0.16, bid=0.80, ask=0.90),
        ]

        candidate = select_csp_candidate(options, support, cfg)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertAlmostEqual(candidate.option.strike, 88)
        self.assertAlmostEqual(abs(candidate.delta), 0.16)

    def test_normal_support_rejects_aggressive_delta(self):
        cfg = load_config("config/markus_wheel.yaml")
        support = _support(score=75)
        options = [_put(strike=90, delta=-0.30, bid=1.00, ask=1.10)]

        candidate = select_csp_candidate(options, support, cfg)

        self.assertIsNone(candidate)

    def test_weak_support_candidate_is_manual_review_not_auto_trade(self):
        cfg = load_config("config/markus_wheel.yaml")
        support = _support(score=60)
        options = [_put(strike=90, delta=-0.30, bid=1.00, ask=1.10)]

        result = evaluate_csp_candidates(options, support, cfg)

        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.delta_bucket, "manual_review")
        self.assertFalse(result.candidate.auto_trade)

    def test_inside_support_zone_is_watch_only_in_paper(self):
        cfg = load_config("config/markus_wheel.yaml")
        support = _support(score=88)
        options = [_put(strike=92, delta=-0.20, bid=1.00, ask=1.10)]

        result = evaluate_csp_candidates(options, support, cfg)

        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertFalse(result.candidate.auto_trade)
        self.assertTrue(result.candidate.diagnostics["inside_zone_watch_only"])

    def test_rejection_summary_explains_no_candidate(self):
        cfg = load_config("config/markus_wheel.yaml")
        support = _support(score=75)
        options = [_put(strike=99, delta=-0.30, bid=0.01, ask=0.02, open_interest=1)]

        result = evaluate_csp_candidates(options, support, cfg)

        self.assertIsNone(result.candidate)
        self.assertGreater(result.rejection_summary["delta_too_high"], 0)
        self.assertGreater(result.rejection_summary["bid_below_min"], 0)
        self.assertGreater(result.rejection_summary["open_interest_below_min"], 0)

    def test_assignment_cash_limit_rejects_oversized_ticker(self):
        cfg = load_config("config/markus_wheel.yaml")
        support = _support(score=88)
        options = [_put(strike=800, delta=-0.20, bid=1.00, ask=1.10)]

        result = evaluate_csp_candidates(options, support, cfg)

        self.assertIsNone(result.candidate)
        self.assertGreater(
            result.rejection_summary["assignment_cash_above_single_ticker_limit"], 0
        )


def _support(score: float) -> SupportAnalysis:
    zone = SupportZone(
        method="pivot_cluster",
        center=92,
        bottom=91,
        top=93,
        touches=3,
        rejections=3,
        score=score,
    )
    return SupportAnalysis(
        trend=TrendCheck(
            passed=True,
            current_price=100,
            sma200=95,
            sma200_slope=1,
        ),
        zones=[zone],
        selected_zone=zone,
        atr14=2.0,
        current_price=100,
        min_score_to_trade=70,
    )


def _put(
    strike: float,
    delta: float,
    bid: float,
    ask: float,
    open_interest: int = 500,
) -> OptionQuote:
    return OptionQuote(
        symbol=f"TEST260529P{int(strike * 1000):08d}",
        expiration=date(2026, 5, 29),
        dte=7,
        strike=strike,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        implied_volatility=0.25,
        open_interest=open_interest,
        volume=100,
        delta=delta,
    )


if __name__ == "__main__":
    unittest.main()
