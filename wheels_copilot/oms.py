from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
OPEN_POSITION_STATUSES = {"OPEN"}
TERMINAL_POSITION_STATUSES = {"ASSIGNED", "CALLED_AWAY", "CLOSED", "EXPIRED"}
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

    def list_open_positions(self) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in OPEN_POSITION_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM positions WHERE status IN ({placeholders}) ORDER BY id",
            tuple(sorted(OPEN_POSITION_STATUSES)),
        ).fetchall()
        return list(rows)

    def reconcile_positions(
        self,
        portfolio,
        *,
        as_of: date | None = None,
    ) -> list[dict[str, Any]]:
        as_of = as_of or date.today()
        open_rows = self.list_open_positions()
        call_groups = _open_call_groups(open_rows)
        results = []
        for row in open_rows:
            decision = _position_reconciliation_decision(
                row,
                portfolio,
                as_of,
                call_group=call_groups.get(str(row["ticker"] or "").upper()) or [],
            )
            self._record_position_reconciliation(row, decision)
            results.append(
                {
                    "client_order_id": row["client_order_id"],
                    "symbol": row["symbol"],
                    "ticker": row["ticker"],
                    "option_type": row["option_type"],
                    "expiration": row["expiration"],
                    "previous_status": row["status"],
                    **decision,
                }
            )
        self.conn.commit()
        return results

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
        source_order = _json_dict(order_row["source_order_json"])
        underlying_qty_at_open = _number(source_order.get("share_quantity"))
        if underlying_qty_at_open is None:
            underlying_qty_at_open = _number(source_order.get("long_shares"))
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO positions
               (client_order_id, alpaca_order_id, ticker, symbol, option_type,
                expiration, strike, qty, side, status, entry_price,
                assignment_cash_required, underlying_qty_at_open, opened_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(client_order_id) DO UPDATE SET
                 alpaca_order_id=excluded.alpaca_order_id,
                 qty=excluded.qty,
                 status=excluded.status,
                 entry_price=excluded.entry_price,
                 assignment_cash_required=excluded.assignment_cash_required,
                 underlying_qty_at_open=COALESCE(
                    excluded.underlying_qty_at_open,
                    positions.underlying_qty_at_open
                 ),
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
                underlying_qty_at_open,
                now,
                now,
            ),
        )

    def _record_position_reconciliation(
        self,
        row: sqlite3.Row,
        decision: dict[str, Any],
    ) -> None:
        now = _now_iso()
        status = str(decision.get("status") or row["status"])
        closed_at = now if status in TERMINAL_POSITION_STATUSES and not row["closed_at"] else None
        self.conn.execute(
            """UPDATE positions
               SET status=?,
                   terminal_reason=COALESCE(?, terminal_reason),
                   closed_at=COALESCE(?, closed_at),
                   last_reconciled_at=?,
                   updated_at=?
               WHERE id=?""",
            (
                status,
                decision.get("terminal_reason"),
                closed_at,
                now,
                now,
                row["id"],
            ),
        )

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        position_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(positions)").fetchall()
        }
        migrations = {
            "underlying_qty_at_open": "underlying_qty_at_open REAL",
            "closed_at": "closed_at TEXT",
            "terminal_reason": "terminal_reason TEXT",
            "last_reconciled_at": "last_reconciled_at TEXT",
        }
        for column, ddl in migrations.items():
            if column not in position_columns:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {ddl}")


def oms_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("oms") or {}).get("enabled"))


