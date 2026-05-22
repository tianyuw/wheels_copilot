from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wheels_copilot.sensitivity import (
    apply_dotted_patch,
    build_run_specs,
    compute_resource_plan,
    leaderboard_sort_key,
    load_scenario_file,
    return_per_drawdown,
    run_directory_state,
    stable_config_hash,
    write_aggregate_outputs,
    write_json,
)


class SensitivityTests(unittest.TestCase):
    def test_apply_dotted_patch_deep_copies_and_validates_path(self):
        base = _base_config()

        patched = apply_dotted_patch(
            base,
            {
                "portfolio.max_active_tickers": 8,
                "csp_selector.delta_policy.normal_support.target_delta_min": 0.12,
            },
        )

        self.assertEqual(base["portfolio"]["max_active_tickers"], 5)
        self.assertEqual(patched["portfolio"]["max_active_tickers"], 8)
        self.assertEqual(
            patched["csp_selector"]["delta_policy"]["normal_support"]["target_delta_min"],
            0.12,
        )
        with self.assertRaises(ValueError):
            apply_dotted_patch(base, {"portfolio.missing": 1})

    def test_apply_dotted_patch_rejects_type_mismatch(self):
        with self.assertRaises(ValueError):
            apply_dotted_patch(_base_config(), {"portfolio.max_active_tickers": 8.5})

    def test_apply_dotted_patch_rejects_non_finite_float(self):
        with self.assertRaises(ValueError):
            apply_dotted_patch(
                _base_config(),
                {"csp_selector.min_strike_distance_pct": float("inf")},
            )

    def test_build_run_specs_deduplicates_baseline_and_is_stable(self):
        scenario = {
            "name": "unit",
            "experiments": [
                {
                    "name": "capital",
                    "matrix": {
                        "portfolio.max_active_tickers": [5, 8],
                        "execution.max_orders_per_run": [3],
                    },
                }
            ],
        }

        specs = build_run_specs(
            scenario=scenario,
            base_config=_base_config(),
            start="2026-01-01",
            end="2026-01-31",
            universe=["AAPL", "MSFT"],
            universe_name="unit",
            max_runs=5,
            max_combinations_per_experiment=4,
            allow_large_matrix=False,
            git_sha="abc",
            data_source_metadata={"provider": "unit"},
        )
        specs_again = build_run_specs(
            scenario=scenario,
            base_config=_base_config(),
            start="2026-01-01",
            end="2026-01-31",
            universe=["MSFT", "AAPL"],
            universe_name="unit",
            max_runs=5,
            max_combinations_per_experiment=4,
            allow_large_matrix=False,
            git_sha="abc",
            data_source_metadata={"provider": "unit"},
        )

        self.assertEqual([spec.run_id for spec in specs], [spec.run_id for spec in specs_again])
        self.assertTrue(specs[0].is_baseline)
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[1].parameter_patch["portfolio.max_active_tickers"], 8)
        self.assertEqual(specs[1].name, "capital_001")

    def test_run_ids_do_not_change_with_git_sha(self):
        scenario = {
            "name": "unit",
            "experiments": [
                {
                    "name": "capital",
                    "matrix": {"portfolio.max_active_tickers": [8]},
                }
            ],
        }

        first = build_run_specs(
            scenario=scenario,
            base_config=_base_config(),
            start="2026-01-01",
            end="2026-01-31",
            universe=["AAPL"],
            universe_name="unit",
            max_runs=5,
            max_combinations_per_experiment=4,
            allow_large_matrix=False,
            git_sha="abc",
            data_source_metadata={"provider": "unit"},
        )
        second = build_run_specs(
            scenario=scenario,
            base_config=_base_config(),
            start="2026-01-01",
            end="2026-01-31",
            universe=["AAPL"],
            universe_name="unit",
            max_runs=5,
            max_combinations_per_experiment=4,
            allow_large_matrix=False,
            git_sha="def",
            data_source_metadata={"provider": "unit"},
        )

        self.assertEqual([spec.run_id for spec in first], [spec.run_id for spec in second])
        self.assertEqual(first[0].git_sha, "abc")
        self.assertEqual(second[0].git_sha, "def")

    def test_build_run_specs_enforces_experiment_cap(self):
        scenario = {
            "name": "unit",
            "experiments": [
                {
                    "name": "too_large",
                    "matrix": {
                        "portfolio.max_active_tickers": [5, 8, 10],
                        "execution.max_orders_per_run": [3, 5],
                    },
                }
            ],
        }

        with self.assertRaises(ValueError):
            build_run_specs(
                scenario=scenario,
                base_config=_base_config(),
                start="2026-01-01",
                end="2026-01-31",
                universe=["AAPL"],
                universe_name="unit",
                max_runs=20,
                max_combinations_per_experiment=4,
                allow_large_matrix=False,
                git_sha="abc",
                data_source_metadata={"provider": "unit"},
            )

    def test_build_run_specs_rejects_invalid_parameter_constraints(self):
        scenario = {
            "name": "unit",
            "experiments": [
                {
                    "name": "bad_dte",
                    "matrix": {
                        "csp_selector.dte_min": [14],
                        "csp_selector.dte_max": [9],
                    },
                }
            ],
        }

        with self.assertRaises(ValueError):
            build_run_specs(
                scenario=scenario,
                base_config=_base_config(),
                start="2026-01-01",
                end="2026-01-31",
                universe=["AAPL"],
                universe_name="unit",
                max_runs=5,
                max_combinations_per_experiment=4,
                allow_large_matrix=False,
                git_sha="abc",
                data_source_metadata={"provider": "unit"},
            )

    def test_compute_resource_plan_auto_respects_cpu_memory_and_cap(self):
        plan = compute_resource_plan(
            total_runs=20,
            requested_workers="auto",
            memory_per_worker_gb=3.0,
            reserve_memory_gb=6.0,
            configured_max_workers=6,
            cpu_reserve=2,
            cpu_count=10,
            available_memory_gb=21.0,
        )

        self.assertEqual(plan.workers, 5)

    def test_compute_resource_plan_explicit_workers_respect_cap(self):
        plan = compute_resource_plan(
            total_runs=20,
            requested_workers="12",
            memory_per_worker_gb=3.0,
            reserve_memory_gb=6.0,
            configured_max_workers=6,
            cpu_reserve=2,
            cpu_count=10,
            available_memory_gb=21.0,
        )

        self.assertEqual(plan.workers, 6)

    def test_compute_resource_plan_rejects_invalid_memory_assumption(self):
        with self.assertRaises(ValueError):
            compute_resource_plan(
                total_runs=20,
                requested_workers="auto",
                memory_per_worker_gb=0.0,
                reserve_memory_gb=6.0,
                configured_max_workers=6,
                cpu_reserve=2,
            )

    def test_run_directory_state_requires_completed_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "sr_unit"
            self.assertEqual(run_directory_state(run_dir), "missing")
            run_dir.mkdir()
            write_json(run_dir / "run_metadata.json", {"status": "running"})
            self.assertEqual(run_directory_state(run_dir), "partial")
            write_json(run_dir / "run_metadata.json", {"status": "failed"})
            self.assertEqual(run_directory_state(run_dir), "failed")
            write_json(run_dir / "run_metadata.json", {"status": "completed"})
            write_json(run_dir / "summary.json", {"total_return_pct": 1.0})
            self.assertEqual(run_directory_state(run_dir), "partial")
            write_json(run_dir / "backtest_results.json", {})
            self.assertEqual(run_directory_state(run_dir), "partial")
            write_json(run_dir / "parameter_patch.json", {})
            self.assertEqual(run_directory_state(run_dir), "completed")

    def test_flatten_run_metrics_marks_low_sample_and_baseline_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            baseline = output_dir / "runs" / "sr_base"
            variant = output_dir / "runs" / "sr_variant"
            baseline.mkdir(parents=True)
            variant.mkdir(parents=True)
            _write_completed_run(
                baseline,
                run_id="sr_base",
                name="baseline",
                is_baseline=True,
                patch={},
                total_return_pct=1.0,
                max_drawdown_pct=-0.5,
                opened_short_puts=10,
            )
            _write_completed_run(
                variant,
                run_id="sr_variant",
                name="variant",
                is_baseline=False,
                patch={"portfolio.max_active_tickers": 8},
                total_return_pct=1.5,
                max_drawdown_pct=-0.6,
                opened_short_puts=40,
            )

            rows = write_aggregate_outputs(output_dir)

            by_run = {row["run_id"]: row for row in rows}
            self.assertTrue(by_run["sr_base"]["sample_size_flag"])
            self.assertFalse(by_run["sr_variant"]["sample_size_flag"])
            self.assertEqual(by_run["sr_variant"]["delta_total_return_pct"], 0.5)
            self.assertTrue((output_dir / "leaderboard.csv").exists())
            self.assertTrue((output_dir / "report.md").exists())

    def test_load_scenario_file_validates_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scenario.yaml"
            path.write_text("name: unit\nexperiments: []\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_scenario_file(path)

    def test_stable_config_hash_ignores_dict_insertion_order(self):
        left = {"b": 2, "a": {"y": 2, "x": 1}}
        right = {"a": {"x": 1, "y": 2}, "b": 2}

        self.assertEqual(stable_config_hash(left), stable_config_hash(right))

    def test_return_per_drawdown_handles_zero_drawdown(self):
        self.assertEqual(return_per_drawdown(1.0, 0.0), 1_000_000.0)
        self.assertEqual(return_per_drawdown(0.0, 0.0), 0.0)
        self.assertEqual(return_per_drawdown(-1.0, 0.0), -1_000_000.0)

    def test_leaderboard_sort_keeps_zero_score_above_missing_score(self):
        zero = {"is_baseline": False, "return_per_drawdown": 0.0, "total_return_pct": 0.0}
        missing = {"is_baseline": False, "return_per_drawdown": None, "total_return_pct": 1.0}

        self.assertLess(leaderboard_sort_key(zero), leaderboard_sort_key(missing))


def _base_config() -> dict:
    return {
        "execution": {"max_orders_per_run": 3},
        "portfolio": {"max_active_tickers": 5},
        "csp_selector": {
            "dte_min": 1,
            "dte_max": 9,
            "min_strike_distance_pct": 3.0,
            "min_strike_distance_atr_multiple": 1.0,
            "delta_policy": {
                "normal_support": {
                    "target_delta_min": 0.10,
                    "target_delta_max": 0.25,
                }
            },
        },
        "cc_selector": {
            "target_delta_min": 0.10,
            "target_delta_max": 0.35,
        },
    }


def _write_completed_run(
    run_dir: Path,
    *,
    run_id: str,
    name: str,
    is_baseline: bool,
    patch: dict,
    total_return_pct: float,
    max_drawdown_pct: float,
    opened_short_puts: int,
) -> None:
    metadata = {
        "run_id": run_id,
        "name": name,
        "experiment_name": "unit",
        "is_baseline": is_baseline,
        "status": "completed",
    }
    summary = {
        "total_return_pct": total_return_pct,
        "ending_equity": 500000 * (1 + total_return_pct / 100),
        "max_drawdown_pct": max_drawdown_pct,
        "realized_option_pnl": 1000.0,
        "realized_stock_pnl": 0.0,
        "opened_short_puts": opened_short_puts,
        "assigned": 1,
        "opened_covered_calls": 2,
        "called_away": 1,
        "average_capital_utilization_pct": 12.0,
        "max_capital_utilization_pct": 24.0,
        "data_issue_count": 0,
        "rejected_reason_counts": {"trend_filter_reject": 3},
    }
    write_json(run_dir / "run_metadata.json", metadata)
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "parameter_patch.json", patch)
    write_json(run_dir / "backtest_results.json", {})


if __name__ == "__main__":
    unittest.main()
