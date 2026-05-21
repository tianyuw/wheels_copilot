from __future__ import annotations

import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alpaca import AlpacaConfigError, AlpacaRequestError, fetch_alpaca_portfolio_snapshot
from .covered_call_planner import (
    build_covered_call_proposals,
    build_covered_call_shadow_orders,
    render_covered_call_report,
)
from .execution import execute_validated_shadow_orders
from .oms import OrderLedger, oms_enabled, reconcile_orders
from .order_validation import build_validated_shadow_orders
from .scan import scan_watchlist, write_scan_outputs
from .wheel_lifecycle import build_wheel_lifecycle_snapshot


DEFAULT_DAILY_RUN_ROOT = Path("workspace/daily_runs")
DEFAULT_LOCK_STALE_MINUTES = 180
SUCCESSFUL_EXECUTION_STATUSES = {
    "SUBMITTED",
    "DUPLICATE_IN_OMS",
    "DUPLICATE_AT_BROKER",
}


class DailyRunLocked(RuntimeError):
    pass


def run_autonomous_daily(
    config: dict[str, Any],
    *,
    tickers: list[str] | None = None,
    period: str = "1y",
    as_of: date | None = None,
    output_dir: Path | None = None,
    execute_paper: bool = False,
    trading_client=None,
    market_data_client=None,
) -> dict[str, Any]:
    """Run one autonomous wheel cycle.

    The runner is intentionally deterministic and artifact-first: every major
    step writes JSON to the daily run directory before the next trading action.
    Alpaca remains the source of truth for live portfolio state.
    """

    as_of = as_of or date.today()
    output_dir = output_dir or DEFAULT_DAILY_RUN_ROOT / as_of.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = _now_iso()
    result = _base_result(
        as_of=as_of,
        output_dir=output_dir,
        execute_paper=execute_paper,
        started_at=started_at,
    )
    try:
        with DailyRunLock(output_dir, config):
            _write_json(output_dir / "daily_run_state.json", result)
            _run_locked(
                result,
                config,
                tickers=tickers,
                period=period,
                as_of=as_of,
                output_dir=output_dir,
                execute_paper=execute_paper,
                trading_client=trading_client,
                market_data_client=market_data_client,
            )
    except DailyRunLocked as exc:
        result["status"] = "LOCKED"
        result["errors"].append({"scope": "lock", "error": str(exc)})
    except Exception as exc:
        result["status"] = "ERROR"
        result["errors"].append({"scope": "daily_runner", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        result["completed_at"] = _now_iso()
        if result["status"] == "RUNNING":
            result["status"] = "COMPLETED" if not result["errors"] else "ERROR"
        result_path, report_path, history_path, history_report_path = _result_paths(
            output_dir,
            result["status"],
            result["completed_at"],
        )
        _write_json(result_path, result)
        report = render_daily_run_report(result)
        report_path.write_text(report, encoding="utf-8")
        if history_path:
            _write_json(history_path, result)
        if history_report_path:
            history_report_path.write_text(report, encoding="utf-8")
    return result


def _run_locked(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    tickers: list[str] | None,
    period: str,
    as_of: date,
    output_dir: Path,
    execute_paper: bool,
    trading_client,
    market_data_client,
) -> None:
    if oms_enabled(config):
        _run_reconciliation(
            result,
            config,
            as_of=as_of,
            output_dir=output_dir,
            trading_client=trading_client,
        )
    else:
        result["steps"]["reconciliation"] = {"status": "SKIPPED", "reason": "oms_disabled"}

    if result["errors"]:
        result["status"] = "ERROR"
        return

    portfolio = _fetch_portfolio(
        result,
        config,
        output_dir=output_dir,
        trading_client=trading_client,
    )
    if portfolio is None:
        result["status"] = "ERROR"
        return

    lifecycle = _run_lifecycle(
        result,
        config,
        portfolio=portfolio,
        as_of=as_of,
        output_dir=output_dir,
    )
    _run_covered_calls(
        result,
        config,
        lifecycle=lifecycle,
        as_of=as_of,
        output_dir=output_dir,
        execute_paper=execute_paper,
        trading_client=trading_client,
        market_data_client=market_data_client,
    )
    if result["errors"]:
        result["status"] = "ERROR"
        return

    portfolio_for_csp = _fetch_portfolio(
        result,
        config,
        output_dir=output_dir,
        trading_client=trading_client,
        step_name="post_covered_call_portfolio",
    )
    if portfolio_for_csp is None:
        result["status"] = "ERROR"
        return

    _run_csp_scan_and_execution(
        result,
        config,
        tickers=tickers,
        period=period,
        as_of=as_of,
        output_dir=output_dir,
        portfolio=portfolio_for_csp,
        execute_paper=execute_paper,
        trading_client=trading_client,
    )


def _run_reconciliation(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    as_of: date,
    output_dir: Path,
    trading_client,
) -> None:
    try:
        reconciliation = reconcile_orders(config, client=trading_client, as_of=as_of)
    except (AlpacaConfigError, AlpacaRequestError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        result["errors"].append({"scope": "reconciliation", "error": error})
        result["steps"]["reconciliation"] = {"status": "ERROR", "error": error}
        return
    path = output_dir / "reconciliation_results.json"
    _write_json(path, reconciliation)
    step = {
        "status": "COMPLETED",
        "path": str(path),
        "summary": reconciliation.get("summary") or {},
        "position_summary": reconciliation.get("position_summary") or {},
        "order_count": reconciliation.get("order_count"),
        "position_count": reconciliation.get("position_count"),
    }
    if reconciliation.get("errors"):
        step["status"] = "ERROR"
        result["errors"].extend(reconciliation["errors"])
    result["steps"]["reconciliation"] = step


def _fetch_portfolio(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    output_dir: Path,
    trading_client,
    step_name: str = "portfolio",
):
    try:
        portfolio = fetch_alpaca_portfolio_snapshot(config, client=trading_client)
    except (AlpacaConfigError, AlpacaRequestError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        result["errors"].append({"scope": step_name, "error": error})
        result["steps"][step_name] = {"status": "ERROR", "error": error}
        return None
    payload = _portfolio_payload(portfolio)
    path = output_dir / f"{step_name}.json"
    _write_json(path, payload)
    result["steps"][step_name] = {
        "status": "COMPLETED",
        "path": str(path),
        "position_count": len(portfolio.positions),
        "open_order_count": len(portfolio.open_orders),
        "cash": portfolio.account.cash,
        "equity": portfolio.account.equity,
    }
    return portfolio


def _run_lifecycle(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    portfolio,
    as_of: date,
    output_dir: Path,
) -> dict[str, Any]:
    ledger_positions = _ledger_positions(config)
    lifecycle = build_wheel_lifecycle_snapshot(
        portfolio,
        config,
        ledger_positions=ledger_positions,
        as_of=as_of,
    )
    path = output_dir / "wheel_lifecycle.json"
    _write_json(path, lifecycle)
    result["steps"]["lifecycle"] = {
        "status": "COMPLETED",
        "path": str(path),
        "summary": lifecycle.get("summary") or {},
        "position_count": lifecycle.get("position_count"),
    }
    return lifecycle


def _run_covered_calls(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    lifecycle: dict[str, Any],
    as_of: date,
    output_dir: Path,
    execute_paper: bool,
    trading_client,
    market_data_client,
) -> None:
    cc_dir = output_dir / "covered_calls"
    cc_dir.mkdir(parents=True, exist_ok=True)
    proposals = build_covered_call_proposals(lifecycle, config, as_of=as_of)
    shadow_orders = build_covered_call_shadow_orders(proposals, config)
    validated_orders = build_validated_shadow_orders(
        shadow_orders,
        config,
        client=market_data_client,
    )
    paths = {
        "covered_call_proposals": cc_dir / "covered_call_proposals.json",
        "covered_call_shadow_orders": cc_dir / "covered_call_shadow_orders.json",
        "validated_covered_call_shadow_orders": cc_dir
        / "validated_covered_call_shadow_orders.json",
        "markdown": cc_dir / "covered_call_report.md",
    }
    _write_json(paths["covered_call_proposals"], proposals)
    _write_json(paths["covered_call_shadow_orders"], shadow_orders)
    _write_json(paths["validated_covered_call_shadow_orders"], validated_orders)
    paths["markdown"].write_text(
        render_covered_call_report(lifecycle, proposals),
        encoding="utf-8",
    )
    execution_result = None
    execution_errors: dict[str, Any] = {}
    if execute_paper:
        execution_path = cc_dir / "covered_call_execution_results.json"
        execution_result = execute_validated_shadow_orders(
            validated_orders,
            config,
            client=trading_client,
            previous_execution_results=_read_json(execution_path),
        )
        _write_json(execution_path, execution_result)
        paths["covered_call_execution_results"] = execution_path
        execution_errors = _append_execution_errors(
            result,
            scope="covered_call_execution",
            execution_result=execution_result,
        )
    result["steps"]["covered_calls"] = {
        "status": "ERROR" if execution_errors else "COMPLETED",
        "paths": _string_paths(paths),
        "proposal_summary": proposals.get("summary") or {},
        "proposal_count": proposals.get("proposal_count"),
        "shadow_order_count": shadow_orders.get("order_count"),
        "validation_summary": validated_orders.get("summary") or {},
        "execution_summary": execution_result.get("summary") if execution_result else None,
    }


def _run_csp_scan_and_execution(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    tickers: list[str] | None,
    period: str,
    as_of: date,
    output_dir: Path,
    portfolio,
    execute_paper: bool,
    trading_client,
) -> None:
    csp_dir = output_dir / "cash_secured_puts"
    scan = scan_watchlist(
        config=config,
        tickers=tickers,
        period=period,
        as_of=as_of,
        portfolio_snapshot=portfolio,
        portfolio_required=True,
    )
    paths = write_scan_outputs(scan, csp_dir, config=config)
    execution_result = None
    if execute_paper:
        execution_path = csp_dir / "execution_results.json"
        validated_path = paths.get("validated_shadow_orders")
        if not validated_path or not validated_path.exists():
            result["errors"].append(
                {
                    "scope": "cash_secured_put_validation",
                    "error": "validated_shadow_orders artifact missing",
                }
            )
            result["steps"]["cash_secured_puts"] = {
                "status": "ERROR",
                "paths": _string_paths(paths),
                "scan_summary": scan.get("summary") or {},
                "ticker_count": scan.get("ticker_count"),
                "execution_summary": None,
            }
            return
        validated_orders = json.loads(validated_path.read_text(encoding="utf-8"))
        execution_result = execute_validated_shadow_orders(
            validated_orders,
            config,
            client=trading_client,
            previous_execution_results=_read_json(execution_path),
        )
        _write_json(execution_path, execution_result)
        paths["execution_results"] = execution_path
        _append_execution_errors(
            result,
            scope="cash_secured_put_execution",
            execution_result=execution_result,
        )
    result["steps"]["cash_secured_puts"] = {
        "status": "ERROR" if result["errors"] else "COMPLETED",
        "paths": _string_paths(paths),
        "scan_summary": scan.get("summary") or {},
        "ticker_count": scan.get("ticker_count"),
        "execution_summary": execution_result.get("summary") if execution_result else None,
    }


def render_daily_run_report(result: dict[str, Any]) -> str:
    lines = [
        f"# Autonomous Wheel Daily Run - {result['run_date']}",
        "",
        f"- Status: `{result['status']}`",
        f"- Execute paper: `{result['execute_paper']}`",
        f"- Started: `{result['started_at']}`",
        f"- Completed: `{result.get('completed_at') or '-'}`",
        f"- Output: `{result['output_dir']}`",
        "",
        "| Step | Status | Summary |",
        "|---|---|---|",
    ]
    for name, step in result.get("steps", {}).items():
        lines.append(
            f"| {name} | {step.get('status', '-')} | {_step_summary(step)} |"
        )
    if result.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in result["errors"]:
            lines.append(f"- `{error.get('scope')}`: {error.get('error')}")
    return "\n".join(lines).rstrip() + "\n"


def _step_summary(step: dict[str, Any]) -> str:
    keys = [
        "summary",
        "position_summary",
        "proposal_summary",
        "validation_summary",
        "execution_summary",
        "scan_summary",
    ]
    parts = [f"{key}={step[key]}" for key in keys if step.get(key) is not None]
    if not parts and step.get("error"):
        parts.append(f"error={step['error']}")
    if not parts and step.get("reason"):
        parts.append(f"reason={step['reason']}")
    return "`" + "; ".join(parts).replace("|", "\\|") + "`"


def _ledger_positions(config: dict[str, Any]) -> list:
    if not oms_enabled(config):
        return []
    ledger = OrderLedger.from_config(config)
    try:
        return ledger.list_positions()
    finally:
        ledger.close()


def _portfolio_payload(portfolio) -> dict[str, Any]:
    return {
        "source": portfolio.source,
        "fetched_at": portfolio.fetched_at,
        "account": {
            "status": portfolio.account.status,
            "equity": portfolio.account.equity,
            "cash": portfolio.account.cash,
            "buying_power": portfolio.account.buying_power,
            "account_id": portfolio.account.account_id,
            "account_number": portfolio.account.account_number,
        },
        "positions": [
            {
                "symbol": position.symbol,
                "qty": position.qty,
                "asset_class": position.asset_class,
                "side": position.side,
                "market_value": position.market_value,
                "cost_basis": position.cost_basis,
                "underlying_symbol": position.underlying_symbol,
                "option_type": position.option_type,
                "expiration": position.expiration.isoformat()
                if position.expiration
                else None,
                "strike": position.strike,
            }
            for position in portfolio.positions
        ],
        "open_orders": [
            {
                "id": order.id,
                "symbol": order.symbol,
                "side": order.side,
                "qty": order.qty,
                "status": order.status,
                "position_intent": order.position_intent,
                "underlying_symbol": order.underlying_symbol,
                "option_type": order.option_type,
                "expiration": order.expiration.isoformat() if order.expiration else None,
                "strike": order.strike,
            }
            for order in portfolio.open_orders
        ],
    }


def _base_result(
    *,
    as_of: date,
    output_dir: Path,
    execute_paper: bool,
    started_at: str,
) -> dict[str, Any]:
    return {
        "run_date": as_of.isoformat(),
        "started_at": started_at,
        "completed_at": None,
        "status": "RUNNING",
        "execute_paper": execute_paper,
        "output_dir": str(output_dir),
        "steps": {},
        "errors": [],
    }


class DailyRunLock:
    def __init__(self, output_dir: Path, config: dict[str, Any]):
        self.path = output_dir / "daily_run.lock"
        cfg = config.get("daily_runner") or {}
        self.stale_after = timedelta(
            minutes=float(cfg.get("lock_stale_minutes") or DEFAULT_LOCK_STALE_MINUTES)
        )
        self.fd: int | None = None
        self.token: str | None = None

    def __enter__(self):
        self._acquire()
        return self

    def __exit__(self, *_exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self._owns_lock_file():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _acquire(self) -> None:
        for _attempt in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if not self._can_steal_lock():
                    raise DailyRunLocked(f"daily run already locked: {self.path}") from exc
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
        if self.fd is None:
            raise DailyRunLocked(f"daily run already locked: {self.path}")
        self.token = secrets.token_hex(12)
        payload = {
            "pid": os.getpid(),
            "created_at": _now_iso(),
            "token": self.token,
        }
        os.write(self.fd, json.dumps(payload, sort_keys=True).encode("utf-8"))

    def _owns_lock_file(self) -> bool:
        if not self.token:
            return False
        payload = _read_json_safely(self.path) or {}
        return payload.get("pid") == os.getpid() and payload.get("token") == self.token

    def _can_steal_lock(self) -> bool:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return False
        modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        if datetime.now(timezone.utc) - modified_at <= self.stale_after:
            return False
        lock_payload = _read_json_safely(self.path) or {}
        pid = _int_or_none(lock_payload.get("pid"))
        if pid is None:
            return True
        return not _pid_is_alive(pid)


def _append_execution_errors(
    result: dict[str, Any],
    *,
    scope: str,
    execution_result: dict[str, Any] | None,
) -> dict[str, Any]:
    if not execution_result:
        return {}
    summary = execution_result.get("summary") or {}
    bad = {
        status: count
        for status, count in summary.items()
        if count and status not in SUCCESSFUL_EXECUTION_STATUSES
    }
    if bad:
        result["errors"].append(
            {
                "scope": scope,
                "error": f"execution_not_fully_successful:{bad}",
            }
        )
    return bad


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_paths(
    output_dir: Path,
    status: str,
    completed_at: str,
) -> tuple[Path, Path, Path | None, Path | None]:
    stamp = (
        completed_at.replace("-", "")
        .replace(":", "")
        .replace("+", "Z")
        .replace("T", "_")
    )
    if status == "LOCKED":
        return (
            output_dir / f"daily_run_locked_{stamp}.json",
            output_dir / f"daily_run_locked_{stamp}.md",
            None,
            None,
        )
    return (
        output_dir / "daily_run_result.json",
        output_dir / "daily_run_report.md",
        output_dir / f"daily_run_result_{stamp}.json",
        output_dir / f"daily_run_report_{stamp}.md",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_safely(path: Path) -> dict[str, Any] | None:
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def _string_paths(paths: dict[str, Path]) -> dict[str, str]:
    return {name: str(path) for name, path in paths.items()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
