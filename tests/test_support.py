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
