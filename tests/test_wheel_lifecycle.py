from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    PortfolioSnapshot,
)
from wheels_copilot.wheel_lifecycle import build_wheel_lifecycle_snapshot


class WheelLifecycleTests(unittest.TestCase):
    def test_long_stock_after_put_assignment_is_assigned_and_cc_eligible(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=490000),
            positions=[
                BrokerPosition(
                    symbol="AAPL",
                    qty=100,
                    asset_class="us_equity",
                    market_value=10000,
                    cost_basis=9000,
                )
            ],
            open_orders=[],
            source="test",
        )
        lifecycle = build_wheel_lifecycle_snapshot(
            portfolio,
            ledger_positions=[
                {
                    "ticker": "AAPL",
                    "symbol": "AAPL260529P00090000",
                    "option_type": "put",
                    "strike": 90,
                    "qty": 1,
                    "entry_price": 1.2,
                    "status": "ASSIGNED",
                }
            ],
            as_of=date(2026, 5, 21),
        )

        position = lifecycle["positions"][0]
        self.assertEqual(position["state"], "ASSIGNED")
        self.assertTrue(position["covered_call_eligible"])
        self.assertEqual(position["share_cost_basis"], 90)
        self.assertEqual(position["adjusted_cost_basis"], 88.8)
        self.assertEqual(position["premium_context"]["csp_credit_per_share"], 1.2)

    def test_unassigned_historical_put_premium_does_not_reduce_stock_basis(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=490000),
            positions=[
                BrokerPosition(
                    symbol="F",
                    qty=100,
                    asset_class="us_equity",
                    market_value=1200,
                    cost_basis=1100,
                )
            ],
            open_orders=[],
            source="test",
        )
        lifecycle = build_wheel_lifecycle_snapshot(
            portfolio,
            ledger_positions=[
                {
                    "ticker": "F",
                    "symbol": "F260529P00011000",
                    "option_type": "put",
                    "strike": 11,
                    "qty": 1,
                    "entry_price": 0.4,
                    "status": "EXPIRED",
                },
                {
                    "ticker": "F",
                    "symbol": "F260605P00011000",
                    "option_type": "put",
                    "strike": 11,
                    "qty": 1,
                    "entry_price": 0.5,
                    "status": "OPEN",
                },
            ],
            as_of=date(2026, 5, 21),
        )

        position = lifecycle["positions"][0]
        self.assertEqual(position["share_cost_basis"], 11)
        self.assertEqual(position["adjusted_cost_basis"], 11)
        self.assertEqual(position["premium_context"]["csp_credit_per_share"], 0)
        self.assertEqual(
            position["premium_context"]["unattributed_csp_credit_total"],
            90,
        )
        self.assertIn("csp_credit_attribution_unavailable", position["reasons"])

    def test_called_away_call_credit_does_not_reduce_remaining_stock_basis(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=490000),
            positions=[
                BrokerPosition(
                    symbol="AAPL",
                    qty=100,
                    asset_class="us_equity",
                    market_value=10000,
                    cost_basis=9000,
                )
            ],
            open_orders=[],
            source="test",
        )
        lifecycle = build_wheel_lifecycle_snapshot(
            portfolio,
            ledger_positions=[
                {
                    "ticker": "AAPL",
                    "symbol": "AAPL260529C00095000",
                    "option_type": "call",
                    "strike": 95,
                    "qty": 1,
                    "entry_price": 2.0,
                    "status": "CALLED_AWAY",
                }
            ],
            as_of=date(2026, 5, 30),
        )

        position = lifecycle["positions"][0]
        self.assertEqual(position["adjusted_cost_basis"], 90)
        self.assertEqual(position["premium_context"]["cc_credit_total"], 0)
        self.assertEqual(position["premium_context"]["called_away_cc_credit_total"], 200)

    def test_open_call_marks_assigned_stock_as_cc_open(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=490000),
            positions=[
                BrokerPosition(
                    symbol="AAPL",
                    qty=100,
                    asset_class="us_equity",
                    market_value=10000,
                    cost_basis=9000,
                )
            ],
            open_orders=[
                BrokerOrder(
                    id="order-1",
                    symbol="AAPL260529C00095000",
                    side="sell",
                    qty=1,
                    status="new",
                    position_intent="sell_to_open",
                    underlying_symbol="AAPL",
                    option_type="call",
                    expiration=date(2026, 5, 29),
                    strike=95,
                )
            ],
            source="test",
        )

        lifecycle = build_wheel_lifecycle_snapshot(portfolio, as_of=date(2026, 5, 21))

        position = lifecycle["positions"][0]
        self.assertEqual(position["state"], "CC_OPEN")
        self.assertFalse(position["covered_call_eligible"])
        self.assertEqual(position["available_shares_for_cc"], 0)

    def test_unresolved_ledger_call_blocks_new_covered_call(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=490000),
            positions=[
                BrokerPosition(
                    symbol="AAPL",
                    qty=100,
                    asset_class="us_equity",
                    market_value=10000,
                    cost_basis=9000,
                )
            ],
            open_orders=[],
            source="test",
        )

        lifecycle = build_wheel_lifecycle_snapshot(
            portfolio,
            ledger_positions=[
                {
                    "ticker": "AAPL",
                    "symbol": "AAPL260529C00095000",
                    "option_type": "call",
                    "strike": 95,
                    "qty": 1,
                    "entry_price": 1.0,
                    "status": "OPEN",
                }
            ],
            as_of=date(2026, 5, 21),
        )

        position = lifecycle["positions"][0]
        self.assertEqual(position["state"], "CC_OPEN")
        self.assertFalse(position["covered_call_eligible"])
        self.assertEqual(position["ledger_open_short_call_contracts"], 1)
        self.assertIn("ledger_open_short_call_reconciliation_required", position["reasons"])

    def test_partially_covered_stock_remains_eligible_for_uncovered_lot(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=490000),
            positions=[
                BrokerPosition(
                    symbol="AAPL",
                    qty=200,
                    asset_class="us_equity",
                    market_value=20000,
                    cost_basis=18000,
                )
            ],
            open_orders=[
                BrokerOrder(
                    id="order-1",
                    symbol="AAPL260529C00095000",
                    side="sell",
                    qty=1,
                    status="new",
                    position_intent="sell_to_open",
                    underlying_symbol="AAPL",
                    option_type="call",
                    expiration=date(2026, 5, 29),
                    strike=95,
                )
            ],
            source="test",
        )

        lifecycle = build_wheel_lifecycle_snapshot(portfolio, as_of=date(2026, 5, 21))

        position = lifecycle["positions"][0]
        self.assertEqual(position["state"], "CC_OPEN")
        self.assertTrue(position["covered_call_eligible"])
        self.assertEqual(position["available_shares_for_cc"], 100)
        self.assertIn("additional_uncovered_share_lot_detected", position["reasons"])

    def test_short_put_without_stock_is_csp_open(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=490000),
            positions=[
                BrokerPosition(
                    symbol="AAPL260529P00090000",
                    qty=-1,
                    asset_class="us_option",
                    side="short",
                    underlying_symbol="AAPL",
                    option_type="put",
                    expiration=date(2026, 5, 29),
                    strike=90,
                )
            ],
            open_orders=[],
            source="test",
        )

        lifecycle = build_wheel_lifecycle_snapshot(portfolio, as_of=date(2026, 5, 21))

        position = lifecycle["positions"][0]
        self.assertEqual(position["state"], "CSP_OPEN")
        self.assertFalse(position["covered_call_eligible"])


if __name__ == "__main__":
    unittest.main()
