from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch

from wheels_copilot.daily_runner import run_autonomous_daily
from wheels_copilot.models import BrokerAccountSnapshot, PortfolioSnapshot


class DailyRunnerTests(unittest.TestCase):
    def test_autonomous_daily_runs_reconcile_cc_then_csp_and_writes_artifacts(self):
        events: list[str] = []
        previous_seen: list[dict | None] = []
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "covered_calls").mkdir()
            (output / "cash_secured_puts").mkdir()
            (output / "covered_calls" / "covered_call_execution_results.json").write_text(
                json.dumps({"orders": [{"client_order_id": "old-cc", "status": "SUBMITTED"}]}),
                encoding="utf-8",
            )
            (output / "cash_secured_puts" / "execution_results.json").write_text(
                json.dumps({"orders": [{"client_order_id": "old-csp", "status": "SUBMITTED"}]}),
                encoding="utf-8",
            )

            with _patched_daily_dependencies(events, previous_seen):
                result = run_autonomous_daily(
                    _config(),
                    tickers=["AAPL"],
                    as_of=date(2026, 5, 21),
                    output_dir=output,
                    execute_paper=True,
                )

            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(
                events,
                [
                    "reconcile",
                    "fetch_portfolio",
                    "ledger_positions",
                    "lifecycle",
                    "cc_proposals",
                    "cc_shadow",
                    "validate_covered_call",
                    "execute_covered_call",
                    "fetch_portfolio",
                    "scan",
                    "write_scan_outputs",
                    "execute_cash_secured_put",
                ],
            )
            self.assertEqual(previous_seen[0]["orders"][0]["client_order_id"], "old-cc")
            self.assertEqual(previous_seen[1]["orders"][0]["client_order_id"], "old-csp")
            self.assertTrue((output / "daily_run_result.json").exists())
            self.assertTrue((output / "daily_run_report.md").exists())
            self.assertEqual(
                result["steps"]["covered_calls"]["execution_summary"],
                {"SUBMITTED": 1},
            )
            self.assertEqual(
                result["steps"]["cash_secured_puts"]["execution_summary"],
                {"SUBMITTED": 1},
            )

    def test_reconciliation_error_fails_closed_before_trading_steps(self):
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "wheels_copilot.daily_runner.reconcile_orders",
                    side_effect=lambda *_args, **_kwargs: events.append("reconcile")
                    or {
                        "summary": {},
                        "position_summary": {"portfolio_snapshot_unavailable": 1},
                        "errors": [
                            {
                                "scope": "position_reconciliation",
                                "error": "AlpacaRequestError: down",
                            }
                        ],
                    },
                ),
                patch(
                    "wheels_copilot.daily_runner.fetch_alpaca_portfolio_snapshot",
                    side_effect=AssertionError("portfolio should not be fetched"),
                ),
                patch(
                    "wheels_copilot.daily_runner.execute_validated_shadow_orders",
                    side_effect=AssertionError("orders should not execute"),
                ),
            ):
                result = run_autonomous_daily(
                    _config(),
                    as_of=date(2026, 5, 21),
                    output_dir=Path(tmp),
                    execute_paper=True,
                )

            self.assertEqual(events, ["reconcile"])
            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(result["errors"][0]["scope"], "position_reconciliation")
            self.assertNotIn("covered_calls", result["steps"])

    def test_covered_call_execution_error_skips_csp_step(self):
        events: list[str] = []
        previous_seen: list[dict | None] = []

        def failing_execute(validated_orders, *_args, previous_execution_results=None, **_kwargs):
            strategy = validated_orders["orders"][0]["strategy"]
            events.append(f"execute_{strategy}")
            previous_seen.append(previous_execution_results)
            return {"summary": {"SUBMIT_ERROR": 1}, "orders": []}

        with tempfile.TemporaryDirectory() as tmp:
            with (
                _patched_daily_dependencies(events, previous_seen),
                patch(
                    "wheels_copilot.daily_runner.execute_validated_shadow_orders",
                    side_effect=failing_execute,
                ),
                patch(
                    "wheels_copilot.daily_runner.scan_watchlist",
                    side_effect=AssertionError("CSP scan should be skipped"),
                ),
            ):
                result = run_autonomous_daily(
                    _config(),
                    as_of=date(2026, 5, 21),
                    output_dir=Path(tmp),
                    execute_paper=True,
                )

            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(result["errors"][0]["scope"], "covered_call_execution")
            self.assertNotIn("cash_secured_puts", result["steps"])
            self.assertEqual(result["steps"]["covered_calls"]["status"], "ERROR")

    def test_execute_paper_false_builds_artifacts_without_submitting_orders(self):
        events: list[str] = []
        previous_seen: list[dict | None] = []
        with tempfile.TemporaryDirectory() as tmp:
            with _patched_daily_dependencies(events, previous_seen):
                result = run_autonomous_daily(
                    _config(),
                    as_of=date(2026, 5, 21),
                    output_dir=Path(tmp),
                    execute_paper=False,
                )

            self.assertEqual(result["status"], "COMPLETED")
            self.assertNotIn("execute_covered_call", events)
            self.assertNotIn("execute_cash_secured_put", events)
            self.assertIsNone(result["steps"]["covered_calls"]["execution_summary"])
            self.assertIsNone(result["steps"]["cash_secured_puts"]["execution_summary"])

    def test_existing_lock_blocks_run_without_removing_lock(self):
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            lock = output / "daily_run.lock"
            lock.write_text("busy", encoding="utf-8")
            prior_result = output / "daily_run_result.json"
            prior_result.write_text('{"status":"COMPLETED"}', encoding="utf-8")

            with patch(
                "wheels_copilot.daily_runner.reconcile_orders",
                side_effect=lambda *_args, **_kwargs: events.append("reconcile"),
            ):
                result = run_autonomous_daily(
                    _config(lock_stale_minutes=9999),
                    as_of=date(2026, 5, 21),
                    output_dir=output,
                    execute_paper=True,
                )

            self.assertEqual(result["status"], "LOCKED")
            self.assertEqual(events, [])
            self.assertTrue(lock.exists())
            self.assertEqual(prior_result.read_text(encoding="utf-8"), '{"status":"COMPLETED"}')
            self.assertTrue(list(output.glob("daily_run_locked_*.json")))

    def test_stale_lock_with_live_pid_is_not_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            lock = output / "daily_run.lock"
            lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            stale_time = time.time() - 3600
            os.utime(lock, (stale_time, stale_time))

            result = run_autonomous_daily(
                _config(lock_stale_minutes=1),
                as_of=date(2026, 5, 21),
                output_dir=output,
                execute_paper=True,
            )

            self.assertEqual(result["status"], "LOCKED")
            self.assertTrue(lock.exists())


