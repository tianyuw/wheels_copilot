from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wheels_copilot.execution import execute_validated_shadow_orders
from wheels_copilot.models import BrokerAccountSnapshot, PortfolioSnapshot
from wheels_copilot.oms import OrderLedger, reconcile_orders


class OmsTests(unittest.TestCase):
    def test_execution_records_submitted_order_in_oms(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient()

            result = execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )

            self.assertEqual(result["summary"], {"SUBMITTED": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                row = ledger.get_order_by_client_order_id("wheel-20260520-AAPL-90P")
                self.assertIsNotNone(row)
                self.assertEqual(row["status"], "SUBMITTED")
                self.assertEqual(row["alpaca_order_id"], "alpaca-1")
                self.assertEqual(row["symbol"], "AAPL260529P00090000")
            finally:
                ledger.close()

    def test_oms_duplicate_blocks_second_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient()

            first = execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )
            second = execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )

            self.assertEqual(first["summary"], {"SUBMITTED": 1})
            self.assertEqual(second["summary"], {"DUPLICATE_IN_OMS": 1})
            self.assertEqual(len(client.submitted_payloads), 1)

    def test_reconcile_filled_order_updates_order_and_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="filled")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )

            result = reconcile_orders(cfg, client=client)

            self.assertEqual(result["summary"], {"FILLED": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                order = ledger.get_order_by_client_order_id("wheel-20260520-AAPL-90P")
                self.assertEqual(order["status"], "FILLED")
                self.assertEqual(order["fill_price"], 1.05)
                positions = ledger.list_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["status"], "OPEN")
                self.assertEqual(positions[0]["assignment_cash_required"], 9000)
            finally:
                ledger.close()

    def test_reconcile_recovers_pending_submit_without_alpaca_id_by_client_order_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            ledger = OrderLedger.from_config(cfg)
            try:
                ledger.begin_submit(
                    client_order_id="wheel-20260520-AAPL-90P",
                    order=_order(),
                    broker_payload=_order()["validated_payload"],
                )
            finally:
                ledger.close()

            result = reconcile_orders(cfg, client=_FakeOmsClient(fetch_order_status="filled"))

            self.assertEqual(result["summary"], {"FILLED": 1})
            self.assertEqual(result["orders"][0]["recovered_by"], "client_order_id")
            ledger = OrderLedger.from_config(cfg)
            try:
                order = ledger.get_order_by_client_order_id("wheel-20260520-AAPL-90P")
                self.assertEqual(order["status"], "FILLED")
                self.assertEqual(order["alpaca_order_id"], "alpaca-1")
                self.assertEqual(len(ledger.list_positions()), 1)
            finally:
                ledger.close()

    def test_reconcile_filled_order_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="filled")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )

            first = reconcile_orders(cfg, client=client)
            second = reconcile_orders(cfg, client=client)

            self.assertEqual(first["summary"], {"FILLED": 1})
            self.assertEqual(second["order_count"], 0)
            ledger = OrderLedger.from_config(cfg)
            try:
                positions = ledger.list_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["assignment_cash_required"], 9000)
            finally:
                ledger.close()

    def test_reconcile_canceled_order_does_not_create_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="canceled")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )

            result = reconcile_orders(cfg, client=client)

            self.assertEqual(result["summary"], {"CANCELED": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                order = ledger.get_order_by_client_order_id("wheel-20260520-AAPL-90P")
                self.assertEqual(order["status"], "CANCELED")
                self.assertEqual(ledger.list_positions(), [])
            finally:
                ledger.close()

    def test_reconcile_done_for_day_uses_distinct_terminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="done_for_day")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )

            result = reconcile_orders(cfg, client=client)

            self.assertEqual(result["summary"], {"DONE_FOR_DAY": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                order = ledger.get_order_by_client_order_id("wheel-20260520-AAPL-90P")
                self.assertEqual(order["status"], "DONE_FOR_DAY")
                self.assertEqual(ledger.list_positions(), [])
            finally:
                ledger.close()


def _config(db_path: Path) -> dict:
    return {
        "mode": "paper",
        "broker": "alpaca",
        "account": {"account_type": "paper", "live_trading_enabled": False},
        "alpaca": {"paper_base_url": "https://paper-api.alpaca.markets"},
        "execution": {
            "max_orders_per_run": 3,
            "max_validated_order_age_seconds": 120,
            "no_open_minutes_before_close": 30,
        },
        "oms": {"enabled": True, "db_path": str(db_path)},
        "risk": {
            "max_assignment_cash_pct": 0.80,
            "min_cash_buffer_pct": 0.15,
            "max_single_ticker_assignment_pct": 0.15,
            "max_single_ticker_assignment_dollars": 75000,
            "no_margin_assignment": True,
        },
        "portfolio": {"max_active_tickers": 5},
    }


def _validated_orders(orders: list[dict]) -> dict:
    return {
        "scan_date": "2026-05-20",
        "generated_at": (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat(),
        "dry_run_only": True,
        "broker": "alpaca",
        "orders": orders,
    }


def _order() -> dict:
    return {
        "shadow_order_id": "wheel-20260520-AAPL-90P",
        "proposal_id": "wheel-20260520-AAPL-90P",
        "validated_at": "2026-05-20T17:00:00+00:00",
        "dry_run_only": True,
        "submit_ready": True,
        "blocking_reasons": [],
        "ticker": "AAPL",
        "strategy": "cash_secured_put",
        "latest_quote": {"bid": 1.0, "ask": 1.2, "mid": 1.1},
        "validated_limit_price": 1.1,
        "validated_payload": {
            "symbol": "AAPL260529P00090000",
            "qty": "1",
            "side": "sell",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": "1.10",
            "position_intent": "sell_to_open",
            "client_order_id": "wheel-20260520-AAPL-90P",
        },
    }


class _FakeOmsClient:
    def __init__(self, fetch_order_status: str = "filled"):
        self.fetch_order_status = fetch_order_status
        self.submitted_payloads = []

    def fetch_clock(self):
        return {
            "is_open": True,
            "timestamp": "2026-05-20T17:00:00Z",
            "next_open": "2026-05-21T13:30:00Z",
            "next_close": "2026-05-20T20:00:00Z",
        }

    def fetch_portfolio_snapshot(self):
        return PortfolioSnapshot(
            account=BrokerAccountSnapshot(
                status="ACTIVE",
                equity=500000,
                cash=500000,
                buying_power=500000,
            ),
            positions=[],
            open_orders=[],
            source="test",
        )

    def submit_order(self, payload):
        self.submitted_payloads.append(dict(payload))
        return {
            "id": "alpaca-1",
            "client_order_id": payload.get("client_order_id"),
            "status": "accepted",
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "qty": payload.get("qty"),
            "limit_price": payload.get("limit_price"),
            "submitted_at": "2026-05-20T17:00:01Z",
        }

    def fetch_order(self, order_id: str):
        return {
            "id": order_id,
            "client_order_id": "wheel-20260520-AAPL-90P",
            "status": self.fetch_order_status,
            "symbol": "AAPL260529P00090000",
            "side": "sell",
            "qty": "1",
            "filled_qty": "1" if self.fetch_order_status == "filled" else "0",
            "filled_avg_price": "1.05" if self.fetch_order_status == "filled" else None,
            "filled_at": "2026-05-20T17:00:15Z" if self.fetch_order_status == "filled" else None,
        }

    def fetch_order_by_client_order_id(self, client_order_id: str):
        if client_order_id != "wheel-20260520-AAPL-90P":
            raise AssertionError(client_order_id)
        return self.fetch_order("alpaca-1")


if __name__ == "__main__":
    unittest.main()
