from __future__ import annotations

import unittest
from datetime import date, timedelta

from wheels_copilot.config import load_config
from wheels_copilot.models import PriceBar
from wheels_copilot.support import _count_rejections, _count_touches, analyze_support


class SupportEngineTests(unittest.TestCase):
    def test_pivot_cluster_range_floor_confluence_scores_tradable_zone(self):
        cfg = load_config("config/markus_wheel.yaml")
        bars = _synthetic_sideways_after_uptrend()

        analysis = analyze_support(bars, cfg)

        self.assertTrue(analysis.trend.passed, analysis.trend.reasons)
        self.assertIsNotNone(analysis.selected_zone)
        assert analysis.selected_zone is not None
        self.assertGreaterEqual(analysis.selected_zone.score, 70)
        self.assertTrue(analysis.tradable, analysis.reasons)
        self.assertEqual(analysis.selected_zone.method, "pivot_cluster")
        self.assertGreaterEqual(analysis.selected_zone.touches, 2)

    def test_trend_filter_fails_below_sma200(self):
        cfg = load_config("config/markus_wheel.yaml")
        bars = _synthetic_sideways_after_uptrend()
        depressed = [
            PriceBar(
                date=b.date,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
            for b in bars
        ]
        for i in range(len(depressed) - 10, len(depressed)):
            b = depressed[i]
            depressed[i] = PriceBar(
                date=b.date,
                open=80,
                high=81,
                low=79,
                close=80,
                volume=b.volume,
            )

        analysis = analyze_support(depressed, cfg)

        self.assertFalse(analysis.trend.passed)
        self.assertTrue(any("SMA200" in r for r in analysis.trend.reasons))

    def test_touch_and_rejection_count_require_actual_zone_test(self):
        bars = [
            _bar(0, low=95, high=100, close=99),  # far below, should not count
            _bar(1, low=104.5, high=108, close=107),  # touch + reject
            _bar(2, low=105.5, high=108, close=106),  # touch, no reject above top
            _bar(3, low=110, high=112, close=111),  # above, should not count
        ]

        self.assertEqual(_count_touches(bars, bottom=104, top=105), 1)
        self.assertEqual(_count_rejections(bars, bottom=104, top=105), 1)

    def test_current_price_inside_support_zone_is_not_discarded(self):
        cfg = load_config("config/markus_wheel.yaml")
        bars = _synthetic_sideways_after_uptrend()
        last = bars[-1]
        bars[-1] = PriceBar(
            date=last.date,
            open=106,
            high=107,
            low=105,
            close=105.2,
            volume=last.volume,
        )

        analysis = analyze_support(bars, cfg)

        self.assertIsNotNone(analysis.selected_zone)
        assert analysis.selected_zone is not None
        self.assertLessEqual(analysis.selected_zone.bottom, analysis.current_price)

    def test_binary_override_scores_non_pivot_detector_as_tradable(self):
        cfg = load_config("config/markus_wheel.yaml")
        cfg["support"]["pivot_cluster"]["enabled"] = False
        cfg["support"]["range_box"]["enabled"] = False
        cfg["support"]["moving_average_supports"]["enabled"] = False
        cfg["support"]["lowest_low_reference"]["enabled"] = False
        cfg["support"]["bollinger_lower_band"]["enabled"] = True
        cfg["support"]["scoring"]["mode"] = "binary_override"
        bars = _synthetic_sideways_after_uptrend()

        analysis = analyze_support(bars, cfg)

        self.assertTrue(analysis.trend.passed, analysis.trend.reasons)
        self.assertIsNotNone(analysis.selected_zone)
        assert analysis.selected_zone is not None
        self.assertEqual(analysis.selected_zone.method, "bollinger_lower_band")
        self.assertEqual(analysis.selected_zone.score, 100)
        self.assertTrue(analysis.tradable, analysis.reasons)

    def test_random_override_scores_are_reproducible_and_seeded(self):
        cfg = load_config("config/markus_wheel.yaml")
        cfg["support"]["scoring"]["mode"] = "random_override"
        cfg["support"]["scoring"]["random_seed"] = 17
        bars = _synthetic_sideways_after_uptrend()

        first = analyze_support(bars, cfg)
        second = analyze_support(bars, cfg)
        cfg["support"]["scoring"]["random_seed"] = 23
        third = analyze_support(bars, cfg)

        first_scores = [round(zone.score, 8) for zone in first.zones]
        second_scores = [round(zone.score, 8) for zone in second.zones]
        third_scores = [round(zone.score, 8) for zone in third.zones]
        self.assertEqual(first_scores, second_scores)
        self.assertNotEqual(first_scores, third_scores)
        self.assertTrue(all(0 <= score <= 100 for score in first_scores))

    def test_rsi_precondition_blocks_otherwise_tradable_support(self):
        cfg = load_config("config/markus_wheel.yaml")
        cfg["support"]["preconditions"]["enabled"] = True
        cfg["support"]["preconditions"]["max_rsi14"] = 10.0
        bars = _synthetic_sideways_after_uptrend()

        analysis = analyze_support(bars, cfg)

        self.assertFalse(analysis.preconditions_passed)
        self.assertFalse(analysis.tradable)
        self.assertTrue(any("rsi14" in reason for reason in analysis.reasons))
        self.assertIsNotNone(analysis.selected_zone)

    def test_bollinger_touch_precondition_requires_recent_pullback(self):
        cfg = load_config("config/markus_wheel.yaml")
        cfg["support"]["preconditions"]["enabled"] = True
        cfg["support"]["preconditions"]["require_recent_bollinger_touch"] = True
        cfg["support"]["preconditions"]["bollinger_touch_lookback_days"] = 5
        cfg["support"]["preconditions"]["bollinger_touch_tolerance_atr_multiple"] = 0.0
        cfg["support"]["preconditions"]["bollinger_touch_tolerance_pct"] = 0.0
        bars = _synthetic_smooth_uptrend()

        analysis = analyze_support(bars, cfg)

        self.assertFalse(analysis.preconditions_passed)
        self.assertFalse(analysis.tradable)
        self.assertIn("no_recent_bollinger_lower_touch", analysis.reasons)

    def test_bollinger_position_precondition_blocks_upper_band_extension(self):
        cfg = load_config("config/markus_wheel.yaml")
        cfg["support"]["preconditions"]["enabled"] = True
        cfg["support"]["preconditions"]["max_bollinger_position"] = 0.33
        bars = _synthetic_smooth_uptrend()

        analysis = analyze_support(bars, cfg)

        self.assertFalse(analysis.preconditions_passed)
        self.assertFalse(analysis.tradable)
        self.assertTrue(
            any("bollinger_position" in reason for reason in analysis.reasons)
        )
        self.assertGreater(
            analysis.precondition_metrics["bollinger_position"],
            0.33,
        )

    def test_context_adjustment_bonus_is_added_to_support_score(self):
        cfg = load_config("config/markus_wheel.yaml")
        bars = _synthetic_sideways_after_uptrend()
        base = analyze_support(bars, cfg)
        assert base.selected_zone is not None
        cfg["support"]["scoring"]["context_adjustments"]["enabled"] = True
        cfg["support"]["scoring"]["context_adjustments"]["return_10d_bonus_max_pct"] = 999.0
        cfg["support"]["scoring"]["context_adjustments"]["return_10d_bonus"] = 20.0

        adjusted = analyze_support(bars, cfg)

        assert adjusted.selected_zone is not None
        self.assertAlmostEqual(
            adjusted.selected_zone.score,
            base.selected_zone.score + 20.0,
        )
        self.assertIn("10d-return-contained bonus", adjusted.selected_zone.reasons)

    def test_context_adjustment_penalty_reduces_support_score(self):
        cfg = load_config("config/markus_wheel.yaml")
        bars = _synthetic_sideways_after_uptrend()
        base = analyze_support(bars, cfg)
        assert base.selected_zone is not None
        cfg["support"]["scoring"]["context_adjustments"]["enabled"] = True
        cfg["support"]["scoring"]["context_adjustments"]["close_vs_sma50_penalty_min_pct"] = 0.0
        cfg["support"]["scoring"]["context_adjustments"]["close_vs_sma50_penalty"] = 15.0

        adjusted = analyze_support(bars, cfg)

        assert adjusted.selected_zone is not None
        self.assertAlmostEqual(
            adjusted.selected_zone.score,
            base.selected_zone.score - 15.0,
        )
        self.assertIn("sma50-extension-overheated penalty", adjusted.selected_zone.reasons)


def _synthetic_sideways_after_uptrend() -> list[PriceBar]:
    start = date(2025, 1, 1)
    bars: list[PriceBar] = []
    for i in range(240):
        d = start + timedelta(days=i)
        if i < 210:
            close = 90.0 + i * 0.08
        else:
            close = 106.5 + ((i % 6) - 3) * 0.25
        low = close - 1.0
        high = close + 1.0

        if i in {216, 226, 234}:
            low = 104.8
            close = 106.4
            high = 107.2
        if i in {215, 217, 225, 227, 233, 235}:
            low = 105.8

        bars.append(
            PriceBar(
                date=d,
                open=close,
                high=high,
                low=low,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def _synthetic_smooth_uptrend() -> list[PriceBar]:
    start = date(2025, 1, 1)
    bars: list[PriceBar] = []
    for i in range(240):
        d = start + timedelta(days=i)
        close = 80.0 + i * 0.15
        bars.append(
            PriceBar(
                date=d,
                open=close,
                high=close + 0.4,
                low=close - 0.4,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def _bar(offset: int, low: float, high: float, close: float) -> PriceBar:
    return PriceBar(
        date=date(2025, 1, 1) + timedelta(days=offset),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
    )


if __name__ == "__main__":
    unittest.main()
