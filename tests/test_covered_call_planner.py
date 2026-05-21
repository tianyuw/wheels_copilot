from __future__ import annotations

import json
import unittest
from datetime import date

from wheels_copilot.covered_call_planner import (
    build_covered_call_proposals,
    build_covered_call_shadow_orders,
)
from wheels_copilot.models import FundamentalSnapshot, OptionQuote


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
            fundamental_by_ticker={"AAPL": _fundamental()},
        )
        orders = build_covered_call_shadow_orders(proposals, _config())

        self.assertEqual(proposals["summary"], {"PROPOSED": 1})
        proposal = proposals["proposals"][0]
        self.assertEqual(proposal["decision"], "PROPOSED")
        self.assertEqual(proposal["option"]["symbol"], "AAPL260529C00090000")
        self.assertGreaterEqual(proposal["option"]["strike"], proposal["adjusted_cost_basis"])
        self.assertEqual(proposal["estimated_premium_credit"], 52.5)
        self.assertEqual(proposal["unchecked_risks"], [])
        self.assertEqual(proposal["earnings_gate"]["status"], "PASS")
        self.assertEqual(proposal["ex_dividend_gate"]["status"], "PASS")

        self.assertEqual(orders["order_count"], 1)
        payload = orders["orders"][0]["payload"]
        self.assertEqual(payload["symbol"], "AAPL260529C00090000")
        self.assertEqual(payload["side"], "sell")
        self.assertEqual(payload["position_intent"], "sell_to_open")
        self.assertTrue(payload["client_order_id"].startswith("whcc-"))
        self.assertEqual(orders["orders"][0]["adjusted_cost_basis"], 88.8)
        self.assertEqual(orders["orders"][0]["available_shares_for_cc"], 100)
        self.assertEqual(orders["orders"][0]["earnings_gate"]["status"], "PASS")
        self.assertEqual(orders["orders"][0]["ex_dividend_gate"]["status"], "PASS")
        self.assertEqual(orders["orders"][0]["unchecked_risks"], [])

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
            fundamental_by_ticker={"AAPL": _fundamental()},
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
            fundamental_by_ticker={"AAPL": _fundamental()},
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
            fundamental_by_ticker={"AAPL": _fundamental()},
        )

        self.assertEqual(proposals["proposal_count"], 0)
        self.assertEqual(proposals["audit_count"], 1)

    def test_covered_call_earnings_before_expiration_keeps_position_on_watch(self):
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
                "AAPL": [_call("AAPL260529C00090000", strike=90, delta=0.22)]
            },
            fundamental_by_ticker={"AAPL": _fundamental(next_earnings_date=date(2026, 5, 29))},
        )

        proposal = proposals["proposals"][0]
        self.assertEqual(proposal["decision"], "WATCH")
        self.assertIn("cc_expiration_on_or_after_earnings", proposal["rejection_summary"])
        self.assertEqual(build_covered_call_shadow_orders(proposals, _config())["order_count"], 0)

    def test_covered_call_ex_dividend_inside_window_keeps_position_on_watch(self):
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
                "AAPL": [_call("AAPL260529C00090000", strike=90, delta=0.22)]
            },
            fundamental_by_ticker={
                "AAPL": _fundamental(ex_dividend_date=date(2026, 5, 28))
            },
        )

        proposal = proposals["proposals"][0]
        self.assertEqual(proposal["decision"], "WATCH")
        self.assertIn("cc_ex_dividend_within_contract_window", proposal["rejection_summary"])
        self.assertEqual(build_covered_call_shadow_orders(proposals, _config())["order_count"], 0)

    def test_covered_call_etf_skips_earnings_but_checks_ex_dividend(self):
        proposals = build_covered_call_proposals(
            _lifecycle(
                [
                    {
                        "ticker": "IWM",
                        "state": "ASSIGNED",
                        "covered_call_eligible": True,
                        "long_shares": 100,
                        "available_shares_for_cc": 100,
                        "adjusted_cost_basis": 200.0,
                    }
                ]
            ),
            _config(),
            as_of=date(2026, 5, 21),
            option_chain_by_ticker={
                "IWM": [_call("IWM260529C00210000", strike=210, delta=0.22)]
            },
            fundamental_by_ticker={
                "IWM": _fundamental(
                    ticker="IWM",
                    quote_type="ETF",
                    next_earnings_date=None,
                    ex_dividend_date=date(2026, 6, 1),
                )
            },
        )

        proposal = proposals["proposals"][0]
        self.assertEqual(proposal["decision"], "PROPOSED")
        self.assertIn("cc_earnings_not_applicable_etf", proposal["decision_reasons"])
        self.assertEqual(proposal["unchecked_risks"], [])

    def test_covered_call_warn_risk_gate_stays_watch_and_does_not_create_order(self):
        cfg = _config()
        cfg["cc_risk"] = {
            "block_unknown_stock_earnings_date": False,
            "block_unknown_ex_dividend_date_for_dividend_payers": False,
        }

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
            cfg,
            as_of=date(2026, 5, 21),
            option_chain_by_ticker={
                "AAPL": [_call("AAPL260529C00090000", strike=90, delta=0.22)]
            },
            fundamental_by_ticker={
                "AAPL": _fundamental(
                    next_earnings_date=None,
                    dividend_yield=0.01,
                    ex_dividend_date=None,
                )
            },
        )

        proposal = proposals["proposals"][0]
        self.assertEqual(proposal["decision"], "WATCH")
        self.assertIn("cc_earnings_date_unknown", proposal["rejection_summary"])
        self.assertIn(
            "cc_ex_dividend_date_unknown_for_dividend_payer",
            proposal["rejection_summary"],
        )
        self.assertEqual(build_covered_call_shadow_orders(proposals, cfg)["order_count"], 0)

    def test_covered_call_shadow_order_metadata_is_json_serializable(self):
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
                "AAPL": [_call("AAPL260529C00090000", strike=90, delta=0.22)]
            },
            fundamental_by_ticker={"AAPL": _fundamental()},
        )
        orders = build_covered_call_shadow_orders(proposals, _config())

        json.dumps(orders)


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


def _fundamental(
    *,
    ticker: str = "AAPL",
    quote_type: str = "EQUITY",
    next_earnings_date: date | None = date(2026, 8, 1),
    dividend_yield: float | None = 0.01,
    annual_dividend_rate: float | None = None,
    ex_dividend_date: date | None = date(2026, 6, 1),
) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        ticker=ticker,
        quote_type=quote_type,
        long_name=f"{ticker} Test",
        market_cap=10_000_000_000,
        pe_ratio=20,
        dividend_yield=dividend_yield,
        annual_dividend_rate=annual_dividend_rate,
        ex_dividend_date=ex_dividend_date,
        quarterly_net_income=[1, 1, 1, 1, 1],
        annual_net_income=[1, 1, 1, 1, 1],
        next_earnings_date=next_earnings_date,
        recent_move_pct=10,
    )


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
