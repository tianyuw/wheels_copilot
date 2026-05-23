from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.price_space_breaks import (
    PRICE_SPACE_BREAK_ALLOW_REAL_GAP,
    PRICE_SPACE_BREAK_BLOCK,
    PRICE_SPACE_BREAK_RESET_LOOKBACK,
    PriceSpaceBreakClassifier,
    SplitEvent,
    StaticSplitEventProvider,
)


class PriceSpaceBreakClassifierTests(unittest.TestCase):
    def test_confirmed_forward_split_matches_observed_price_ratio(self):
        classifier = PriceSpaceBreakClassifier(
            split_provider=StaticSplitEventProvider(
                [
                    SplitEvent(
                        ticker="NVDA",
                        execution_date=date(2024, 6, 10),
                        split_from=1,
                        split_to=10,
                    )
                ]
            )
        )
        classifier.preload(["NVDA"], date(2024, 6, 1), date(2024, 6, 30))

        result = classifier.classify(
            ticker="NVDA",
            issue={
                "date": "2024-06-10",
                "previous_close": 1208.88,
                "close": 121.79,
                "ratio": 0.1007,
            },
        )

        self.assertEqual(result.category, "confirmed_split")
        self.assertEqual(result.action, PRICE_SPACE_BREAK_RESET_LOOKBACK)
        self.assertEqual(result.confidence, "high")

    def test_confirmed_reverse_split_resets_lookback(self):
        classifier = PriceSpaceBreakClassifier(
            split_provider=StaticSplitEventProvider(
                [
                    SplitEvent(
                        ticker="XYZ",
                        execution_date=date(2026, 1, 10),
                        split_from=10,
                        split_to=1,
                    )
                ]
            )
        )
        classifier.preload(["XYZ"], date(2026, 1, 1), date(2026, 1, 31))

        result = classifier.classify(
            ticker="XYZ",
            issue={
                "date": "2026-01-10",
                "previous_close": 1,
                "close": 10,
                "ratio": 10,
            },
        )

        self.assertEqual(result.category, "confirmed_reverse_split")
        self.assertEqual(result.action, PRICE_SPACE_BREAK_RESET_LOOKBACK)

    def test_no_matching_split_classifies_non_split_ratio_as_real_gap(self):
        classifier = PriceSpaceBreakClassifier(
            split_provider=StaticSplitEventProvider([])
        )
        classifier.preload(["APP"], date(2025, 1, 1), date(2025, 1, 31))

        result = classifier.classify(
            ticker="APP",
            issue={
                "date": "2025-01-10",
                "previous_close": 100,
                "open": 132,
                "close": 130,
                "ratio": 1.32,
                "ratio_basis": "open_to_previous_close",
            },
        )

        self.assertEqual(result.category, "real_gap_move")
        self.assertEqual(result.action, PRICE_SPACE_BREAK_ALLOW_REAL_GAP)
        self.assertEqual(result.confidence, "medium")

    def test_missing_split_for_common_split_ratio_stays_blocked(self):
        classifier = PriceSpaceBreakClassifier(
            split_provider=StaticSplitEventProvider([])
        )
        classifier.preload(["AAPL"], date(2026, 1, 1), date(2026, 1, 31))

        result = classifier.classify(
            ticker="AAPL",
            issue={
                "date": "2026-01-10",
                "previous_close": 100,
                "close": 50,
                "ratio": 0.5,
            },
        )

        self.assertEqual(result.category, "unknown_price_break")
        self.assertEqual(result.action, PRICE_SPACE_BREAK_BLOCK)

    def test_missing_three_for_two_split_ratio_stays_blocked(self):
        classifier = PriceSpaceBreakClassifier(
            split_provider=StaticSplitEventProvider([])
        )
        classifier.preload(["ABC"], date(2026, 1, 1), date(2026, 1, 31))

        result = classifier.classify(
            ticker="ABC",
            issue={
                "date": "2026-01-10",
                "previous_close": 90,
                "close": 60,
                "ratio": 2 / 3,
            },
        )

        self.assertEqual(result.category, "unknown_price_break")
        self.assertEqual(result.action, PRICE_SPACE_BREAK_BLOCK)


if __name__ == "__main__":
    unittest.main()
