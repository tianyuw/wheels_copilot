from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.config import load_config
from wheels_copilot.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    CspCandidate,
    OptionQuote,
    PortfolioSnapshot,
    SupportZone,
)
from wheels_copilot.portfolio_risk import evaluate_portfolio_risk


class PortfolioRiskTests(unittest.TestCase):
    def test_passes_with_cash_and_no_existing_exposure(self):
        gate, risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=100),
            _portfolio(cash=500000, equity=500000),
            load_config("config/markus_wheel.yaml"),
            required=True,
        )

        self.assertEqual(gate.status, "PASS")
        self.assertEqual(risk["assignment_cash_required"], 10000)
        self.assertEqual(risk["projected_reserved_assignment_cash"], 10000)

    def test_rejects_existing_short_put_or_open_sell_put_order(self):
        portfolio = _portfolio(
            positions=[
                BrokerPosition(
                    symbol="AAPL260522P00100000",
                    qty=-1,
                    underlying_symbol="AAPL",
                    option_type="put",
                    expiration=date(2026, 5, 22),
                    strike=100,
                )
            ],
            open_orders=[
                BrokerOrder(
                    id="1",
                    symbol="AAPL260529P00095000",
                    side="sell",
                    qty=1,
                    underlying_symbol="AAPL",
                    option_type="put",
                    expiration=date(2026, 5, 29),
                    strike=95,
                )
            ],
        )

        gate, risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=90),
            portfolio,
            load_config("config/markus_wheel.yaml"),
            required=True,
        )

        self.assertEqual(gate.status, "REJECT")
        self.assertIn("existing_short_put_position", gate.reasons)
        self.assertIn("duplicate_open_short_put_order", gate.reasons)
        self.assertEqual(risk["existing_short_put_assignment_cash"], 10000)
        self.assertEqual(risk["existing_open_sell_put_assignment_cash"], 9500)

    def test_long_put_and_sell_to_close_do_not_reserve_assignment_cash(self):
        portfolio = _portfolio(
            positions=[
                BrokerPosition(
                    symbol="AAPL260522P00100000",
                    qty=1,
                    underlying_symbol="AAPL",
                    option_type="put",
                    expiration=date(2026, 5, 22),
                    strike=100,
                )
            ],
            open_orders=[
                BrokerOrder(
                    id="1",
                    symbol="AAPL260522P00100000",
                    side="sell",
                    qty=1,
                    position_intent="sell_to_close",
                    underlying_symbol="AAPL",
                    option_type="put",
                    expiration=date(2026, 5, 22),
                    strike=100,
                )
            ],
        )

        gate, risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=90),
            portfolio,
            load_config("config/markus_wheel.yaml"),
            required=True,
        )

        self.assertEqual(gate.status, "PASS")
        self.assertEqual(risk["existing_short_put_assignment_cash"], 0)
        self.assertEqual(risk["existing_open_sell_put_assignment_cash"], 0)
        self.assertEqual(risk["reserved_assignment_cash"], 0)

    def test_rejects_existing_covered_stock_position(self):
        portfolio = _portfolio(
            positions=[
                BrokerPosition(symbol="AAPL", qty=100, asset_class="us_equity"),
            ]
        )

        gate, _risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=90),
            portfolio,
            load_config("config/markus_wheel.yaml"),
            required=True,
        )

        self.assertEqual(gate.status, "REJECT")
        self.assertIn("covered_call_workflow_required_existing_100_shares", gate.reasons)

    def test_rejects_existing_short_stock_position(self):
        portfolio = _portfolio(
            positions=[
                BrokerPosition(symbol="AAPL", qty=-10, asset_class="us_equity"),
            ]
        )

        gate, _risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=90),
            portfolio,
            load_config("config/markus_wheel.yaml"),
            required=True,
        )

        self.assertEqual(gate.status, "REJECT")
        self.assertIn("existing_short_underlying_position", gate.reasons)

    def test_rejects_cash_buffer_violation(self):
        gate, risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=100),
            _portfolio(cash=20000, equity=100000),
            load_config("config/markus_wheel.yaml"),
            required=True,
        )

        self.assertEqual(gate.status, "REJECT")
        self.assertTrue(
            any(reason.startswith("cash_buffer_after_assignment_below_min") for reason in gate.reasons)
        )
        self.assertEqual(risk["projected_cash_after_reserve"], 10000)

    def test_rejects_blocked_or_inactive_account(self):
        portfolio = PortfolioSnapshot(
            account=BrokerAccountSnapshot(
                status="ACCOUNT_UPDATED",
                equity=500000,
                cash=500000,
                buying_power=500000,
                trading_blocked=True,
            ),
            positions=[],
            open_orders=[],
        )

        gate, _risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=90),
            portfolio,
            load_config("config/markus_wheel.yaml"),
            required=True,
        )

        self.assertEqual(gate.status, "REJECT")
        self.assertIn("account_status_ACCOUNT_UPDATED", gate.reasons)
        self.assertIn("trading_blocked", gate.reasons)

    def test_required_missing_portfolio_is_manual_review(self):
        gate, risk = evaluate_portfolio_risk(
            "AAPL",
            _candidate(strike=100),
            None,
            load_config("config/markus_wheel.yaml"),
            required=True,
            portfolio_error="AlpacaRequestError: down",
        )

        self.assertEqual(gate.status, "WARN")
        self.assertTrue(gate.manual_review_required)
        self.assertEqual(risk["portfolio_error"], "AlpacaRequestError: down")


def _portfolio(
    *,
    cash: float = 500000,
    equity: float = 500000,
    positions: list[BrokerPosition] | None = None,
    open_orders: list[BrokerOrder] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account=BrokerAccountSnapshot(
            status="ACTIVE",
            equity=equity,
            cash=cash,
            buying_power=cash,
        ),
        positions=positions or [],
        open_orders=open_orders or [],
        source="test",
    )


def _candidate(strike: float) -> CspCandidate:
    option = OptionQuote(
        symbol=f"AAPL260522P{int(strike * 1000):08d}",
        expiration=date(2026, 5, 22),
        dte=7,
        strike=strike,
        bid=1,
        ask=1.1,
        last=1,
        delta=-0.2,
    )
    return CspCandidate(
        option=option,
        support_zone=SupportZone(
            method="test",
            center=strike,
            bottom=strike,
            top=strike,
            score=90,
        ),
        delta=-0.2,
        delta_bucket="strong_support",
        auto_trade=True,
        weekly_return_on_strike_pct=1.0,
        assignment_cash_required=strike * 100,
    )


if __name__ == "__main__":
    unittest.main()
