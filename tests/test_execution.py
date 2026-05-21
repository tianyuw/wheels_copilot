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


def _config() -> dict:
    return {
        "mode": "paper",
        "broker": "alpaca",
        "account": {"account_type": "paper", "live_trading_enabled": False},
        "alpaca": {"paper_base_url": "https://paper-api.alpaca.markets"},
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
    submit_ready: bool = True,
) -> dict:
    return {
        "shadow_order_id": client_order_id,
        "proposal_id": client_order_id,
        "validated_at": "2026-05-20T17:00:00+00:00",
        "dry_run_only": True,
        "submit_ready": submit_ready,
        "blocking_reasons": [],
        "ticker": ticker,
        "strategy": "cash_secured_put",
        "latest_quote": {"bid": 1.0, "ask": 1.2, "mid": 1.1},
        "validated_limit_price": 1.1,
        "validated_payload": {
            "symbol": symbol,
            "qty": "1",
            "side": "sell",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": "1.10",
            "position_intent": "sell_to_open",
            "client_order_id": client_order_id,
        },
    }


def _portfolio(
    *,
    positions: list[BrokerPosition] | None = None,
    open_orders: list[BrokerOrder] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account=BrokerAccountSnapshot(
            status="ACTIVE",
            equity=500000,
            cash=500000,
            buying_power=500000,
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

    def fetch_clock(self):
        return self.clock

    def fetch_portfolio_snapshot(self):
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
