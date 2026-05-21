from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from wheels_copilot.execution import execute_validated_shadow_orders
from wheels_copilot.models import BrokerAccountSnapshot, BrokerPosition, PortfolioSnapshot
from wheels_copilot.alpaca import AlpacaConfigError, AlpacaRequestError
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

    def test_reconcile_filled_covered_call_records_open_position_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(
                fetch_order_status="filled",
                positions=[_stock("AAPL", 100, cost_basis=9000)],
            )
            execute_validated_shadow_orders(
                _validated_orders([_covered_call_order()]),
                cfg,
                client=client,
            )
            client.positions = [
                _stock("AAPL", 100, cost_basis=9000),
                _short_option("AAPL260529C00095000"),
            ]

            result = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))

            self.assertEqual(result["summary"], {"FILLED": 1})
            self.assertEqual(result["position_summary"], {"broker_position_open": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                positions = ledger.list_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["option_type"], "call")
                self.assertEqual(positions[0]["status"], "OPEN")
                self.assertEqual(positions[0]["assignment_cash_required"], 0)
                self.assertEqual(positions[0]["underlying_qty_at_open"], 100)
            finally:
                ledger.close()

    def test_reconcile_expired_covered_call_returns_stock_to_assigned_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(
                fetch_order_status="filled",
                positions=[_stock("AAPL", 100, cost_basis=9000)],
            )
            execute_validated_shadow_orders(
                _validated_orders([_covered_call_order()]),
                cfg,
                client=client,
            )
            client.positions = [
                _stock("AAPL", 100, cost_basis=9000),
                _short_option("AAPL260529C00095000"),
            ]
            reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))
            client.positions = [_stock("AAPL", 100, cost_basis=9000)]

            result = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 30))

            self.assertEqual(result["summary"], {})
            self.assertEqual(result["position_summary"], {"marked_EXPIRED": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                position = ledger.list_positions()[0]
                self.assertEqual(position["status"], "EXPIRED")
                self.assertEqual(
                    position["terminal_reason"],
                    "short_call_absent_after_expiration_stock_retained",
                )
                self.assertIsNotNone(position["closed_at"])
            finally:
                ledger.close()

    def test_reconcile_covered_call_called_away_when_shares_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(
                fetch_order_status="filled",
                positions=[_stock("AAPL", 200, cost_basis=18000)],
            )
            execute_validated_shadow_orders(
                _validated_orders([_covered_call_order(share_quantity=200)]),
                cfg,
                client=client,
            )
            client.positions = [
                _stock("AAPL", 200, cost_basis=18000),
                _short_option("AAPL260529C00095000"),
            ]
            reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))
            client.positions = [_stock("AAPL", 100, cost_basis=9000)]

            result = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 25))

            self.assertEqual(result["position_summary"], {"marked_CALLED_AWAY": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                position = ledger.list_positions()[0]
                self.assertEqual(position["status"], "CALLED_AWAY")
                self.assertEqual(
                    position["terminal_reason"],
                    "short_call_absent_and_underlying_shares_reduced",
                )
            finally:
                ledger.close()

    def test_reconcile_short_put_assignment_marks_ledger_position_assigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="filled")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )
            client.positions = [_short_option("AAPL260529P00090000")]
            reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))
            client.positions = [_stock("AAPL", 100, cost_basis=9000)]

            result = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 30))

            self.assertEqual(result["position_summary"], {"marked_ASSIGNED": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                position = ledger.list_positions()[0]
                self.assertEqual(position["status"], "ASSIGNED")
                self.assertEqual(
                    position["terminal_reason"],
                    "short_put_absent_and_underlying_shares_detected",
                )
            finally:
                ledger.close()

    def test_reconcile_short_put_expiration_marks_ledger_position_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="filled")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )
            client.positions = [_short_option("AAPL260529P00090000")]
            reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))
            client.positions = []

            result = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 30))

            self.assertEqual(result["position_summary"], {"marked_EXPIRED": 1})
            ledger = OrderLedger.from_config(cfg)
            try:
                position = ledger.list_positions()[0]
                self.assertEqual(position["status"], "EXPIRED")
                self.assertEqual(
                    position["terminal_reason"],
                    "short_put_absent_after_expiration_without_assignment",
                )
            finally:
                ledger.close()

    def test_reconcile_terminal_position_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="filled")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )
            client.positions = [_short_option("AAPL260529P00090000")]
            reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))
            client.positions = []
            first = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 30))
            second = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 31))

            self.assertEqual(first["position_summary"], {"marked_EXPIRED": 1})
            self.assertEqual(second["position_count"], 0)
            self.assertEqual(second["position_summary"], {})

    def test_reconcile_ambiguous_multi_call_outcome_stays_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(
                fetch_order_status="filled",
                positions=[_stock("AAPL", 200, cost_basis=18000)],
            )
            execute_validated_shadow_orders(
                _validated_orders([_covered_call_order(share_quantity=200)]),
                cfg,
                client=client,
            )
            first_client_id = client.submitted_payloads[-1]["client_order_id"]
            second_order = _covered_call_order(
                share_quantity=200,
                client_order_id="wheel-20260520-AAPL-2026-06-05-100C",
                symbol="AAPL260605C00100000",
            )
            execute_validated_shadow_orders(
                _validated_orders([second_order]),
                cfg,
                client=client,
            )
            client.positions = [
                _stock("AAPL", 200, cost_basis=18000),
                _short_option("AAPL260529C00095000"),
                _short_option("AAPL260605C00100000"),
            ]
            reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))
            client.positions = [_stock("AAPL", 100, cost_basis=9000)]

            result = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 30))

            self.assertEqual(result["position_summary"], {"ambiguous_multi_call_outcome": 2})
            ledger = OrderLedger.from_config(cfg)
            try:
                statuses = {
                    row["client_order_id"]: row["status"]
                    for row in ledger.list_positions()
                }
                self.assertEqual(statuses[first_client_id], "OPEN")
                self.assertEqual(statuses["wheel-20260520-AAPL-2026-06-05-100C"], "OPEN")
            finally:
                ledger.close()

    def test_reconcile_portfolio_fetch_error_surfaces_top_level_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            client = _FakeOmsClient(fetch_order_status="filled")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                cfg,
                client=client,
            )
            client.portfolio_error = AlpacaRequestError("portfolio unavailable")

            result = reconcile_orders(cfg, client=client, as_of=date(2026, 5, 21))

            self.assertEqual(result["summary"], {"FILLED": 1})
            self.assertEqual(
                result["position_summary"],
                {"portfolio_snapshot_unavailable": 1},
            )
            self.assertEqual(result["errors"][0]["scope"], "position_reconciliation")

    def test_order_ledger_migrates_existing_position_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_order_id TEXT NOT NULL UNIQUE,
                        alpaca_order_id TEXT,
                        shadow_order_id TEXT,
                        proposal_id TEXT,
                        ticker TEXT,
                        strategy TEXT,
                        symbol TEXT,
                        side TEXT,
                        qty REAL,
                        limit_price REAL,
                        status TEXT NOT NULL,
                        broker_status TEXT,
                        broker_payload_json TEXT,
                        source_order_json TEXT,
                        broker_order_json TEXT,
                        error_message TEXT,
                        fill_price REAL,
                        filled_qty REAL,
                        submitted_at TEXT,
                        filled_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE positions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_order_id TEXT NOT NULL UNIQUE REFERENCES orders(client_order_id),
                        alpaca_order_id TEXT,
                        ticker TEXT,
                        symbol TEXT NOT NULL,
                        option_type TEXT,
                        expiration TEXT,
                        strike REAL,
                        qty REAL NOT NULL,
                        side TEXT,
                        status TEXT NOT NULL,
                        entry_price REAL,
                        assignment_cash_required REAL NOT NULL DEFAULT 0,
                        opened_at TEXT,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            ledger = OrderLedger(db_path)
            try:
                columns = {
                    row["name"]
                    for row in ledger.conn.execute("PRAGMA table_info(positions)")
                }
            finally:
                ledger.close()

            self.assertIn("underlying_qty_at_open", columns)
            self.assertIn("closed_at", columns)
            self.assertIn("terminal_reason", columns)
            self.assertIn("last_reconciled_at", columns)

    def test_reconcile_fails_closed_on_account_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp) / "oms.sqlite")
            cfg["alpaca"]["expected_account_id"] = "expected-account"
            client = _FakeOmsClient(account_id="other-account")
            execute_validated_shadow_orders(
                _validated_orders([_order()]),
                _config(Path(tmp) / "submit.sqlite"),
                client=client,
            )

            with self.assertRaises(AlpacaConfigError):
                reconcile_orders(cfg, client=client)