def reconcile_orders(
    config: dict[str, Any],
    *,
    client: AlpacaTradingClient | None = None,
    ledger: OrderLedger | None = None,
    as_of: date | None = None,
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
        position_results = []
        position_stats: dict[str, int] = {}
        errors = []
        try:
            portfolio = client.fetch_portfolio_snapshot()
            position_results = ledger.reconcile_positions(portfolio, as_of=as_of)
            position_stats = _position_stats(position_results)
        except (AlpacaConfigError, AlpacaRequestError) as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            position_results = [
                {
                    "status": "ERROR",
                    "action": "portfolio_snapshot_unavailable",
                    "error": error_text,
                }
            ]
            errors.append(
                {
                    "scope": "position_reconciliation",
                    "error": error_text,
                }
            )
            position_stats = {"portfolio_snapshot_unavailable": 1}
        result = {
            "generated_at": _now_iso(),
            "order_count": len(orders),
            "summary": dict(sorted(stats.items())),
            "orders": results,
            "position_count": len(position_results),
            "position_summary": dict(sorted(position_stats.items())),
            "positions": position_results,
        }
        if errors:
            result["errors"] = errors
        return result
    finally:
        if owns_ledger and ledger:
            ledger.close()


def _position_reconciliation_decision(
    row: sqlite3.Row,
    portfolio,
    as_of: date,
    *,
    call_group: list[sqlite3.Row] | None = None,
) -> dict[str, Any]:
    symbol = str(row["symbol"] or "").upper()
    ticker = str(row["ticker"] or "").upper()
    option_type = str(row["option_type"] or "").lower()
    qty = abs(_number(row["qty"]) or 0.0)
    expiration = _parse_date(row["expiration"])
    broker_contracts = _broker_contracts(portfolio, symbol)
    if broker_contracts >= qty and qty > 0:
        return {
            "status": "OPEN",
            "action": "broker_position_open",
            "broker_contracts": broker_contracts,
            "terminal_reason": None,
        }
    if broker_contracts > 0:
        return {
            "status": "OPEN",
            "action": "broker_position_partially_open",
            "broker_contracts": broker_contracts,
            "terminal_reason": None,
            "warnings": [f"broker_contract_qty_below_ledger:{broker_contracts:g}<{qty:g}"],
        }

    long_shares = _long_shares(portfolio, ticker)
    expected_shares = qty * 100.0
    if option_type == "put":
        if long_shares + 1e-6 >= expected_shares and expected_shares > 0:
            return {
                "status": "ASSIGNED",
                "action": "position_status_updated",
                "broker_contracts": 0.0,
                "long_shares": long_shares,
                "terminal_reason": "short_put_absent_and_underlying_shares_detected",
            }
        if expiration is not None and as_of > expiration:
            return {
                "status": "EXPIRED",
                "action": "position_status_updated",
                "broker_contracts": 0.0,
                "long_shares": long_shares,
                "terminal_reason": "short_put_absent_after_expiration_without_assignment",
            }
        return {
            "status": "OPEN",
            "action": "option_missing_before_expiration",
            "broker_contracts": 0.0,
            "long_shares": long_shares,
            "terminal_reason": None,
            "warnings": ["short_put_not_in_broker_positions_before_expiration"],
        }

    if option_type == "call":
        call_group = call_group or []
        underlying_qty_at_open = _number(row["underlying_qty_at_open"])
        if underlying_qty_at_open is None:
            underlying_qty_at_open = _source_order_number(row, "share_quantity")
        if underlying_qty_at_open is None:
            underlying_qty_at_open = expected_shares
        if len(call_group) > 1:
            group_decision = _multi_call_group_decision(
                call_group,
                portfolio,
                ticker=ticker,
                as_of=as_of,
            )
            if group_decision is not None:
                return group_decision
        called_away_threshold = max(0.0, underlying_qty_at_open - expected_shares)
        if long_shares <= called_away_threshold + 1e-6 and expected_shares > 0:
            return {
                "status": "CALLED_AWAY",
                "action": "position_status_updated",
                "broker_contracts": 0.0,
                "long_shares": long_shares,
                "underlying_qty_at_open": underlying_qty_at_open,
                "terminal_reason": "short_call_absent_and_underlying_shares_reduced",
            }
        if expiration is not None and as_of > expiration:
            return {
                "status": "EXPIRED",
                "action": "position_status_updated",
                "broker_contracts": 0.0,
                "long_shares": long_shares,
                "underlying_qty_at_open": underlying_qty_at_open,
                "terminal_reason": "short_call_absent_after_expiration_stock_retained",
            }
        return {
            "status": "OPEN",
            "action": "option_missing_before_expiration",
            "broker_contracts": 0.0,
            "long_shares": long_shares,
            "underlying_qty_at_open": underlying_qty_at_open,
            "terminal_reason": None,
            "warnings": ["short_call_not_in_broker_positions_before_expiration"],
        }

    return {
        "status": "OPEN",
        "action": "unsupported_position_type",
        "broker_contracts": broker_contracts,
        "terminal_reason": None,
        "warnings": [f"unsupported_option_type:{option_type or 'missing'}"],
    }


def _broker_contracts(portfolio, symbol: str) -> float:
    return sum(
        abs(float(position.qty))
        for position in getattr(portfolio, "positions", [])
        if getattr(position, "symbol", "").upper() == symbol
        and getattr(position, "qty", 0.0) < 0
    )


def _open_call_groups(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if str(row["option_type"] or "").lower() != "call":
            continue
        ticker = str(row["ticker"] or "").upper()
        if not ticker:
            continue
        groups.setdefault(ticker, []).append(row)
    return groups


def _multi_call_group_decision(
    rows: list[sqlite3.Row],
    portfolio,
    *,
    ticker: str,
    as_of: date,
) -> dict[str, Any] | None:
    if any(_broker_contracts(portfolio, str(row["symbol"] or "").upper()) > 0 for row in rows):
        return None
    expirations = [_parse_date(row["expiration"]) for row in rows]
    if any(expiration is None for expiration in expirations):
        return _ambiguous_multi_call_result(
            portfolio,
            ticker,
            "multi_call_expiration_missing",
            rows,
        )
    long_shares = _long_shares(portfolio, ticker)
    total_expected_shares = sum(abs(_number(row["qty"]) or 0.0) * 100.0 for row in rows)
    opening_share_estimate = max(
        [
            _number(row["underlying_qty_at_open"])
            or _source_order_number(row, "share_quantity")
            or total_expected_shares
            for row in rows
        ]
    )
    all_called_away_threshold = max(0.0, opening_share_estimate - total_expected_shares)
    if long_shares <= all_called_away_threshold + 1e-6 and total_expected_shares > 0:
        return {
            "status": "CALLED_AWAY",
            "action": "position_status_updated",
            "broker_contracts": 0.0,
            "long_shares": long_shares,
            "underlying_qty_at_open": opening_share_estimate,
            "terminal_reason": "multi_short_call_absent_and_all_covered_shares_reduced",
        }
    if all(as_of > expiration for expiration in expirations if expiration):
        if long_shares + 1e-6 >= opening_share_estimate:
            return {
                "status": "EXPIRED",
                "action": "position_status_updated",
                "broker_contracts": 0.0,
                "long_shares": long_shares,
                "underlying_qty_at_open": opening_share_estimate,
                "terminal_reason": "multi_short_call_absent_after_expiration_stock_retained",
            }
        return _ambiguous_multi_call_result(
            portfolio,
            ticker,
            "multi_call_partial_assignment_after_expiration",
            rows,
            long_shares=long_shares,
        )
    return _ambiguous_multi_call_result(
        portfolio,
        ticker,
        "multi_call_missing_before_expiration",
        rows,
        long_shares=long_shares,
    )


def _ambiguous_multi_call_result(
    portfolio,
    ticker: str,
    reason: str,
    rows: list[sqlite3.Row],
    *,
    long_shares: float | None = None,
) -> dict[str, Any]:
    return {
        "status": "OPEN",
        "action": "ambiguous_multi_call_outcome",
        "broker_contracts": 0.0,
        "long_shares": _long_shares(portfolio, ticker) if long_shares is None else long_shares,
        "terminal_reason": None,
        "warnings": [
            reason,
            f"open_call_rows_for_ticker:{len(rows)}",
        ],
    }


def _long_shares(portfolio, ticker: str) -> float:
    if not ticker:
        return 0.0
    return sum(
        float(position.qty)
        for position in getattr(portfolio, "positions", [])
        if getattr(position, "is_long_equity", False)
        and getattr(position, "active_underlying", "").upper() == ticker
    )


def _position_stats(results: list[dict[str, Any]]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for result in results:
        key = str(result.get("action") or result.get("status") or "UNKNOWN")
        if result.get("action") == "position_status_updated":
            key = f"marked_{result.get('status')}"
        stats[key] = stats.get(key, 0) + 1
    return stats


def _source_order_number(row: sqlite3.Row, key: str) -> float | None:
    return _number(_json_dict(row["source_order_json"]).get(key))


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _json_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
