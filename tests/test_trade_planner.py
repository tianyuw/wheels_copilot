from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wheels_copilot.scan import write_scan_outputs
from wheels_copilot.trade_planner import build_shadow_orders, build_trade_proposals


class TradePlannerTests(unittest.TestCase):
    def test_auto_trade_candidate_creates_proposal_and_shadow_order(self):
        scan = _scan([_row("AAPL")])
        proposals = build_trade_proposals(scan, _config())
        orders = build_shadow_orders(proposals, _config())

        self.assertEqual(proposals["summary"], {"PROPOSED": 1})
        proposal = proposals["proposals"][0]
        self.assertEqual(proposal["decision"], "PROPOSED")
        self.assertEqual(proposal["assignment_cash_required"], 9000)
        self.assertEqual(proposal["estimated_premium_credit"], 110)
        self.assertFalse(proposal["requires_manual_review"])

        self.assertEqual(orders["order_count"], 1)
        payload = orders["orders"][0]["payload"]
        self.assertEqual(payload["symbol"], "AAPL260529P00090000")
        self.assertLessEqual(len(payload["client_order_id"]), 48)
        self.assertEqual(payload["side"], "sell")
        self.assertEqual(payload["type"], "limit")
        self.assertEqual(payload["time_in_force"], "day")
        self.assertEqual(payload["limit_price"], "1.10")
        self.assertEqual(payload["position_intent"], "sell_to_open")

    def test_watch_candidate_does_not_create_shadow_order(self):
        row = _row(
            "AAPL",
            status="WATCH",
            manual_review_required=True,
            auto_trade=True,
        )
        proposals = build_trade_proposals(_scan([row]), _config())
        orders = build_shadow_orders(proposals, _config())

        self.assertEqual(proposals["summary"], {"WATCH": 1})
        self.assertEqual(proposals["proposals"][0]["decision"], "WATCH")
        self.assertTrue(proposals["proposals"][0]["requires_manual_review"])
        self.assertEqual(orders["order_count"], 0)

    def test_missing_price_downgrades_candidate_to_watch(self):
        row = _row("AAPL")
        option = row["candidate"]["option"]
        option["executable_mid"] = None
        option["mid"] = None
        option["last"] = None

        proposals = build_trade_proposals(_scan([row]), _config())
        orders = build_shadow_orders(proposals, _config())

        self.assertEqual(proposals["proposals"][0]["decision"], "WATCH")
        self.assertIn("missing_option_price", proposals["proposals"][0]["decision_reasons"])
        self.assertEqual(orders["order_count"], 0)

    def test_defensive_gate_check_blocks_inconsistent_auto_trade_row(self):
        row = _row("AAPL")
        row["portfolio_gate"] = {
            "status": "REJECT",
            "reasons": ["duplicate_open_short_put_order"],
            "warnings": [],
        }

        proposals = build_trade_proposals(_scan([row]), _config())
        orders = build_shadow_orders(proposals, _config())

        self.assertEqual(proposals["proposals"][0]["decision"], "REJECTED_BY_GATE")
        self.assertIn(
            "portfolio_gate_reject", proposals["proposals"][0]["decision_reasons"]
        )
        self.assertEqual(orders["order_count"], 0)

    def test_sequential_allocation_rejects_later_candidate(self):
        config = _config(
            account={"starting_equity": 100000},
            risk={
                "max_assignment_cash_pct": 0.20,
                "min_cash_buffer_pct": 0.0,
                "no_margin_assignment": True,
            },
        )
        scan = _scan(
            [
                _row("AAPL", strike=100, symbol="AAPL260529P00100000"),
                _row("MSFT", strike=100, symbol="MSFT260529P00100000"),
                _row("UPS", strike=100, symbol="UPS260529P00100000"),
            ]
        )
        proposals = build_trade_proposals(scan, config)
        orders = build_shadow_orders(proposals, config)

        decisions = [proposal["decision"] for proposal in proposals["proposals"]]
        self.assertEqual(decisions, ["PROPOSED", "PROPOSED", "REJECTED_BY_ALLOCATION"])
        self.assertEqual(proposals["allocation"]["final_reserved_assignment_cash"], 20000)
        self.assertEqual(orders["order_count"], 2)

    def test_allocation_sorts_by_support_score_before_assigning_cash(self):
        config = _config(
            account={"starting_equity": 100000},
            risk={
                "max_assignment_cash_pct": 0.10,
                "min_cash_buffer_pct": 0.0,
                "no_margin_assignment": True,
            },
        )
        low_quality = _row("AAA", strike=100, symbol="AAA260529P00100000")
        low_quality["support_score"] = 70.0
        low_quality["selected_support"]["score"] = 70.0
        high_quality = _row("ZZZ", strike=100, symbol="ZZZ260529P00100000")
        high_quality["support_score"] = 95.0
        high_quality["selected_support"]["score"] = 95.0

        proposals = build_trade_proposals(_scan([low_quality, high_quality]), config)

        self.assertEqual(proposals["proposals"][0]["ticker"], "ZZZ")
        self.assertEqual(proposals["proposals"][0]["decision"], "PROPOSED")
        self.assertEqual(proposals["proposals"][1]["ticker"], "AAA")
        self.assertEqual(proposals["proposals"][1]["decision"], "REJECTED_BY_ALLOCATION")

    def test_allocation_exactly_at_cap_passes(self):
        config = _config(
            account={"starting_equity": 100000},
            risk={
                "max_assignment_cash_pct": 0.10,
                "min_cash_buffer_pct": 0.0,
                "no_margin_assignment": True,
            },
        )

        proposals = build_trade_proposals(
            _scan([_row("AAPL", strike=100, symbol="AAPL260529P00100000")]),
            config,
        )

        self.assertEqual(proposals["proposals"][0]["decision"], "PROPOSED")
        self.assertEqual(proposals["allocation"]["final_reserved_assignment_cash"], 10000)

    def test_min_cash_buffer_violation_rejects_allocation(self):
        config = _config(
            account={"starting_equity": 100000},
            risk={
                "max_assignment_cash_pct": 1.0,
                "min_cash_buffer_pct": 0.95,
                "no_margin_assignment": True,
            },
        )

        proposals = build_trade_proposals(
            _scan([_row("AAPL", strike=100, symbol="AAPL260529P00100000")]),
            config,
        )

        self.assertEqual(
            proposals["proposals"][0]["decision"], "REJECTED_BY_ALLOCATION"
        )
        self.assertTrue(
            any(
                reason.startswith("cash_buffer_after_assignment_below_min")
                for reason in proposals["proposals"][0]["decision_reasons"]
            )
        )

    def test_empty_scan_returns_empty_artifacts(self):
        proposals = build_trade_proposals(_scan([]), _config())
        orders = build_shadow_orders(proposals, _config())

        self.assertEqual(proposals["proposal_count"], 0)
        self.assertEqual(proposals["summary"], {})
        self.assertEqual(orders["order_count"], 0)

    def test_scan_output_writes_trade_proposals_and_shadow_orders(self):
        scan = _scan([_row("AAPL")])
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_scan_outputs(scan, Path(tmp), config=_config())

            self.assertTrue(paths["trade_proposals"].exists())
            self.assertTrue(paths["shadow_orders"].exists())
            self.assertTrue(paths["validated_shadow_orders"].exists())
            proposals = json.loads(paths["trade_proposals"].read_text())
            orders = json.loads(paths["shadow_orders"].read_text())
            validated = json.loads(paths["validated_shadow_orders"].read_text())
            self.assertEqual(proposals["summary"], {"PROPOSED": 1})
            self.assertEqual(orders["order_count"], 1)
            self.assertEqual(validated["order_count"], 1)
            self.assertEqual(validated["summary"], {"BLOCKED": 1})