def _config(db_path: Path) -> dict:
    return {
        "mode": "paper",
        "broker": "alpaca",
        "account": {"account_type": "paper", "live_trading_enabled": False},
        "alpaca": {
            "paper_base_url": "https://paper-api.alpaca.markets",
            "expected_account_id": "acct-test",
        },
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


def _covered_call_order(
    share_quantity: int = 100,
    *,
    client_order_id: str = "wheel-20260520-AAPL-2026-05-29-95C",
    symbol: str = "AAPL260529C00095000",
) -> dict:
    return {
        "shadow_order_id": client_order_id,
        "proposal_id": client_order_id,
        "validated_at": "2026-05-20T17:00:00+00:00",
        "dry_run_only": True,
        "submit_ready": True,
        "blocking_reasons": [],
        "ticker": "AAPL",
        "strategy": "covered_call",
        "share_quantity": share_quantity,
        "available_shares_for_cc": share_quantity,
        "adjusted_cost_basis": 90,
        "min_acceptable_strike": 90,
        "unchecked_risks": [],
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


def _stock(symbol: str, qty: float, *, cost_basis: float | None = None) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol,
        qty=qty,
        asset_class="us_equity",
        cost_basis=cost_basis,
    )


def _short_option(symbol: str) -> BrokerPosition:
    option_type = "call" if "C" in symbol[-9:] else "put"
    expiration = date(2000 + int(symbol[-15:-13]), int(symbol[-13:-11]), int(symbol[-11:-9]))
    strike = int(symbol[-8:]) / 1000.0
    return BrokerPosition(
        symbol=symbol,
        qty=-1,
        asset_class="us_option",
        side="short",
        underlying_symbol="AAPL",
        option_type=option_type,
        expiration=expiration,
        strike=strike,
    )


class _FakeOmsClient:
    def __init__(
        self,
        fetch_order_status: str = "filled",
        *,
        account_id: str | None = "acct-test",
        account_number: str | None = None,
        positions: list[BrokerPosition] | None = None,
    ):
        self.fetch_order_status = fetch_order_status
        self.account_id = account_id
        self.account_number = account_number
        self.positions = positions or []
        self.portfolio_error = None
        self.submitted_payloads = []

    def fetch_clock(self):
        return {
            "is_open": True,
            "timestamp": "2026-05-20T17:00:00Z",
            "next_open": "2026-05-21T13:30:00Z",
            "next_close": "2026-05-20T20:00:00Z",
        }

    def fetch_portfolio_snapshot(self):
        if self.portfolio_error:
            raise self.portfolio_error
        return PortfolioSnapshot(
            account=BrokerAccountSnapshot(
                status="ACTIVE",
                equity=500000,
                cash=500000,
                buying_power=500000,
                account_id=self.account_id,
                account_number=self.account_number,
            ),
            positions=list(self.positions),
            open_orders=[],
            source="test",
        )

    def fetch_account_snapshot(self):
        return BrokerAccountSnapshot(
            status="ACTIVE",
            equity=500000,
            cash=500000,
            buying_power=500000,
            account_id=self.account_id,
            account_number=self.account_number,
        )

    def submit_order(self, payload):
        order_id = f"alpaca-{len(self.submitted_payloads) + 1}"
        stored_payload = dict(payload)
        stored_payload["_alpaca_order_id"] = order_id
        self.submitted_payloads.append(stored_payload)
        return {
            "id": order_id,
            "client_order_id": payload.get("client_order_id"),
            "status": "accepted",
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "qty": payload.get("qty"),
            "limit_price": payload.get("limit_price"),
            "submitted_at": "2026-05-20T17:00:01Z",
        }

    def fetch_order(self, order_id: str):
        payload = next(
            (
                item
                for item in self.submitted_payloads
                if item.get("_alpaca_order_id") == order_id
            ),
            self.submitted_payloads[-1] if self.submitted_payloads else _order()["validated_payload"],
        )
        return {
            "id": order_id,
            "client_order_id": payload.get("client_order_id"),
            "status": self.fetch_order_status,
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "qty": payload.get("qty"),
            "filled_qty": "1" if self.fetch_order_status == "filled" else "0",
            "filled_avg_price": "1.05" if self.fetch_order_status == "filled" else None,
            "filled_at": "2026-05-20T17:00:15Z" if self.fetch_order_status == "filled" else None,
        }

    def fetch_order_by_client_order_id(self, client_order_id: str):
        for payload in self.submitted_payloads:
            if payload.get("client_order_id") == client_order_id:
                return self.fetch_order(payload["_alpaca_order_id"])
        return self.fetch_order("alpaca-1")


if __name__ == "__main__":
    unittest.main()
