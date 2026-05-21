from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .alpaca import (
    AlpacaConfigError,
    AlpacaRequestError,
    AlpacaTradingClient,
    account_identity_reasons,
    parse_occ_option_symbol,
)


DEFAULT_OMS_DB_PATH = Path("workspace/oms/wheels_oms.sqlite")
OPEN_ORDER_STATUSES = {
    "PENDING_SUBMIT",
    "SUBMIT_ERROR",
    "DUPLICATE_AT_BROKER",
    "SUBMITTED",
    "PARTIAL",
}
TERMINAL_ORDER_STATUSES = {
    "FILLED",
    "CANCELED",
    "EXPIRED",
    "DONE_FOR_DAY",
    "REJECTED",
    "ERROR",
}
BROKER_TO_OMS_STATUS = {
    "accepted": "SUBMITTED",
    "new": "SUBMITTED",
    "pending_new": "SUBMITTED",
    "held": "SUBMITTED",
    "partially_filled": "PARTIAL",
    "filled": "FILLED",
    "canceled": "CANCELED",
    "cancelled": "CANCELED",
    "expired": "EXPIRED",
    "done_for_day": "DONE_FOR_DAY",
    "rejected": "REJECTED",
}
RECOVER_BY_CLIENT_ID_STATUSES = {
    "PENDING_SUBMIT",
    "SUBMIT_ERROR",
    "DUPLICATE_AT_BROKER",
}


@dataclass(frozen=True)
class BeginSubmitResult:
    inserted: bool
    order_id: int
    existing_status: str | None = None


class OrderLedger:
    def __init__(self, db_path: str | Path = DEFAULT_OMS_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> OrderLedger:
        oms_cfg = config.get("oms") or {}
        return cls(oms_cfg.get("db_path") or DEFAULT_OMS_DB_PATH)

    def close(self) -> None:
        self.conn.close()

    def begin_submit(
        self,
        *,
        client_order_id: str,
        order: dict[str, Any],
        broker_payload: dict[str, Any],
    ) -> BeginSubmitResult:
        now = _now_iso()
        try:
            cursor = self.conn.execute(
                """INSERT INTO orders
                   (client_order_id, shadow_order_id, proposal_id, ticker, strategy,
                    symbol, side, qty, limit_price, status, broker_payload_json,
                    source_order_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    client_order_id,
                    order.get("shadow_order_id"),
                    order.get("proposal_id"),
                    order.get("ticker"),
                    order.get("strategy"),
                    broker_payload.get("symbol"),
                    broker_payload.get("side"),
                    _number(broker_payload.get("qty")),
                    _number(broker_payload.get("limit_price")),
                    "PENDING_SUBMIT",
                    json.dumps(broker_payload, sort_keys=True),
                    json.dumps(order, default=str, sort_keys=True),
                    now,
                    now,
                ),
            )
            self.conn.commit()
            return BeginSubmitResult(inserted=True, order_id=int(cursor.lastrowid))
        except sqlite3.IntegrityError:
            row = self.get_order_by_client_order_id(client_order_id)
            if not row:
                raise
            return BeginSubmitResult(
                inserted=False,
                order_id=int(row["id"]),
                existing_status=str(row["status"]),
            )

    def update_after_submit(
        self,
        *,
        order_id: int,
        status: str,
        broker_order: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        broker_order = broker_order or {}
        now = _now_iso()
        self.conn.execute(
            """UPDATE orders
               SET status=?, alpaca_order_id=COALESCE(?, alpaca_order_id),
                   broker_status=?, broker_order_json=?, error_message=?,
                   submitted_at=COALESCE(submitted_at, ?), updated_at=?
               WHERE id=?""",
            (
                status,
                broker_order.get("id"),
                broker_order.get("status"),
                json.dumps(broker_order, default=str, sort_keys=True) if broker_order else None,
                error_message,
                now if status in {"SUBMITTED", "PARTIAL", "FILLED"} else None,
                now,
                order_id,
            ),
        )
        self.conn.commit()

    def update_from_broker_order(
        self,
        *,
        order_id: int,
        broker_order: dict[str, Any],
    ) -> str:
        broker_status = str(broker_order.get("status") or "").lower()
        status = BROKER_TO_OMS_STATUS.get(broker_status, "ERROR")
        error_message = None if status != "ERROR" else f"unexpected_broker_status:{broker_status or 'missing'}"
        fill_price = _number(broker_order.get("filled_avg_price"))
        filled_qty = _number(broker_order.get("filled_qty"))
        now = _now_iso()
        filled_at = broker_order.get("filled_at") if status == "FILLED" else None
        self.conn.execute(
            """UPDATE orders
               SET status=?, broker_status=?, broker_order_json=?, error_message=?,
                   fill_price=?, filled_qty=?, filled_at=COALESCE(?, filled_at),
                   updated_at=?
               WHERE id=?""",
            (
                status,
                broker_order.get("status"),
                json.dumps(broker_order, default=str, sort_keys=True),
                error_message,
                fill_price,
                filled_qty,
                filled_at,
                now,
                order_id,
            ),
        )
        if status == "FILLED":
            row = self.get_order(order_id)
            if row:
                self._upsert_position_from_order(row, broker_order)
        self.conn.commit()
        return status

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def get_order_by_client_order_id(self, client_order_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()

    def list_open_orders(self) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in OPEN_ORDER_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY id",
            tuple(sorted(OPEN_ORDER_STATUSES)),
        ).fetchall()
        return list(rows)

    def list_positions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM positions ORDER BY id").fetchall())

    def _upsert_position_from_order(
        self,
        order_row: sqlite3.Row,
        broker_order: dict[str, Any],
    ) -> None:
        symbol = str(order_row["symbol"] or "").upper()
        parsed = parse_occ_option_symbol(symbol) or {}
        qty = _number(broker_order.get("filled_qty")) or _number(order_row["qty"]) or 0.0
        entry_price = _number(broker_order.get("filled_avg_price")) or _number(order_row["limit_price"])
        assignment_cash = 0.0
        # Wheels Copilot currently supports standard US equity options only:
        # one contract controls 100 shares. Mini/non-standard multipliers are
        # intentionally unsupported until contract multiplier data is wired in.
        if parsed.get("option_type") == "put" and parsed.get("strike") is not None:
            assignment_cash = abs(qty) * float(parsed["strike"]) * 100.0
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO positions
               (client_order_id, alpaca_order_id, ticker, symbol, option_type,
                expiration, strike, qty, side, status, entry_price,
                assignment_cash_required, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(client_order_id) DO UPDATE SET
                 alpaca_order_id=excluded.alpaca_order_id,
                 qty=excluded.qty,
                 status=excluded.status,
                 entry_price=excluded.entry_price,
                 assignment_cash_required=excluded.assignment_cash_required,
                 updated_at=excluded.updated_at""",
            (
                order_row["client_order_id"],
                order_row["alpaca_order_id"],
                order_row["ticker"],
                symbol,
                parsed.get("option_type"),
                parsed.get("expiration").isoformat() if parsed.get("expiration") else None,
                parsed.get("strike"),
                qty,
                order_row["side"],
                "OPEN",
                entry_price,
                assignment_cash,
                now,
                now,
            ),
        )

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()


def oms_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("oms") or {}).get("enabled"))