def _config(
    *,
    account: dict | None = None,
    risk: dict | None = None,
) -> dict:
    return {
        "account": account or {"starting_equity": 500000},
        "risk": risk
        or {
            "max_assignment_cash_pct": 0.80,
            "min_cash_buffer_pct": 0.15,
            "no_margin_assignment": True,
        },
        "trade_planner": {
            "default_contract_quantity": 1,
            "shadow_order": {
                "broker": "alpaca",
                "type": "limit",
                "time_in_force": "day",
                "position_intent": "sell_to_open",
            },
        },
    }


def _scan(rows: list[dict]) -> dict:
    return {
        "scan_date": "2026-05-20",
        "generated_at": "2026-05-20T10:00:00",
        "period": "1y",
        "ticker_count": len(rows),
        "portfolio": None,
        "summary": {},
        "results": rows,
    }


def _row(
    ticker: str,
    *,
    status: str = "AUTO_TRADE",
    manual_review_required: bool = False,
    auto_trade: bool = True,
    strike: float = 90,
    symbol: str = "AAPL260529P00090000",
) -> dict:
    return {
        "status": status,
        "ticker": ticker,
        "current_price": 100.0,
        "support_score": 88.0,
        "selected_support": {
            "method": "pivot_cluster",
            "bottom": 90.0,
            "top": 92.0,
            "score": 88.0,
        },
        "fundamental_gate": {"status": "PASS", "reasons": ["ok"], "warnings": []},
        "earnings_gate": {"status": "PASS", "reasons": ["ok"], "warnings": []},
        "portfolio_gate": {"status": "PASS", "reasons": ["ok"], "warnings": []},
        "manual_review_required": manual_review_required,
        "candidate": {
            "option": {
                "symbol": symbol,
                "expiration": "2026-05-29",
                "strike": strike,
                "dte": 9,
                "bid": 1.0,
                "ask": 1.2,
                "last": 1.0,
                "mid": 1.1,
                "executable_mid": 1.1,
                "open_interest": 500,
                "volume": 50,
                "spread_pct_of_mid": 0.18,
            },
            "delta": -0.2,
            "delta_bucket": "strong_support",
            "auto_trade": auto_trade,
            "weekly_return_on_strike_pct": 0.95,
            "assignment_cash_required": strike * 100,
            "reasons": ["support score 88.0"],
            "diagnostics": {"support_top": 92.0},
        },
        "status_reason": "sample",
        "rejection_summary": {},
        "error": None,
    }


if __name__ == "__main__":
    unittest.main()