@contextmanager
def _patched_daily_dependencies(events: list[str], previous_seen: list[dict | None]):
    portfolio = PortfolioSnapshot(
        account=BrokerAccountSnapshot(status="ACTIVE", equity=500000, cash=500000),
        positions=[],
        open_orders=[],
        source="test",
    )

    def fake_reconcile(*_args, **_kwargs):
        events.append("reconcile")
        return {
            "summary": {},
            "position_summary": {},
            "order_count": 0,
            "position_count": 0,
            "orders": [],
            "positions": [],
        }

    def fake_fetch_portfolio(*_args, **_kwargs):
        events.append("fetch_portfolio")
        return portfolio

    def fake_ledger_positions(_config):
        events.append("ledger_positions")
        return []

    def fake_lifecycle(*_args, **_kwargs):
        events.append("lifecycle")
        return {
            "as_of": "2026-05-21",
            "generated_at": "2026-05-21T10:00:00",
            "summary": {"ASSIGNED": 1},
            "position_count": 1,
            "positions": [],
        }

    def fake_cc_proposals(*_args, **_kwargs):
        events.append("cc_proposals")
        return {
            "scan_date": "2026-05-21",
            "generated_at": "2026-05-21T10:00:00",
            "summary": {"PROPOSED": 1},
            "proposal_count": 1,
            "proposals": [],
            "non_eligible_audit": [],
        }

    def fake_cc_shadow(*_args, **_kwargs):
        events.append("cc_shadow")
        return _shadow_orders("covered_call")

    def fake_validate(shadow_orders, *_args, **_kwargs):
        strategy = shadow_orders["orders"][0]["strategy"]
        events.append(f"validate_{strategy}")
        return _validated_orders(strategy)

    def fake_execute(validated_orders, *_args, previous_execution_results=None, **_kwargs):
        strategy = validated_orders["orders"][0]["strategy"]
        events.append(f"execute_{strategy}")
        previous_seen.append(previous_execution_results)
        return {
            "summary": {"SUBMITTED": 1},
            "orders": [
                {
                    "client_order_id": f"new-{strategy}",
                    "status": "SUBMITTED",
                }
            ],
        }

    def fake_scan(*_args, **_kwargs):
        events.append("scan")
        return {
            "scan_date": "2026-05-21",
            "generated_at": "2026-05-21T10:00:00",
            "period": "1y",
            "ticker_count": 1,
            "portfolio": {},
            "summary": {"AUTO_TRADE": 1},
            "results": [],
        }

    def fake_write_scan_outputs(scan, output_dir, *_args, **_kwargs):
        events.append("write_scan_outputs")
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "json": output_dir / "scan_results.json",
            "markdown": output_dir / "scan_report.md",
            "csv": output_dir / "scan_summary.csv",
            "trade_proposals": output_dir / "trade_proposals.json",
            "shadow_orders": output_dir / "shadow_orders.json",
            "validated_shadow_orders": output_dir / "validated_shadow_orders.json",
        }
        paths["json"].write_text(json.dumps(scan), encoding="utf-8")
        paths["markdown"].write_text("# scan\n", encoding="utf-8")
        paths["csv"].write_text("ticker\n", encoding="utf-8")
        paths["trade_proposals"].write_text("{}", encoding="utf-8")
        paths["shadow_orders"].write_text("{}", encoding="utf-8")
        paths["validated_shadow_orders"].write_text(
            json.dumps(_validated_orders("cash_secured_put")),
            encoding="utf-8",
        )
        return paths

    patches = (
        patch("wheels_copilot.daily_runner.reconcile_orders", side_effect=fake_reconcile),
        patch(
            "wheels_copilot.daily_runner.fetch_alpaca_portfolio_snapshot",
            side_effect=fake_fetch_portfolio,
        ),
        patch("wheels_copilot.daily_runner._ledger_positions", side_effect=fake_ledger_positions),
        patch(
            "wheels_copilot.daily_runner.build_wheel_lifecycle_snapshot",
            side_effect=fake_lifecycle,
        ),
        patch(
            "wheels_copilot.daily_runner.build_covered_call_proposals",
            side_effect=fake_cc_proposals,
        ),
        patch(
            "wheels_copilot.daily_runner.build_covered_call_shadow_orders",
            side_effect=fake_cc_shadow,
        ),
        patch(
            "wheels_copilot.daily_runner.build_validated_shadow_orders",
            side_effect=fake_validate,
        ),
        patch(
            "wheels_copilot.daily_runner.execute_validated_shadow_orders",
            side_effect=fake_execute,
        ),
        patch("wheels_copilot.daily_runner.scan_watchlist", side_effect=fake_scan),
        patch(
            "wheels_copilot.daily_runner.write_scan_outputs",
            side_effect=fake_write_scan_outputs,
        ),
    )
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        yield


