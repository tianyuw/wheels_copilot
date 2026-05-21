from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.covered_call_planner import (
    build_covered_call_proposals,
    build_covered_call_shadow_orders,
)
from wheels_copilot.models import OptionQuote


class CoveredCallPlannerTests(unittest.TestCase):
    def test_assigned_position_creates_cost_basis_safe_covered_call(self):
        proposals = build_covered_call_proposals(
            _lifecycle(
                [
                    {
                        "ticker": "AAPL",
                        "state": "ASSIGNED",
                        "covered_call_eligible": True,
                        "long_shares": 100,
                        "available_shares_for_cc": 100,
                        "adjusted_cost_basis": 88.8,
                    }
                ]
            ),
            _config(),
            as_of=date(2026, 5, 21),
            option_chain_by_ticker={
                "AAPL": [
                    _call("AAPL260529C00085000", strike=85, delta=0.2),
                    _call("AAPL260529C00090000", strike=90, delta=0.22),
                    _call("AAPL260529C00095000", strike=95, delta=0.45),
                ]
            },
        )
        orders = build_covered_call_shadow_orders(proposals, _config())

        self.assertEqual(proposals["summary"], {"PROPOSED": 1})
        proposal = proposals["proposals"][0]
        self.assertEqual(proposal["decision"], "PROPOSED")
        self.assertEqual(proposal["option"]["symbol"], "AAPL260529C00090000")
        self.assertGreaterEqual(proposal["option"]["strike"], proposal["adjusted_cost_basis"])
        self.assertEqual(proposal["estimated_premium_credit"], 52.5)
        self.assertEqual(
            proposal["unchecked_risks"],
            ["earnings_not_checked", "ex_dividend_not_checked"],
        )

        self.assertEqual(orders["order_count"], 1)
        payload = orders["orders"][0]["payload"]
        self.assertEqual(payload["symbol"], "AAPL260529C00090000")
        self.assertEqual(payload["side"], "sell")
        self.assertEqual(payload["position_intent"], "sell_to_open")
        self.assertTrue(payload["client_order_id"].startswith("whcc-"))
        self.assertEqual(orders["orders"][0]["adjusted_cost_basis"], 88.8)
        self.assertEqual(orders["orders"][0]["available_shares_for_cc"], 100)
        self.assertEqual(
            orders["orders"][0]["unchecked_risks"],
            ["earnings_not_checked", "ex_dividend_not_checked"],
        )

    def test_no_eligible_call_keeps_position_on_watch(self):
        proposals = build_covered_call_proposals(
            _lifecycle(
                [
                    {
                        "ticker": "AAPL",
                        "state": "ASSIGNED",
                        "covered_call_eligible": True,
                        "long_shares": 100,
                        "available_shares_for_cc": 100,
                        "adjusted_cost_basis": 100.0,
                    }
                ]
            ),
            _config(),
            as_of=date(2026, 5, 21),
            option_chain_by_ticker={
                "AAPL": [
                    _call("AAPL260529C00095000", strike=95, delta=0.2),
                    _call("AAPL260529C00105000", strike=105, delta=0.5),
                ]
            },
        )
        orders = build_covered_call_shadow_orders(proposals, _config())

        self.assertEqual(proposals["summary"], {"WATCH": 1})
        proposal = proposals["proposals"][0]
        self.assertTrue(proposal["requires_manual_review"])
        self.assertIn("strike_below_adjusted_cost_basis", proposal["rejection_summary"])
        self.assertIn("delta_outside_target", proposal["rejection_summary"])
        self.assertEqual(orders["order_count"], 0)

    def test_watch_proposal_ids_include_position_context(self):
        proposals = build_covered_call_proposals(
            _lifecycle(
                [
                    {
                        "ticker": "AAPL",
                        "state": "ASSIGNED",
                        "covered_call_eligible": True,
                        "long_shares": 100,
                        "available_shares_for_cc": 100,
                        "adjusted_cost_basis": 100.0,
                    },
                    {
                        "ticker": "AAPL",
                        "state": "CC_OPEN",
                        "covered_call_eligible": True,
                        "long_shares": 200,
                        "available_shares_for_cc": 100,
                        "adjusted_cost_basis": 100.0,
                    },
                ]
            ),
            _config(),
            as_of=date(2026, 5, 21),
            option_chain_by_ticker={"AAPL": []},
        )

        ids = [proposal["proposal_id"] for proposal in proposals["proposals"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_non_assigned_positions_are_audited_not_proposed(self):
        proposals = build_covered_call_proposals(
            _lifecycle(
                [
                    {
                        "ticker": "AAPL",
                        "state": "CSP_OPEN",
                        "covered_call_eligible": False,
                        "long_shares": 0,
                        "available_shares_for_cc": 0,
                        "adjusted_cost_basis": None,
                    }
                ]
            ),
            _config(),
            as_of=date(2026, 5, 21),
            option_chain_by_ticker={"AAPL": []},
        )

        self.assertEqual(proposals["proposal_count"], 0)
        self.assertEqual(proposals["audit_count"], 1)


def _config() -> dict:
    return {
        "cc_selector": {
            "dte_min": 1,
            "dte_max": 9,
            "min_strike_vs_cost_basis_pct": 0.0,
            "target_delta_min": 0.10,
            "target_delta_max": 0.35,
            "min_bid": 0.10,
            "max_spread_pct_of_mid": 0.15,
            "min_open_interest": 50,
        },
        "trade_planner": {
            "shadow_order": {
                "broker": "alpaca",
                "type": "limit",
                "time_in_force": "day",
            }
        },
    }


def _lifecycle(positions: list[dict]) -> dict:
    return {
        "as_of": "2026-05-21",
        "generated_at": "2026-05-21T10:00:00",
        "summary": {},
        "positions": positions,
    }


def _call(
    symbol: str,
    *,
    strike: float,
    delta: float,
    bid: float = 0.5,
    ask: float = 0.55,
    open_interest: int = 100,
) -> OptionQuote:
    return OptionQuote(
        symbol=symbol,
        expiration=date(2026, 5, 29),
        dte=8,
        strike=strike,
        bid=bid,
        ask=ask,
        last=bid,
        delta=delta,
        open_interest=open_interest,
        volume=10,
        data_feed="opra",
    )


if __name__ == "__main__":
    unittest.main()
