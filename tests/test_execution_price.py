from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.execution_price import (
    BacktestExecutionModel,
    SyntheticSpreadConfig,
    entry_fill_price,
    modeled_quote_from_reference,
    synthetic_spread_pct,
)
from wheels_copilot.models import OptionQuote


class ExecutionPriceTests(unittest.TestCase):
    def test_synthetic_spread_adds_liquidity_and_moneyness_penalties_then_caps(self):
        spread = synthetic_spread_pct(
            reference_price=0.25,
            option_type="put",
            strike=80,
            stock_price=100,
            volume=10,
            config=SyntheticSpreadConfig(
                min_spread_pct_of_mid=0.08,
                max_spread_pct_of_mid=0.18,
                low_premium_threshold=0.50,
                low_premium_extra_pct=0.05,
                low_volume_threshold=50,
                low_volume_extra_pct=0.05,
                wide_otm_pct_threshold=0.15,
                wide_otm_extra_pct=0.03,
            ),
        )

        self.assertAlmostEqual(spread, 0.18)

    def test_modeled_quote_zero_spread_legacy_applies_reference_adjustment(self):
        bid, ask, spread = modeled_quote_from_reference(
            reference_price=1.00,
            option_type="put",
            strike=95,
            stock_price=100,
            volume=100,
            execution_model=BacktestExecutionModel(
                model="zero_spread_legacy",
                reference_price_adjustment_pct=0.05,
            ),
        )

        self.assertAlmostEqual(bid, 0.95)
        self.assertAlmostEqual(ask, 0.95)
        self.assertAlmostEqual(spread, 0.0)

    def test_entry_fill_policy_mid_uses_executable_mid(self):
        option = OptionQuote(
            symbol="AAPL260109P00095000",
            expiration=date(2026, 1, 9),
            dte=4,
            strike=95,
            bid=0.90,
            ask=1.10,
            last=1.00,
        )

        fill = entry_fill_price(option, BacktestExecutionModel(fill_policy="mid"))

        self.assertAlmostEqual(fill or 0.0, 1.00)


if __name__ == "__main__":
    unittest.main()
