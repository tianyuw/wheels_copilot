from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from wheels_copilot.alpaca import AlpacaRequestError
from wheels_copilot.execution import execute_validated_shadow_orders
from wheels_copilot.models import BrokerAccountSnapshot, BrokerOrder, BrokerPosition, PortfolioSnapshot


class ExecutionTests(unittest.TestCase):
    def test_submit_ready_order_is_submitted_to_alpaca_paper(self):
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"SUBMITTED": 1})
        self.assertEqual(result["submitted_count"], 1)
        submitted = client.submitted_payloads[0]
        self.assertEqual(submitted["symbol"], "AAPL260529P00090000")
        self.assertEqual(submitted["side"], "sell")
        self.assertEqual(submitted["type"], "limit")
        self.assertEqual(submitted["position_intent"], "sell_to_open")
        self.assertEqual(submitted["client_order_id"], "wheel-20260520-AAPL-90P")
        self.assertNotIn("dry_run_only", submitted)

    def test_non_paper_config_blocks_submission(self):
        cfg = _config()
        cfg["mode"] = "live"
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            cfg,
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn("config_mode_not_paper", result["orders"][0]["blocking_reasons"])
        self.assertEqual(client.submitted_payloads, [])

    def test_market_closed_blocks_submission(self):
        client = _FakeExecutionClient(clock={"is_open": False, "timestamp": "2026-05-20T20:00:00Z", "next_open": "2026-05-21T13:30:00Z", "next_close": "2026-05-21T20:00:00Z"})

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn("market_closed", result["orders"][0]["blocking_reasons"])
        self.assertEqual(client.submitted_payloads, [])

    def test_near_close_blocks_submission(self):
        client = _FakeExecutionClient(
            clock={
                "is_open": True,
                "timestamp": "2026-05-20T19:45:00Z",
                "next_open": "2026-05-21T13:30:00Z",
                "next_close": "2026-05-20T20:00:00Z",
            }
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn("near_close_gate:15.0m<30m", result["orders"][0]["blocking_reasons"])

    def test_portfolio_gate_warning_or_reject_blocks_autonomous_submit(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(
                positions=[
                    BrokerPosition(
                        symbol="AAPL260522P00100000",
                        qty=-1,
                        underlying_symbol="AAPL",
                        option_type="put",
                        expiration=date(2026, 5, 22),
                        strike=100,
                    )
                ]
            )
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
        )

        reasons = result["orders"][0]["blocking_reasons"]
        self.assertIn("portfolio_gate_reject", reasons)
        self.assertIn("portfolio:existing_short_put_position", reasons)
        self.assertEqual(client.submitted_payloads, [])

    def test_account_identity_mismatch_blocks_submission(self):
        cfg = _config()
        cfg["alpaca"]["expected_account_id"] = "expected-account"
        client = _FakeExecutionClient(
            portfolio=_portfolio(
                account=BrokerAccountSnapshot(
                    status="ACTIVE",
                    equity=500000,
                    cash=500000,
                    buying_power=500000,
                    account_id="other-account",
                )
            )
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            cfg,
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "account_identity:account_id_mismatch:actual=other-account",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.fetch_account_snapshot_count, 1)
        self.assertEqual(client.fetch_clock_count, 0)
        self.assertEqual(client.fetch_portfolio_snapshot_count, 0)
        self.assertEqual(client.submitted_payloads, [])

    def test_missing_account_identity_guard_config_blocks_submission(self):
        cfg = _config()
        cfg["alpaca"].pop("expected_account_id")
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            cfg,
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "account_identity:missing_account_identity_guard_config",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.fetch_account_snapshot_count, 1)
        self.assertEqual(client.fetch_clock_count, 0)
        self.assertEqual(client.fetch_portfolio_snapshot_count, 0)
        self.assertEqual(client.submitted_payloads, [])

    def test_previous_execution_result_blocks_duplicate_client_order_id(self):
        client = _FakeExecutionClient()
        previous = {
            "orders": [
                {
                    "status": "SUBMITTED",
                    "client_order_id": "wheel-20260520-AAPL-90P",
                }
            ]
        }

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
            previous_execution_results=previous,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "duplicate_client_order_id_previous_execution",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_stale_validation_artifact_blocks_submission(self):
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders([_order()], generated_at="2026-05-20T17:00:00+00:00"),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertTrue(
            any(
                reason.startswith("stale_validation_artifact")
                for reason in result["orders"][0]["blocking_reasons"]
            )
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_in_run_assignment_cash_blocks_later_order_before_broker_propagates(self):
        cfg = _config()
        cfg["risk"]["max_assignment_cash_pct"] = 0.03
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders(
                [
                    _order(),
                    _order(
                        ticker="MSFT",
                        symbol="MSFT260529P00070000",
                        client_order_id="wheel-20260520-MSFT-70P",
                    ),
                ]
            ),
            cfg,
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1, "SUBMITTED": 1})
        self.assertEqual(len(client.submitted_payloads), 1)
        reasons = result["orders"][1]["blocking_reasons"]
        self.assertIn("portfolio_gate_reject", reasons)
        self.assertTrue(
            any(reason.startswith("portfolio:max_assignment_cash_exceeded") for reason in reasons)
        )

    def test_duplicate_client_order_id_at_broker_is_not_generic_error(self):
        client = _FakeExecutionClient(
            submit_error=AlpacaRequestError(
                'Alpaca POST /v2/orders failed with HTTP 422 body={"message":"client_order_id already exists"}'
            )
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"DUPLICATE_AT_BROKER": 1})
        self.assertEqual(result["orders"][0]["status"], "DUPLICATE_AT_BROKER")

    def test_broker_submit_network_failure_is_error(self):
        client = _FakeExecutionClient(
            submit_error=AlpacaRequestError("Alpaca POST /v2/orders failed due to network error")
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"SUBMIT_ERROR": 1})
        self.assertIn("network error", result["orders"][0]["error"])

    def test_unexpected_broker_status_is_error(self):
        client = _FakeExecutionClient(submit_status="suspended")

        result = execute_validated_shadow_orders(
            _validated_orders([_order()]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"ERROR": 1})
        self.assertEqual(
            result["orders"][0]["error"],
            "unexpected_broker_order_status:suspended",
        )

    def test_unexpected_payload_keys_fail_closed(self):
        order = _order()
        order["validated_payload"]["extended_hours"] = "true"
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders([order]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "unexpected_payload_keys:extended_hours",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_max_orders_per_run_blocks_later_orders(self):
        cfg = _config()
        cfg["execution"]["max_orders_per_run"] = 1
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders(
                [
                    _order(),
                    _order(
                        ticker="MSFT",
                        symbol="MSFT260529P00300000",
                        client_order_id="wheel-20260520-MSFT-300P",
                    ),
                ]
            ),
            cfg,
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1, "SUBMITTED": 1})
        self.assertEqual(len(client.submitted_payloads), 1)
        self.assertIn("max_orders_per_run_reached", result["orders"][1]["blocking_reasons"])

    def test_covered_call_with_unchecked_risks_blocks_submission(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(positions=[BrokerPosition(symbol="AAPL", qty=100)])
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order()]),
            _config(),
            client=client,
        )

        reasons = result["orders"][0]["blocking_reasons"]
        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn("portfolio_gate_reject", reasons)
        self.assertTrue(
            any(
                reason.startswith("portfolio:covered_call_unchecked_risks_present")
                for reason in reasons
            )
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_requires_broker_verified_long_stock_coverage(self):
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order(unchecked_risks=[])]),
            _config(),
            client=client,
        )

        reasons = result["orders"][0]["blocking_reasons"]
        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "portfolio:covered_call_insufficient_long_shares:0<100",
            reasons,
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_submit_ready_order_is_submitted_when_risks_clear(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(positions=[BrokerPosition(symbol="AAPL", qty=100)])
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order(unchecked_risks=[])]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"SUBMITTED": 1})
        submitted = client.submitted_payloads[0]
        self.assertEqual(submitted["symbol"], "AAPL260529C00090000")
        self.assertEqual(submitted["side"], "sell")
        self.assertEqual(submitted["position_intent"], "sell_to_open")

    def test_covered_call_below_cost_basis_blocks_submission(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(positions=[BrokerPosition(symbol="AAPL", qty=100)])
        )

        result = execute_validated_shadow_orders(
            _validated_orders(
                [_covered_call_order(symbol="AAPL260529C00085000", unchecked_risks=[])]
            ),
            _config(),
            client=client,
        )

        reasons = result["orders"][0]["blocking_reasons"]
        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "portfolio:covered_call_strike_below_adjusted_cost_basis:85.00<88.80",
            reasons,
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_existing_short_call_consumes_share_coverage(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(
                positions=[
                    BrokerPosition(symbol="AAPL", qty=100),
                    BrokerPosition(
                        symbol="AAPL260529C00090000",
                        qty=-1,
                        underlying_symbol="AAPL",
                        option_type="call",
                        expiration=date(2026, 5, 29),
                        strike=90,
                    ),
                ]
            )
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order(unchecked_risks=[])]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "portfolio:covered_call_insufficient_long_shares:0<100",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_open_sell_call_order_consumes_share_coverage(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(
                positions=[BrokerPosition(symbol="AAPL", qty=100)],
                open_orders=[
                    BrokerOrder(
                        id="open-call",
                        symbol="AAPL260529C00090000",
                        side="sell",
                        qty=1,
                        underlying_symbol="AAPL",
                        option_type="call",
                        expiration=date(2026, 5, 29),
                        strike=90,
                    )
                ],
            )
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order(unchecked_risks=[])]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "portfolio:covered_call_insufficient_long_shares:0<100",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_open_equity_sell_order_consumes_share_coverage(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(
                positions=[BrokerPosition(symbol="AAPL", qty=100)],
                open_orders=[
                    BrokerOrder(
                        id="open-stock-sale",
                        symbol="AAPL",
                        side="sell",
                        qty=100,
                        asset_class="us_equity",
                    )
                ],
            )
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order(unchecked_risks=[])]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "portfolio:covered_call_insufficient_long_shares:0<100",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_min_acceptable_strike_is_required_and_enforced(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(positions=[BrokerPosition(symbol="AAPL", qty=100)])
        )

        missing = execute_validated_shadow_orders(
            _validated_orders(
                [_covered_call_order(unchecked_risks=[], min_acceptable_strike=None)]
            ),
            _config(),
            client=client,
        )
        self.assertIn(
            "portfolio:covered_call_min_acceptable_strike_missing",
            missing["orders"][0]["blocking_reasons"],
        )

        below_floor = execute_validated_shadow_orders(
            _validated_orders(
                [_covered_call_order(unchecked_risks=[], min_acceptable_strike=95.0)]
            ),
            _config(),
            client=client,
        )
        self.assertIn(
            "portfolio:covered_call_strike_below_min_acceptable:90.00<95.00",
            below_floor["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_multi_contract_order_requires_matching_shares(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(positions=[BrokerPosition(symbol="AAPL", qty=200)])
        )

        result = execute_validated_shadow_orders(
            _validated_orders(
                [
                    _covered_call_order(
                        qty="2",
                        unchecked_risks=[],
                        available_shares_for_cc=200,
                    )
                ]
            ),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"SUBMITTED": 1})
        self.assertEqual(client.submitted_payloads[0]["qty"], "2")

    def test_covered_call_inactive_account_blocks_submission(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(
                account=BrokerAccountSnapshot(
                    status="INACTIVE",
                    equity=500000,
                    cash=500000,
                    buying_power=500000,
                    account_id="acct-test",
                ),
                positions=[BrokerPosition(symbol="AAPL", qty=100)],
            )
        )

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order(unchecked_risks=[])]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn("portfolio:account_status_INACTIVE", result["orders"][0]["blocking_reasons"])
        self.assertEqual(client.submitted_payloads, [])

    def test_covered_call_previous_execution_blocks_duplicate_client_order_id(self):
        client = _FakeExecutionClient(
            portfolio=_portfolio(positions=[BrokerPosition(symbol="AAPL", qty=100)])
        )
        previous = {
            "orders": [
                {
                    "status": "SUBMITTED",
                    "client_order_id": "whcc-260520-AAPL-260529-9000000-test",
                }
            ]
        }

        result = execute_validated_shadow_orders(
            _validated_orders([_covered_call_order(unchecked_risks=[])]),
            _config(),
            client=client,
            previous_execution_results=previous,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn(
            "duplicate_client_order_id_previous_execution",
            result["orders"][0]["blocking_reasons"],
        )
        self.assertEqual(client.submitted_payloads, [])

    def test_missing_strategy_fails_closed_in_executor(self):
        order = _order(strategy="")
        client = _FakeExecutionClient()

        result = execute_validated_shadow_orders(
            _validated_orders([order]),
            _config(),
            client=client,
        )

        self.assertEqual(result["summary"], {"BLOCKED": 1})
        self.assertIn("unsupported_strategy:missing", result["orders"][0]["blocking_reasons"])
        self.assertEqual(client.submitted_payloads, [])


def _config() -> dict:
    return {
        "mode": "paper",
        "broker": "alpaca",
        "account": {"account_type": "paper", "live_trading_enabled": False},
        "alpaca": {
            "paper_base_url": "https://paper-api.alpaca.markets",
            "expected_account_id": "acct-test",
        },
        "execution": {"max_orders_per_run": 3, "no_open_minutes_before_close": 30},
        "risk": {
            "max_assignment_cash_pct": 0.80,
            "min_cash_buffer_pct": 0.15,
            "max_single_ticker_assignment_pct": 0.15,
            "max_single_ticker_assignment_dollars": 75000,
            "no_margin_assignment": True,
        },
        "portfolio": {"max_active_tickers": 5},
    }


def _validated_orders(orders: list[dict], generated_at: str | None = None) -> dict:
    return {
        "scan_date": "2026-05-20",
        "generated_at": generated_at or _fresh_timestamp(),
        "dry_run_only": True,
        "broker": "alpaca",
        "orders": orders,
    }


def _order(
    *,
    ticker: str = "AAPL",
    symbol: str = "AAPL260529P00090000",
    client_order_id: str = "wheel-20260520-AAPL-90P",
    qty: str = "1",
    submit_ready: bool = True,
    strategy: str = "cash_secured_put",
    extra: dict | None = None,
) -> dict:
    order = {
        "shadow_order_id": client_order_id,
        "proposal_id": client_order_id,
        "validated_at": "2026-05-20T17:00:00+00:00",
        "dry_run_only": True,
        "submit_ready": submit_ready,
        "blocking_reasons": [],
        "ticker": ticker,
        "strategy": strategy,
        "latest_quote": {"bid": 1.0, "ask": 1.2, "mid": 1.1},
        "validated_limit_price": 1.1,
        "validated_payload": {
            "symbol": symbol,
            "qty": qty,
            "side": "sell",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": "1.10",
            "position_intent": "sell_to_open",
            "client_order_id": client_order_id,
        },
    }
    if extra:
        order.update(extra)
    return order


def _covered_call_order(
    *,
    symbol: str = "AAPL260529C00090000",
    qty: str = "1",
    unchecked_risks: list[str] | None = None,
    available_shares_for_cc: int = 100,
    min_acceptable_strike: float | None = 88.8,
) -> dict:
    return _order(
        ticker="AAPL",
        symbol=symbol,
        client_order_id="whcc-260520-AAPL-260529-9000000-test",
        qty=qty,
        strategy="covered_call",
        extra={
            "adjusted_cost_basis": 88.8,
            "available_shares_for_cc": available_shares_for_cc,
            "min_acceptable_strike": min_acceptable_strike,
            "unchecked_risks": (
                ["earnings_not_checked", "ex_dividend_not_checked"]
                if unchecked_risks is None
                else unchecked_risks
            ),
        },
    )


def _portfolio(
    *,
    account: BrokerAccountSnapshot | None = None,
    positions: list[BrokerPosition] | None = None,
    open_orders: list[BrokerOrder] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account=account
        or BrokerAccountSnapshot(
            status="ACTIVE",
            equity=500000,
            cash=500000,
            buying_power=500000,
            account_id="acct-test",
        ),
        positions=positions or [],
        open_orders=open_orders or [],
        source="test",
    )


class _FakeExecutionClient:
    def __init__(
        self,
        *,
        clock: dict | None = None,
        portfolio: PortfolioSnapshot | None = None,
        submit_error: Exception | None = None,
        submit_status: str = "accepted",
    ):
        self.clock = clock or {
            "is_open": True,
            "timestamp": "2026-05-20T17:00:00Z",
            "next_open": "2026-05-21T13:30:00Z",
            "next_close": "2026-05-20T20:00:00Z",
        }
        self.portfolio = portfolio or _portfolio()
        self.submit_error = submit_error
        self.submit_status = submit_status
        self.submitted_payloads = []
        self.fetch_account_snapshot_count = 0
        self.fetch_clock_count = 0
        self.fetch_portfolio_snapshot_count = 0

    def fetch_clock(self):
        self.fetch_clock_count += 1
        return self.clock

    def fetch_account_snapshot(self):
        self.fetch_account_snapshot_count += 1
        return self.portfolio.account

    def fetch_portfolio_snapshot(self):
        self.fetch_portfolio_snapshot_count += 1
        return self.portfolio

    def submit_order(self, payload):
        if self.submit_error:
            raise self.submit_error
        self.submitted_payloads.append(dict(payload))
        return {
            "id": f"alpaca-{len(self.submitted_payloads)}",
            "client_order_id": payload.get("client_order_id"),
            "status": self.submit_status,
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "qty": payload.get("qty"),
            "limit_price": payload.get("limit_price"),
            "submitted_at": "2026-05-20T17:00:01Z",
        }


def _fresh_timestamp() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


if __name__ == "__main__":
    unittest.main()