def reconcile_orders(
    config: dict[str, Any],
    *,
    client: AlpacaTradingClient | None = None,
    ledger: OrderLedger | None = None,
) -> dict[str, Any]:
    owns_ledger = ledger is None
    ledger = ledger or OrderLedger.from_config(config)
    try:
        client = client or AlpacaTradingClient.from_config(config)
        account = client.fetch_account_snapshot()
        identity_reasons = account_identity_reasons(config, account)
        if identity_reasons:
            raise AlpacaConfigError(
                "Alpaca account identity mismatch: " + "; ".join(identity_reasons)
            )
        orders = ledger.list_open_orders()
        results = []
        stats: dict[str, int] = {}
        for row in orders:
            if not row["alpaca_order_id"]:
                if row["status"] in RECOVER_BY_CLIENT_ID_STATUSES:
                    try:
                        broker_order = client.fetch_order_by_client_order_id(
                            str(row["client_order_id"])
                        )
                        if broker_order.get("id"):
                            ledger.conn.execute(
                                "UPDATE orders SET alpaca_order_id=? WHERE id=?",
                                (broker_order.get("id"), row["id"]),
                            )
                            ledger.conn.commit()
                        new_status = ledger.update_from_broker_order(
                            order_id=int(row["id"]),
                            broker_order=broker_order,
                        )
                        results.append(
                            {
                                "client_order_id": row["client_order_id"],
                                "alpaca_order_id": broker_order.get("id"),
                                "previous_status": row["status"],
                                "status": new_status,
                                "broker_status": broker_order.get("status"),
                                "recovered_by": "client_order_id",
                            }
                        )
                        stats[new_status] = stats.get(new_status, 0) + 1
                    except (AlpacaConfigError, AlpacaRequestError) as exc:
                        result = {
                            "client_order_id": row["client_order_id"],
                            "status": row["status"],
                            "action": "missing_alpaca_order_id_unresolved",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        results.append(result)
                        stats[result["action"]] = stats.get(result["action"], 0) + 1
                    continue
                result = {
                    "client_order_id": row["client_order_id"],
                    "status": row["status"],
                    "action": "skipped_missing_alpaca_order_id",
                }
                results.append(result)
                stats[result["action"]] = stats.get(result["action"], 0) + 1
                continue
            try:
                broker_order = client.fetch_order(str(row["alpaca_order_id"]))
                new_status = ledger.update_from_broker_order(
                    order_id=int(row["id"]),
                    broker_order=broker_order,
                )
                results.append(
                    {
                        "client_order_id": row["client_order_id"],
                        "alpaca_order_id": row["alpaca_order_id"],
                        "previous_status": row["status"],
                        "status": new_status,
                        "broker_status": broker_order.get("status"),
                    }
                )
                stats[new_status] = stats.get(new_status, 0) + 1
            except (AlpacaConfigError, AlpacaRequestError) as exc:
                result = {
                    "client_order_id": row["client_order_id"],
                    "alpaca_order_id": row["alpaca_order_id"],
                    "status": row["status"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
                results.append(result)
                stats["ERROR"] = stats.get("ERROR", 0) + 1
        return {
            "generated_at": _now_iso(),
            "order_count": len(orders),
            "summary": dict(sorted(stats.items())),
            "orders": results,
        }
    finally:
        if owns_ledger and ledger:
            ledger.close()


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
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

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_alpaca_order_id ON orders(alpaca_order_id);

CREATE TABLE IF NOT EXISTS positions (
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

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);
"""