def _shadow_orders(strategy: str) -> dict:
    return {
        "scan_date": "2026-05-21",
        "generated_at": "2026-05-21T10:00:00",
        "broker": "alpaca",
        "order_count": 1,
        "orders": [
            {
                "shadow_order_id": f"shadow-{strategy}",
                "proposal_id": f"shadow-{strategy}",
                "strategy": strategy,
                "ticker": "AAPL",
                "payload": {
                    "symbol": "AAPL260529C00095000"
                    if strategy == "covered_call"
                    else "AAPL260529P00090000",
                    "client_order_id": f"shadow-{strategy}",
                },
            }
        ],
    }


def _validated_orders(strategy: str) -> dict:
    return {
        "scan_date": "2026-05-21",
        "generated_at": "2026-05-21T10:00:00",
        "broker": "alpaca",
        "summary": {"SUBMIT_READY": 1},
        "orders": [
            {
                "shadow_order_id": f"shadow-{strategy}",
                "proposal_id": f"shadow-{strategy}",
                "strategy": strategy,
                "ticker": "AAPL",
                "submit_ready": True,
                "validated_payload": {"client_order_id": f"shadow-{strategy}"},
            }
        ],
    }


def _config(lock_stale_minutes: int = 180) -> dict:
    return {
        "oms": {"enabled": True, "db_path": ":memory:"},
        "daily_runner": {"lock_stale_minutes": lock_stale_minutes},
    }


if __name__ == "__main__":
    unittest.main()
