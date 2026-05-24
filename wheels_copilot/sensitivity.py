from __future__ import annotations

import csv
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

import yaml

from .backtest import BACKTEST_VERSION, run_backtest, write_backtest_outputs
from .historical_data import FlatFilesStore


SENSITIVITY_SCHEMA_VERSION = "sensitivity_runner.v1"


@dataclass(frozen=True)
class SensitivityRunSpec:
    run_id: str
    name: str
    experiment_name: str
    parameter_patch: dict[str, Any]
    config: dict[str, Any]
    start: str
    end: str
    universe: list[str]
    universe_name: str
    is_baseline: bool
    config_hash: str
    backtest_version: str
    git_sha: str
    data_source_metadata: dict[str, Any]


@dataclass(frozen=True)
class ResourcePlan:
    workers: int
    requested_workers: str
    cpu_count: int
    cpu_reserve: int
    available_memory_gb: float | None
    reserve_memory_gb: float
    memory_per_worker_gb: float
    configured_max_workers: int
    total_runs: int


def load_scenario_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Scenario file must contain a mapping: {path}")
    if not payload.get("name"):
        raise ValueError("Scenario file must define name")
    experiments = payload.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("Scenario file must define a non-empty experiments list")
    for experiment in experiments:
        if not isinstance(experiment, dict) or not experiment.get("name"):
            raise ValueError("Each experiment must be a mapping with name")
        modes = [
            key
            for key in ("matrix", "variants", "variant_axes")
            if key in experiment
        ]
        if len(modes) != 1:
            raise ValueError(
                f"Experiment {experiment.get('name')} must define exactly one of "
                "matrix, variants, or variant_axes"
            )
        if "matrix" in experiment:
            _validate_matrix_shape(experiment)
        elif "variants" in experiment:
            _validate_variants_shape(experiment)
        else:
            _validate_variant_axes_shape(experiment)
    return payload


def _validate_matrix_shape(experiment: dict[str, Any]) -> None:
    matrix = experiment.get("matrix")
    if not isinstance(matrix, dict) or not matrix:
        raise ValueError(f"Experiment {experiment.get('name')} must define matrix")
    for path_key, values in matrix.items():
        if not isinstance(path_key, str) or not path_key:
            raise ValueError(f"Invalid matrix path in {experiment.get('name')}: {path_key}")
        if not isinstance(values, list) or not values:
            raise ValueError(f"Matrix path {path_key} must contain a non-empty list")


def _validate_variants_shape(experiment: dict[str, Any]) -> None:
    variants = experiment.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ValueError(f"Experiment {experiment.get('name')} must define variants")
    for index, variant in enumerate(variants, start=1):
        if not isinstance(variant, dict):
            raise ValueError(f"Variant {index} in {experiment.get('name')} must be a mapping")
        patch = variant.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise ValueError(
                f"Variant {index} in {experiment.get('name')} must define patch"
            )
        _validate_patch_shape(patch, f"variant {index} in {experiment.get('name')}")


def _validate_variant_axes_shape(experiment: dict[str, Any]) -> None:
    axes = experiment.get("variant_axes")
    if not isinstance(axes, dict) or not axes:
        raise ValueError(f"Experiment {experiment.get('name')} must define variant_axes")
    for axis_name, choices in axes.items():
        if not isinstance(axis_name, str) or not axis_name:
            raise ValueError(f"Invalid variant axis in {experiment.get('name')}: {axis_name}")
        if not isinstance(choices, list) or not choices:
            raise ValueError(
                f"Variant axis {axis_name} in {experiment.get('name')} must contain choices"
            )
        for index, choice in enumerate(choices, start=1):
            if not isinstance(choice, dict):
                raise ValueError(
                    f"Choice {index} in axis {axis_name} must be a mapping"
                )
            if not choice.get("name"):
                raise ValueError(
                    f"Choice {index} in axis {axis_name} must define name"
                )
            patch = choice.get("patch")
            if not isinstance(patch, dict) or not patch:
                raise ValueError(
                    f"Choice {index} in axis {axis_name} must define patch"
                )
            _validate_patch_shape(patch, f"choice {index} in axis {axis_name}")


def _validate_patch_shape(patch: dict[Any, Any], context: str) -> None:
    for path_key in patch:
        if not isinstance(path_key, str) or not path_key:
            raise ValueError(f"Invalid patch path in {context}: {path_key}")


def build_run_specs(
    *,
    scenario: dict[str, Any],
    base_config: dict[str, Any],
    start: str,
    end: str,
    universe: list[str],
    universe_name: str,
    max_runs: int,
    max_combinations_per_experiment: int,
    allow_large_matrix: bool,
    git_sha: str | None = None,
    backtest_version: str = BACKTEST_VERSION,
    data_source_metadata: dict[str, Any] | None = None,
) -> list[SensitivityRunSpec]:
    if max_runs < 1:
        raise ValueError("max_runs must be at least 1")
    validate_config_constraints(base_config)
    git_sha = git_sha or current_git_sha()
    data_source_metadata = data_source_metadata or default_data_source_metadata()
    specs: list[SensitivityRunSpec] = []
    seen_run_ids: set[str] = set()

    baseline = _make_run_spec(
        name="baseline",
        experiment_name="baseline",
        parameter_patch={},
        config=copy.deepcopy(base_config),
        start=start,
        end=end,
        universe=universe,
        universe_name=universe_name,
        is_baseline=True,
        git_sha=git_sha,
        backtest_version=backtest_version,
        data_source_metadata=data_source_metadata,
    )
    specs.append(baseline)
    seen_run_ids.add(baseline.run_id)

    for experiment in scenario["experiments"]:
        combinations = expand_experiment_patches(experiment)
        if (
            len(combinations) > max_combinations_per_experiment
            and not allow_large_matrix
        ):
            raise ValueError(
                f"Experiment {experiment['name']} expands to {len(combinations)} "
                f"runs, above cap {max_combinations_per_experiment}. "
                "Use --allow-large-matrix to override."
            )
        variant_index = 0
        for patch, name_suffix in combinations:
            for path_key, value in patch.items():
                current = get_dotted_path(base_config, path_key)
                validate_patch_value(path_key, current, value)
            config = apply_dotted_patch(base_config, patch)
            validate_config_constraints(config)
            spec = _make_run_spec(
                name=format_spec_name(str(experiment["name"]), variant_index + 1, name_suffix),
                experiment_name=str(experiment["name"]),
                parameter_patch=patch,
                config=config,
                start=start,
                end=end,
                universe=universe,
                universe_name=universe_name,
                is_baseline=False,
                git_sha=git_sha,
                backtest_version=backtest_version,
                data_source_metadata=data_source_metadata,
            )
            if spec.run_id in seen_run_ids:
                continue
            variant_index += 1
            spec = replace(
                spec,
                name=format_spec_name(
                    str(experiment["name"]),
                    variant_index,
                    name_suffix,
                ),
            )
            if len(specs) >= max_runs:
                raise ValueError(f"Scenario expands to more than max_runs={max_runs}")
            specs.append(spec)
            seen_run_ids.add(spec.run_id)
    return specs


def expand_experiment_patches(
    experiment: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    if "matrix" in experiment:
        matrix = experiment["matrix"]
        keys = list(matrix)
        return [
            (dict(zip(keys, combination)), "")
            for combination in product(*(matrix[key] for key in keys))
        ]
    if "variants" in experiment:
        return [
            (dict(variant["patch"]), safe_name(str(variant.get("name") or index)))
            for index, variant in enumerate(experiment["variants"], start=1)
        ]
    axes = experiment["variant_axes"]
    axis_names = list(axes)
    rows: list[tuple[dict[str, Any], str]] = []
    for choices in product(*(axes[axis] for axis in axis_names)):
        patch: dict[str, Any] = {}
        suffix_parts: list[str] = []
        for choice in choices:
            suffix_parts.append(safe_name(str(choice["name"])))
            patch = merge_patch(patch, dict(choice["patch"]))
        rows.append((patch, "__".join(suffix_parts)))
    return rows


def merge_patch(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key in merged and merged[key] != value:
            raise ValueError(f"Conflicting patch value for {key}: {merged[key]} vs {value}")
        merged[key] = value
    return merged


def format_spec_name(experiment_name: str, index: int, suffix: str) -> str:
    base = f"{experiment_name}_{index:03d}"
    if not suffix:
        return base
    return f"{base}_{suffix[:96]}"


def safe_name(value: str) -> str:
    result = "".join(
        char.lower() if char.isalnum() else "_"
        for char in value.strip()
    ).strip("_")
    while "__" in result:
        result = result.replace("__", "_")
    return result or "variant"


def get_dotted_path(config: dict[str, Any], path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"Unknown config path: {path}")
        current = current[part]
    return current


def apply_dotted_patch(
    base_config: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(base_config)
    for path, value in patch.items():
        current = get_dotted_path(base_config, path)
        validate_patch_value(path, current, value)
        target = result
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return result


def validate_patch_value(path: str, current: Any, value: Any) -> None:
    if current is None:
        valid = value is None
    elif isinstance(current, bool):
        valid = isinstance(value, bool)
    elif isinstance(current, int) and not isinstance(current, bool):
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(current, float):
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    elif isinstance(current, str):
        valid = isinstance(value, str)
    elif isinstance(current, list):
        valid = isinstance(value, list)
    elif isinstance(current, dict):
        valid = isinstance(value, dict)
    else:
        valid = isinstance(value, type(current))
    if not valid:
        raise ValueError(
            f"Type mismatch for {path}: expected {type(current).__name__}, "
            f"got {type(value).__name__}"
        )


def validate_config_constraints(config: dict[str, Any]) -> None:
    csp_cfg = config.get("csp_selector", {})
    cc_cfg = config.get("cc_selector", {})
    execution_cfg = config.get("execution", {})
    portfolio_cfg = config.get("portfolio", {})
    management_cfg = config.get("management", {})
    _validate_min_lte_max(
        "csp_selector.dte_min",
        csp_cfg.get("dte_min"),
        "csp_selector.dte_max",
        csp_cfg.get("dte_max"),
    )
    _validate_min_lte_max(
        "cc_selector.dte_min",
        cc_cfg.get("dte_min"),
        "cc_selector.dte_max",
        cc_cfg.get("dte_max"),
    )
    _validate_min_lte_max(
        "cc_selector.target_delta_min",
        cc_cfg.get("target_delta_min"),
        "cc_selector.target_delta_max",
        cc_cfg.get("target_delta_max"),
    )
    for name, policy in (csp_cfg.get("delta_policy") or {}).items():
        if isinstance(policy, dict):
            _validate_min_lte_max(
                f"csp_selector.delta_policy.{name}.target_delta_min",
                policy.get("target_delta_min"),
                f"csp_selector.delta_policy.{name}.target_delta_max",
                policy.get("target_delta_max"),
            )
    _validate_positive_int(
        "execution.max_orders_per_run",
        execution_cfg.get("max_orders_per_run"),
    )
    _validate_positive_int(
        "portfolio.max_active_tickers",
        portfolio_cfg.get("max_active_tickers"),
    )
    _validate_non_negative(
        "csp_selector.min_strike_distance_pct",
        csp_cfg.get("min_strike_distance_pct"),
    )
    _validate_non_negative(
        "csp_selector.min_strike_distance_atr_multiple",
        csp_cfg.get("min_strike_distance_atr_multiple"),
    )
    for path, model in (
        ("management.csp_exit_model", management_cfg.get("csp_exit_model")),
        ("management.cc_exit_model", management_cfg.get("cc_exit_model")),
    ):
        if model is not None and model not in {
            "hold_to_expiration",
            "close_at_50pct_profit_or_expiry",
            "manage_at_dte_or_expiry",
            "close_at_50pct_profit_or_manage_dte_or_expiry",
        }:
            raise ValueError(f"{path} has unsupported exit model: {model}")


def _validate_min_lte_max(
    min_name: str,
    min_value: Any,
    max_name: str,
    max_value: Any,
) -> None:
    if min_value is None or max_value is None:
        return
    if float(min_value) > float(max_value):
        raise ValueError(f"{min_name} must be <= {max_name}")


def _validate_positive_int(name: str, value: Any) -> None:
    if value is None:
        return
    if int(value) < 1:
        raise ValueError(f"{name} must be at least 1")


def _validate_non_negative(name: str, value: Any) -> None:
    if value is None:
        return
    if float(value) < 0:
        raise ValueError(f"{name} cannot be negative")


def compute_resource_plan(
    *,
    total_runs: int,
    requested_workers: str,
    memory_per_worker_gb: float,
    reserve_memory_gb: float,
    configured_max_workers: int,
    cpu_reserve: int,
    cpu_count: int | None = None,
    available_memory_gb: float | None = None,
) -> ResourcePlan:
    if configured_max_workers < 1:
        raise ValueError("configured_max_workers must be at least 1")
    if memory_per_worker_gb <= 0:
        raise ValueError("memory_per_worker_gb must be positive")
    if reserve_memory_gb < 0:
        raise ValueError("reserve_memory_gb cannot be negative")
    if cpu_reserve < 0:
        raise ValueError("cpu_reserve cannot be negative")
    cpu_count = cpu_count or os.cpu_count() or 1
    if total_runs <= 0:
        workers = 0
    elif requested_workers != "auto":
        workers = max(1, min(int(requested_workers), configured_max_workers, total_runs))
    else:
        available_memory_gb = (
            detect_available_memory_gb()
            if available_memory_gb is None
            else available_memory_gb
        )
        cpu_limit = max(cpu_count - cpu_reserve, 1)
        if available_memory_gb is None:
            memory_limit = min(cpu_limit, 3)
        else:
            usable = max(available_memory_gb - reserve_memory_gb, 0.0)
            memory_limit = max(int(math.floor(usable / memory_per_worker_gb)), 1)
        workers = min(cpu_limit, memory_limit, configured_max_workers, total_runs)
    return ResourcePlan(
        workers=workers,
        requested_workers=requested_workers,
        cpu_count=cpu_count,
        cpu_reserve=cpu_reserve,
        available_memory_gb=available_memory_gb,
        reserve_memory_gb=reserve_memory_gb,
        memory_per_worker_gb=memory_per_worker_gb,
        configured_max_workers=configured_max_workers,
        total_runs=total_runs,
    )


def detect_available_memory_gb() -> float | None:
    try:
        import psutil  # type: ignore

        return psutil.virtual_memory().available / (1024**3)
    except Exception:
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return pages * page_size / (1024**3)
    except (AttributeError, OSError, ValueError):
        return None


def run_sensitivity(
    *,
    specs: list[SensitivityRunSpec],
    output_dir: Path,
    cache_dir: Path,
    raw_cache_dir: Path | None = None,
    indexed_cache_dir: Path | None = None,
    require_warm_cache: bool = False,
    resource_plan: ResourcePlan,
    resume: bool,
    rerun_failed: bool,
    skip_cache_preflight: bool,
    run_options: dict[str, Any],
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = output_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    if not skip_cache_preflight:
        FlatFilesStore(
            cache_dir=cache_dir,
            raw_cache_dir=raw_cache_dir,
            indexed_cache_dir=indexed_cache_dir,
            require_warm_cache=require_warm_cache,
        ).require_writable_cache()
    results: list[dict[str, Any]] = []
    planned = _filter_specs_for_resume(
        specs,
        runs_dir=runs_dir,
        resume=resume,
        rerun_failed=rerun_failed,
        results=results,
    )
    write_progress(output_dir, specs, results, running=[])
    if not planned:
        write_aggregate_outputs(output_dir)
        return results

    baseline_specs = [spec for spec in planned if spec.is_baseline]
    variant_specs = [spec for spec in planned if not spec.is_baseline]
    if baseline_specs:
        result = _run_specs_parallel(
            baseline_specs,
            output_dir=output_dir,
            cache_dir=cache_dir,
            raw_cache_dir=raw_cache_dir,
            indexed_cache_dir=indexed_cache_dir,
            require_warm_cache=require_warm_cache,
            workers=1,
            skip_cache_preflight=True,
            run_options=run_options,
            all_specs=specs,
            existing_results=results,
        )
        results.extend(result)
    if variant_specs:
        result = _run_specs_parallel(
            variant_specs,
            output_dir=output_dir,
            cache_dir=cache_dir,
            raw_cache_dir=raw_cache_dir,
            indexed_cache_dir=indexed_cache_dir,
            require_warm_cache=require_warm_cache,
            workers=resource_plan.workers,
            skip_cache_preflight=True,
            run_options=run_options,
            all_specs=specs,
            existing_results=results,
        )
        results.extend(result)
    write_aggregate_outputs(output_dir)
    return results


def execute_run_worker(payload: dict[str, Any]) -> dict[str, Any]:
    spec = SensitivityRunSpec(**payload["spec"])
    output_dir = Path(payload["output_dir"])
    runs_dir = output_dir / "runs"
    final_dir = runs_dir / spec.run_id
    temp_dir = runs_dir / f".tmp_{spec.run_id}_{os.getpid()}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    try:
        metadata = _run_metadata(spec, status="running", started_at=started)
        write_json(temp_dir / "run_metadata.json", metadata)
        write_json(temp_dir / "parameter_patch.json", spec.parameter_patch)
        raw_cache_dir = payload.get("raw_cache_dir")
        indexed_cache_dir = payload.get("indexed_cache_dir")
        store = FlatFilesStore(
            cache_dir=Path(payload["cache_dir"]),
            raw_cache_dir=Path(raw_cache_dir) if raw_cache_dir else None,
            indexed_cache_dir=Path(indexed_cache_dir) if indexed_cache_dir else None,
            require_warm_cache=bool(payload.get("require_warm_cache")),
        )
        if not payload["skip_cache_preflight"]:
            store.require_writable_cache()
        result = run_backtest(
            config=spec.config,
            data=store,
            universe=spec.universe,
            start=datetime.fromisoformat(spec.start).date(),
            end=datetime.fromisoformat(spec.end).date(),
            **payload["run_options"],
        )
        write_backtest_outputs(result, temp_dir)
        completed = datetime.now(timezone.utc)
        metadata = _run_metadata(
            spec,
            status="completed",
            started_at=started,
            completed_at=completed,
            elapsed_seconds=(completed - started).total_seconds(),
        )
        write_json(temp_dir / "run_metadata.json", metadata)
        if final_dir.exists():
            quarantine_run_dir(final_dir)
        temp_dir.rename(final_dir)
        return {
            "run_id": spec.run_id,
            "status": "completed",
            "run_dir": str(final_dir),
            "summary": result.get("summary", {}),
        }
    except Exception as exc:
        completed = datetime.now(timezone.utc)
        metadata = _run_metadata(
            spec,
            status="failed",
            started_at=started,
            completed_at=completed,
            elapsed_seconds=(completed - started).total_seconds(),
            error=repr(exc),
            traceback_text=traceback.format_exc(),
        )
        write_json(temp_dir / "run_metadata.json", metadata)
        failure_dir = _move_failed_temp_dir(temp_dir, final_dir)
        return {
            "run_id": spec.run_id,
            "status": "failed",
            "run_dir": str(failure_dir),
            "error": repr(exc),
        }


def write_aggregate_outputs(output_dir: Path) -> list[dict[str, Any]]:
    rows = load_leaderboard_rows(output_dir)
    baseline = next((row for row in rows if row.get("is_baseline")), None)
    if baseline:
        for row in rows:
            _add_baseline_deltas(row, baseline)
    add_candidate_supply_scores(rows)
    rows.sort(
        key=leaderboard_sort_key
    )
    write_json(output_dir / "leaderboard.json", rows)
    write_leaderboard_csv(output_dir / "leaderboard.csv", rows)
    write_text_atomic(output_dir / "report.md", render_sensitivity_report(rows))
    return rows


def load_leaderboard_rows(output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in sorted((output_dir / "runs").glob("*")):
        if not run_dir.is_dir() or run_dir.name.startswith(".tmp_"):
            continue
        if run_directory_state(run_dir) != "completed":
            continue
        metadata = read_json(run_dir / "run_metadata.json")
        summary = read_json(run_dir / "summary.json")
        patch = read_json(run_dir / "parameter_patch.json")
        rows.append(flatten_run_metrics(metadata=metadata, summary=summary, patch=patch))
    return rows


def flatten_run_metrics(
    *,
    metadata: dict[str, Any],
    summary: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    opened_csp = int(summary.get("opened_short_puts") or 0)
    opened_cc = int(summary.get("opened_covered_calls") or 0)
    drawdown = abs(float(summary.get("max_drawdown_pct") or 0.0))
    total_return = float(summary.get("total_return_pct") or 0.0)
    support_diagnostics = summary.get("support_diagnostics") or {}
    market_regime_diagnostics = summary.get("market_regime_diagnostics") or {}
    candidate_ledger_diagnostics = summary.get("candidate_ledger_diagnostics") or {}
    scan_days = candidate_ledger_diagnostics.get("scan_days")
    data_issue_count = summary.get("data_issue_count")
    support_attempts = support_diagnostics.get("attempts")
    row: dict[str, Any] = {
        "run_id": metadata["run_id"],
        "name": metadata.get("name"),
        "experiment_name": metadata.get("experiment_name"),
        "is_baseline": bool(metadata.get("is_baseline")),
        "status": metadata.get("status"),
        "parameter_patch": patch,
        "total_return_pct": total_return,
        "ending_equity": summary.get("ending_equity"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "return_per_drawdown": return_per_drawdown(total_return, drawdown),
        "realized_option_pnl": summary.get("realized_option_pnl"),
        "realized_stock_pnl": summary.get("realized_stock_pnl"),
        "opened_short_puts": opened_csp,
        "assigned": summary.get("assigned"),
        "expired_worthless": summary.get("expired_worthless"),
        "closed_short_puts": summary.get("closed_short_puts"),
        "csp_profit_target_closes": summary.get("csp_profit_target_closes"),
        "csp_manage_dte_closes": summary.get("csp_manage_dte_closes"),
        "opened_covered_calls": opened_cc,
        "called_away": summary.get("called_away"),
        "closed_covered_calls": summary.get("closed_covered_calls"),
        "cc_profit_target_closes": summary.get("cc_profit_target_closes"),
        "cc_manage_dte_closes": summary.get("cc_manage_dte_closes"),
        "assignment_rate_pct": (
            round(float(summary.get("assigned") or 0) / opened_csp * 100.0, 4)
            if opened_csp
            else None
        ),
        "csp_expired_worthless_rate_pct": summary.get(
            "csp_expired_worthless_rate_pct"
        ),
        "csp_assignment_rate_pct": summary.get("csp_assignment_rate_pct"),
        "csp_realized_option_pnl_per_trade": summary.get(
            "csp_realized_option_pnl_per_trade"
        ),
        "called_away_rate_pct": (
            round(float(summary.get("called_away") or 0) / opened_cc * 100.0, 4)
            if opened_cc
            else None
        ),
        "average_capital_utilization_pct": summary.get("average_capital_utilization_pct"),
        "max_capital_utilization_pct": summary.get("max_capital_utilization_pct"),
        "data_issue_count": data_issue_count,
        "data_issues_per_100_scan_days": (
            summary.get("data_issues_per_100_scan_days")
            if summary.get("data_issues_per_100_scan_days") is not None
            else _rate_per_100(data_issue_count, scan_days)
        ),
        "data_issue_rate_pct_of_support_attempts": (
            summary.get("data_issue_rate_pct_of_support_attempts")
            if summary.get("data_issue_rate_pct_of_support_attempts") is not None
            else _rate_pct(data_issue_count, support_attempts)
        ),
        "fundamental_profile": summary.get("fundamental_profile"),
        "cc_risk_profile": summary.get("cc_risk_profile"),
        "csp_exit_model": summary.get("csp_exit_model"),
        "cc_exit_model": summary.get("cc_exit_model"),
        "profit_take_pct_of_credit": summary.get("profit_take_pct_of_credit"),
        "profit_take_price_field": summary.get("profit_take_price_field"),
        "manage_at_dte": summary.get("manage_at_dte"),
        "candidate_ledger_rows": candidate_ledger_diagnostics.get("rows"),
        "candidate_selection_stage_counts": candidate_ledger_diagnostics.get(
            "selection_stage_counts"
        ),
        "candidate_binding_filter_counts": candidate_ledger_diagnostics.get(
            "binding_filter_counts"
        ),
        "candidate_top_binding_filters": candidate_ledger_diagnostics.get(
            "top_binding_filters"
        ),
        "mechanical_candidate_count": candidate_ledger_diagnostics.get(
            "mechanical_candidate_count"
        ),
        "mechanical_auto_trade_candidate_count": candidate_ledger_diagnostics.get(
            "mechanical_auto_trade_candidate_count"
        ),
        "quality_candidate_filter": candidate_ledger_diagnostics.get(
            "quality_candidate_filter"
        ),
        "quality_candidate_count": candidate_ledger_diagnostics.get(
            "quality_candidate_count"
        ),
        "quality_candidate_pass_rate_pct": candidate_ledger_diagnostics.get(
            "quality_candidate_pass_rate_pct"
        ),
        "average_daily_quality_candidate_pass_rate_pct": (
            candidate_ledger_diagnostics.get(
                "average_daily_quality_candidate_pass_rate_pct"
            )
        ),
        "quality_candidate_days": candidate_ledger_diagnostics.get(
            "quality_candidate_days"
        ),
        "median_quality_candidates_per_day": candidate_ledger_diagnostics.get(
            "median_quality_candidates_per_day"
        ),
        "average_quality_candidates_per_day": candidate_ledger_diagnostics.get(
            "average_quality_candidates_per_day"
        ),
        "median_unique_quality_candidate_tickers_per_day": (
            candidate_ledger_diagnostics.get(
                "median_unique_quality_candidate_tickers_per_day"
            )
        ),
        "average_unique_quality_candidate_tickers_per_day": (
            candidate_ledger_diagnostics.get(
                "average_unique_quality_candidate_tickers_per_day"
            )
        ),
        "pct_days_with_3plus_quality_candidates": candidate_ledger_diagnostics.get(
            "pct_days_with_3plus_quality_candidates"
        ),
        "pct_days_with_5plus_quality_candidates": candidate_ledger_diagnostics.get(
            "pct_days_with_5plus_quality_candidates"
        ),
        "pct_days_with_3plus_unique_quality_tickers": (
            candidate_ledger_diagnostics.get(
                "pct_days_with_3plus_unique_quality_tickers"
            )
        ),
        "pct_days_with_5plus_unique_quality_tickers": (
            candidate_ledger_diagnostics.get(
                "pct_days_with_5plus_unique_quality_tickers"
            )
        ),
        "watch_only_candidate_count": candidate_ledger_diagnostics.get(
            "watch_only_candidate_count"
        ),
        "selected_for_backtest_count": candidate_ledger_diagnostics.get(
            "selected_for_backtest_count"
        ),
        "candidate_days": candidate_ledger_diagnostics.get("candidate_days"),
        "auto_trade_candidate_days": candidate_ledger_diagnostics.get(
            "auto_trade_candidate_days"
        ),
        "scan_days": candidate_ledger_diagnostics.get("scan_days"),
        "starvation_days": candidate_ledger_diagnostics.get("starvation_days"),
        "median_candidates_per_day": candidate_ledger_diagnostics.get(
            "median_candidates_per_day"
        ),
        "average_candidates_per_day": candidate_ledger_diagnostics.get(
            "average_candidates_per_day"
        ),
        "median_unique_candidate_tickers_per_day": candidate_ledger_diagnostics.get(
            "median_unique_candidate_tickers_per_day"
        ),
        "average_unique_candidate_tickers_per_day": candidate_ledger_diagnostics.get(
            "average_unique_candidate_tickers_per_day"
        ),
        "average_unique_candidate_ticker_expirations_per_day": (
            candidate_ledger_diagnostics.get(
                "average_unique_candidate_ticker_expirations_per_day"
            )
        ),
        "pct_days_with_3plus_candidates": candidate_ledger_diagnostics.get(
            "pct_days_with_3plus_candidates"
        ),
        "pct_days_with_5plus_candidates": candidate_ledger_diagnostics.get(
            "pct_days_with_5plus_candidates"
        ),
        "pct_days_with_3plus_unique_tickers": candidate_ledger_diagnostics.get(
            "pct_days_with_3plus_unique_tickers"
        ),
        "pct_days_with_5plus_unique_tickers": candidate_ledger_diagnostics.get(
            "pct_days_with_5plus_unique_tickers"
        ),
        "pct_weeks_with_15plus_candidates": candidate_ledger_diagnostics.get(
            "pct_weeks_with_15plus_candidates"
        ),
        "average_unique_candidate_tickers_per_week": candidate_ledger_diagnostics.get(
            "average_unique_candidate_tickers_per_week"
        ),
        "mechanical_candidates_per_week": candidate_ledger_diagnostics.get(
            "mechanical_candidates_per_week"
        ),
        "mechanical_auto_trade_candidates_per_week": candidate_ledger_diagnostics.get(
            "mechanical_auto_trade_candidates_per_week"
        ),
        "opened_from_candidate_per_week": candidate_ledger_diagnostics.get(
            "opened_from_candidate_per_week"
        ),
        "candidate_to_trade_ratio_pct": candidate_ledger_diagnostics.get(
            "candidate_to_trade_ratio_pct"
        ),
        "auto_candidate_to_trade_ratio_pct": candidate_ledger_diagnostics.get(
            "auto_candidate_to_trade_ratio_pct"
        ),
        "market_regime_reject_day_pct": market_regime_diagnostics.get(
            "reject_day_pct"
        ),
        "market_regime_reason_counts": market_regime_diagnostics.get(
            "reason_counts"
        ),
        "support_attempts": support_diagnostics.get("attempts"),
        "support_coverage_pct": support_diagnostics.get("support_coverage_pct"),
        "support_tradable_pct": support_diagnostics.get("tradable_pct"),
        "support_candidate_found_pct": support_diagnostics.get("candidate_found_pct"),
        "support_auto_trade_candidate_pct": support_diagnostics.get(
            "auto_trade_candidate_pct"
        ),
        "support_average_zone_count": support_diagnostics.get("average_zone_count"),
        "support_average_selected_score": support_diagnostics.get(
            "average_selected_score"
        ),
        "support_average_spot_to_bottom_pct": support_diagnostics.get(
            "average_spot_to_support_bottom_pct"
        ),
        "support_average_spot_to_top_pct": support_diagnostics.get(
            "average_spot_to_support_top_pct"
        ),
        "support_average_spot_to_bottom_atr": support_diagnostics.get(
            "average_spot_to_support_bottom_atr"
        ),
        "support_selected_method_counts": support_diagnostics.get(
            "selected_method_counts"
        ),
        "uncovered_assigned_days": summary.get("uncovered_assigned_days"),
        "uncovered_assigned_share_days": summary.get("uncovered_assigned_share_days"),
        "average_uncovered_assigned_shares": summary.get(
            "average_uncovered_assigned_shares"
        ),
        "max_uncovered_assigned_shares": summary.get("max_uncovered_assigned_shares"),
        "cc_realized_option_pnl": summary.get("cc_realized_option_pnl"),
        "cc_called_away_stock_pnl": summary.get("cc_called_away_stock_pnl"),
        "open_assigned_stock_unrealized_pnl": summary.get(
            "open_assigned_stock_unrealized_pnl"
        ),
        "assigned_stock_recovery_pnl_estimate": summary.get(
            "assigned_stock_recovery_pnl_estimate"
        ),
        "execution_model": summary.get("execution_model"),
        "execution_fill_policy": summary.get("execution_fill_policy"),
        "execution_reference_price_source": summary.get(
            "execution_reference_price_source"
        ),
        "execution_calibration_status": summary.get("execution_calibration_status"),
        "average_entry_spread_pct_of_mid": summary.get(
            "average_entry_spread_pct_of_mid"
        ),
        "average_entry_fill_discount_pct_of_mid": summary.get(
            "average_entry_fill_discount_pct_of_mid"
        ),
        "sample_size_flag": opened_csp < 30,
    }
    for reason, count in (summary.get("rejected_reason_counts") or {}).items():
        row[f"reject_{reason}"] = count
    return row


def _rate_pct(numerator: Any, denominator: Any) -> float | None:
    numerator_value = _finite_float_or_none(numerator)
    denominator_value = _finite_float_or_none(denominator)
    if numerator_value is None or denominator_value is None or denominator_value <= 0:
        return None
    return round(numerator_value / denominator_value * 100.0, 4)


def _rate_per_100(numerator: Any, denominator: Any) -> float | None:
    return _rate_pct(numerator, denominator)


def return_per_drawdown(total_return: float, drawdown: float) -> float:
    if drawdown > 0:
        return round(total_return / drawdown, 6)
    if total_return > 0:
        return 1_000_000.0
    if total_return < 0:
        return -1_000_000.0
    return 0.0


def leaderboard_sort_key(row: dict[str, Any]) -> tuple[bool, float, float]:
    supply_score = _ranking_supply_score(row)
    supply_score_value = (
        -1_000_000.0 if supply_score is None else float(supply_score)
    )
    return_per_dd = row.get("return_per_drawdown")
    total_return = row.get("total_return_pct")
    return_per_dd_score = (
        -1_000_000.0 if return_per_dd is None else float(return_per_dd)
    )
    total_return_score = (
        -1_000_000.0 if total_return is None else float(total_return)
    )
    return (
        not row.get("is_baseline", False),
        -supply_score_value,
        -return_per_dd_score,
        -total_return_score,
    )


def _ranking_supply_score(row: dict[str, Any]) -> Any:
    return (
        row.get("candidate_supply_score_v2")
        if row.get("candidate_supply_score_v2") is not None
        else row.get("candidate_supply_score_v1")
    )


def add_candidate_supply_scores(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    metric_names = [
        "pct_days_with_3plus_candidates",
        "median_candidates_per_day",
        "average_candidates_per_day",
        "total_return_pct",
        "opened_short_puts",
        "max_drawdown_abs",
        "starvation_days",
        "csp_assignment_rate_pct",
    ]
    metric_values: dict[str, list[float | None]] = {name: [] for name in metric_names}
    for row in rows:
        row["max_drawdown_abs"] = abs(float(row.get("max_drawdown_pct") or 0.0))
        for name in metric_names:
            metric_values[name].append(_finite_float_or_none(row.get(name)))
    zscores = {
        name: _zscore_series(values) for name, values in metric_values.items()
    }
    for index, row in enumerate(rows):
        raw_score = (
            0.70 * zscores["pct_days_with_3plus_candidates"][index]
            + 0.50 * zscores["median_candidates_per_day"][index]
            + 0.40 * zscores["average_candidates_per_day"][index]
            + 0.40 * zscores["total_return_pct"][index]
            + 0.20 * zscores["opened_short_puts"][index]
            - 0.30 * zscores["max_drawdown_abs"][index]
            - 0.30 * zscores["starvation_days"][index]
            - 0.20 * zscores["csp_assignment_rate_pct"][index]
        )
        penalties = _candidate_supply_penalties(row)
        row["candidate_supply_score_v1_raw"] = round(raw_score, 6)
        row["candidate_supply_score_v1_penalties"] = penalties
        row["candidate_supply_score_v1_warnings"] = _candidate_supply_warnings(row)
        row["candidate_supply_score_v1_eligible"] = not penalties
        row["candidate_supply_score_v1"] = (
            -1_000_000.0 if penalties else round(raw_score, 6)
        )
    _add_candidate_supply_score_v2(rows)


def _add_candidate_supply_score_v2(rows: list[dict[str, Any]]) -> None:
    metric_names = [
        "pct_days_with_3plus_quality_candidates",
        "median_quality_candidates_per_day",
        "average_quality_candidates_per_day",
        "pct_days_with_3plus_candidates",
        "total_return_pct",
        "opened_short_puts",
        "max_drawdown_abs",
        "starvation_days",
        "csp_assignment_rate_pct",
    ]
    metric_values: dict[str, list[float | None]] = {name: [] for name in metric_names}
    for row in rows:
        for name in metric_names:
            metric_values[name].append(_finite_float_or_none(row.get(name)))
    zscores = {
        name: _zscore_series(values) for name, values in metric_values.items()
    }
    for index, row in enumerate(rows):
        raw_score = (
            0.70 * zscores["pct_days_with_3plus_quality_candidates"][index]
            + 0.50 * zscores["median_quality_candidates_per_day"][index]
            + 0.30 * zscores["average_quality_candidates_per_day"][index]
            + 0.25 * zscores["pct_days_with_3plus_candidates"][index]
            + 0.35 * zscores["total_return_pct"][index]
            + 0.15 * zscores["opened_short_puts"][index]
            - 0.30 * zscores["max_drawdown_abs"][index]
            - 0.25 * zscores["starvation_days"][index]
            - 0.20 * zscores["csp_assignment_rate_pct"][index]
        )
        penalties = _candidate_supply_penalties(row)
        row["candidate_supply_score_v2_raw"] = round(raw_score, 6)
        row["candidate_supply_score_v2_penalties"] = penalties
        row["candidate_supply_score_v2_warnings"] = _candidate_supply_warnings(row)
        row["candidate_supply_score_v2_eligible"] = not penalties
        row["candidate_supply_score_v2"] = (
            -1_000_000.0 if penalties else round(raw_score, 6)
        )


def _finite_float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _zscore_series(values: list[float | None]) -> list[float]:
    finite = [value for value in values if value is not None]
    if not finite:
        return [0.0 for _ in values]
    mean = sum(finite) / len(finite)
    variance = sum((value - mean) ** 2 for value in finite) / len(finite)
    stddev = math.sqrt(variance)
    if stddev == 0:
        return [0.0 for _ in values]
    return [0.0 if value is None else (value - mean) / stddev for value in values]


def _candidate_supply_penalties(row: dict[str, Any]) -> list[str]:
    penalties: list[str] = []
    total_return = _finite_float_or_none(row.get("total_return_pct"))
    max_drawdown_abs = _finite_float_or_none(row.get("max_drawdown_abs"))
    opened_short_puts = _finite_float_or_none(row.get("opened_short_puts"))
    data_issue_rate = _finite_float_or_none(
        row.get("data_issue_rate_pct_of_support_attempts")
    )
    data_issues_per_100_scan_days = _finite_float_or_none(
        row.get("data_issues_per_100_scan_days")
    )
    starvation_days = _finite_float_or_none(row.get("starvation_days"))
    scan_days = _finite_float_or_none(row.get("scan_days"))
    if (
        total_return is not None
        and total_return < -0.25
        and (opened_short_puts is None or opened_short_puts >= 30)
    ):
        penalties.append("negative_return_lt_-0.25pct")
    elif total_return is not None and total_return < -1.0:
        penalties.append("negative_return_lt_-1pct_low_sample")
    if max_drawdown_abs is not None and max_drawdown_abs > 15.0:
        penalties.append("max_drawdown_gt_15pct")
    if data_issue_rate is not None:
        if data_issue_rate > 2.0:
            penalties.append("data_issue_rate_gt_2pct")
    elif (
        data_issues_per_100_scan_days is not None
        and data_issues_per_100_scan_days > 25.0
    ):
        penalties.append("data_issues_per_100_scan_days_gt_25")
    if (
        starvation_days is not None
        and scan_days is not None
        and scan_days > 0
        and starvation_days / scan_days > 0.60
    ):
        penalties.append("starvation_gt_60pct")
    return penalties


def _candidate_supply_warnings(row: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    total_return = _finite_float_or_none(row.get("total_return_pct"))
    opened_short_puts = _finite_float_or_none(row.get("opened_short_puts"))
    if total_return is not None and total_return < 0:
        if total_return >= -0.25:
            warnings.append("marginal_negative_return")
        elif opened_short_puts is not None and opened_short_puts < 30:
            warnings.append("negative_return_low_sample")
    return warnings


def run_directory_state(run_dir: Path) -> str:
    if not run_dir.exists():
        return "missing"
    metadata_path = run_dir / "run_metadata.json"
    if not metadata_path.exists():
        return "partial"
    try:
        metadata = read_json(metadata_path)
    except Exception:
        return "partial"
    status = metadata.get("status")
    if status == "completed":
        required = [
            "summary.json",
            "backtest_results.json",
            "run_metadata.json",
            "parameter_patch.json",
        ]
        return "completed" if all((run_dir / name).exists() for name in required) else "partial"
    if status == "failed":
        return "failed"
    return "partial"


def quarantine_run_dir(run_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = run_dir.parent / "_quarantine" / f"{run_dir.name}_{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if run_dir.exists():
        shutil.move(str(run_dir), str(target))
    return target


def write_progress(
    output_dir: Path,
    specs: list[SensitivityRunSpec],
    results: list[dict[str, Any]],
    *,
    running: list[str],
) -> None:
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status"))
        counts[status] = counts.get(status, 0) + 1
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_planned": len(specs),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped_resume", 0) + counts.get("skipped_failed", 0),
        "running": running,
        "last_completed_run": next(
            (result.get("run_id") for result in reversed(results) if result.get("status") == "completed"),
            None,
        ),
    }
    write_json(output_dir / "progress.json", payload)


def write_leaderboard_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        write_text_atomic(path, "")
        return
    fieldnames: list[str] = []
    preferred = [
        "run_id",
        "name",
        "experiment_name",
        "is_baseline",
        "total_return_pct",
        "max_drawdown_pct",
        "return_per_drawdown",
        "candidate_supply_score_v1",
        "candidate_supply_score_v1_raw",
        "candidate_supply_score_v1_eligible",
        "candidate_supply_score_v1_penalties",
        "candidate_supply_score_v1_warnings",
        "candidate_supply_score_v2",
        "candidate_supply_score_v2_raw",
        "candidate_supply_score_v2_eligible",
        "candidate_supply_score_v2_penalties",
        "candidate_supply_score_v2_warnings",
        "delta_total_return_pct",
        "opened_short_puts",
        "assigned",
        "expired_worthless",
        "closed_short_puts",
        "csp_profit_target_closes",
        "csp_manage_dte_closes",
        "assignment_rate_pct",
        "csp_expired_worthless_rate_pct",
        "csp_assignment_rate_pct",
        "csp_realized_option_pnl_per_trade",
        "opened_covered_calls",
        "called_away",
        "closed_covered_calls",
        "cc_profit_target_closes",
        "cc_manage_dte_closes",
        "called_away_rate_pct",
        "average_capital_utilization_pct",
        "max_capital_utilization_pct",
        "data_issue_count",
        "data_issues_per_100_scan_days",
        "data_issue_rate_pct_of_support_attempts",
        "fundamental_profile",
        "cc_risk_profile",
        "csp_exit_model",
        "cc_exit_model",
        "profit_take_pct_of_credit",
        "profit_take_price_field",
        "manage_at_dte",
        "candidate_ledger_rows",
        "candidate_selection_stage_counts",
        "candidate_binding_filter_counts",
        "candidate_top_binding_filters",
        "mechanical_candidate_count",
        "mechanical_auto_trade_candidate_count",
        "quality_candidate_count",
        "quality_candidate_pass_rate_pct",
        "average_daily_quality_candidate_pass_rate_pct",
        "quality_candidate_days",
        "median_quality_candidates_per_day",
        "average_quality_candidates_per_day",
        "median_unique_quality_candidate_tickers_per_day",
        "average_unique_quality_candidate_tickers_per_day",
        "pct_days_with_3plus_quality_candidates",
        "pct_days_with_5plus_quality_candidates",
        "pct_days_with_3plus_unique_quality_tickers",
        "pct_days_with_5plus_unique_quality_tickers",
        "quality_candidate_filter",
        "watch_only_candidate_count",
        "selected_for_backtest_count",
        "candidate_days",
        "auto_trade_candidate_days",
        "scan_days",
        "starvation_days",
        "median_candidates_per_day",
        "average_candidates_per_day",
        "median_unique_candidate_tickers_per_day",
        "average_unique_candidate_tickers_per_day",
        "average_unique_candidate_ticker_expirations_per_day",
        "pct_days_with_3plus_candidates",
        "pct_days_with_5plus_candidates",
        "pct_days_with_3plus_unique_tickers",
        "pct_days_with_5plus_unique_tickers",
        "pct_weeks_with_15plus_candidates",
        "average_unique_candidate_tickers_per_week",
        "mechanical_candidates_per_week",
        "mechanical_auto_trade_candidates_per_week",
        "opened_from_candidate_per_week",
        "candidate_to_trade_ratio_pct",
        "auto_candidate_to_trade_ratio_pct",
        "market_regime_reject_day_pct",
        "market_regime_reason_counts",
        "csp_regime_override",
        "support_attempts",
        "support_coverage_pct",
        "support_tradable_pct",
        "support_candidate_found_pct",
        "support_auto_trade_candidate_pct",
        "support_average_zone_count",
        "support_average_selected_score",
        "support_average_spot_to_bottom_pct",
        "support_average_spot_to_top_pct",
        "support_average_spot_to_bottom_atr",
        "support_selected_method_counts",
        "uncovered_assigned_days",
        "uncovered_assigned_share_days",
        "average_uncovered_assigned_shares",
        "max_uncovered_assigned_shares",
        "cc_realized_option_pnl",
        "cc_called_away_stock_pnl",
        "open_assigned_stock_unrealized_pnl",
        "assigned_stock_recovery_pnl_estimate",
        "execution_model",
        "execution_fill_policy",
        "execution_reference_price_source",
        "execution_calibration_status",
        "average_entry_spread_pct_of_mid",
        "average_entry_fill_discount_pct_of_mid",
        "sample_size_flag",
        "parameter_patch",
    ]
    for key in preferred:
        if any(key in row for row in rows):
            fieldnames.append(key)
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _atomic_temp_path(path)
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def render_sensitivity_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Backtest Sensitivity Report",
        "",
        f"- Runs completed: {len(rows)}",
        "- Ranking is diagnostic, not an automatic live-trading parameter choice.",
        "- Single-window results can overfit one market regime; validate on more windows before adopting.",
        "- Candidate supply score v2 prioritizes quality-filtered candidate supply, then positive return and risk controls.",
        "",
        "| Run | Experiment | Supply Score | Return | Max DD | Return/DD | Candidates/Day | Quality/Day | 3+ Quality Days | Starve | Top Binding Filter | CSPs | Assigned | CCs | Called Away | Avg Util | Avg Spread | Exec | Data Issues | Issue Rate | Sample |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|",
    ]
    for row in rows[:50]:
        sample = "LOW" if row.get("sample_size_flag") else "OK"
        lines.append(
            "| "
            f"{row.get('name')} | {row.get('experiment_name')} | "
            f"{_fmt(_ranking_supply_score(row))} | "
            f"{_fmt(row.get('total_return_pct'))}% | "
            f"{_fmt(row.get('max_drawdown_pct'))}% | "
            f"{_fmt(row.get('return_per_drawdown'))} | "
            f"{_fmt(row.get('average_candidates_per_day'))} | "
            f"{_fmt(row.get('average_quality_candidates_per_day'))} | "
            f"{_fmt(row.get('pct_days_with_3plus_quality_candidates'))}% | "
            f"{row.get('starvation_days')} | "
            f"{_top_binding_filter_label(row)} | "
            f"{row.get('opened_short_puts')} | {row.get('assigned')} | "
            f"{row.get('opened_covered_calls')} | {row.get('called_away')} | "
            f"{_fmt(row.get('average_capital_utilization_pct'))}% | "
            f"{_fmt(row.get('average_entry_spread_pct_of_mid'))} | "
            f"{row.get('execution_model') or ''}/{row.get('execution_fill_policy') or ''} | "
            f"{row.get('data_issue_count')} | "
            f"{_fmt(row.get('data_issue_rate_pct_of_support_attempts'))}% | "
            f"{sample} |"
        )
    return "\n".join(lines) + "\n"


def write_json(path: Path, payload: Any) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
    )


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _atomic_temp_path(path)
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def current_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def default_data_source_metadata() -> dict[str, Any]:
    return {
        "provider": "massive_flatfiles",
        "datasets": {
            "stocks": "us_stocks_sip/day_aggs_v1",
            "options": "us_options_opra/day_aggs_v1 with backtest execution model",
        },
    }


def stable_config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _make_run_spec(
    *,
    name: str,
    experiment_name: str,
    parameter_patch: dict[str, Any],
    config: dict[str, Any],
    start: str,
    end: str,
    universe: list[str],
    universe_name: str,
    is_baseline: bool,
    git_sha: str,
    backtest_version: str,
    data_source_metadata: dict[str, Any],
) -> SensitivityRunSpec:
    config_hash = stable_config_hash(config)
    run_payload = {
        "schema": SENSITIVITY_SCHEMA_VERSION,
        "config_hash": config_hash,
        "config": config,
        "start": start,
        "end": end,
        "universe": sorted(universe),
        "universe_name": universe_name,
        "backtest_version": backtest_version,
        "data_source_metadata": data_source_metadata,
    }
    digest = hashlib.sha256(canonical_json(run_payload).encode("utf-8")).hexdigest()[:14]
    return SensitivityRunSpec(
        run_id=f"sr_{digest}",
        name=name,
        experiment_name=experiment_name,
        parameter_patch=parameter_patch,
        config=config,
        start=start,
        end=end,
        universe=sorted(universe),
        universe_name=universe_name,
        is_baseline=is_baseline,
        config_hash=config_hash,
        backtest_version=backtest_version,
        git_sha=git_sha,
        data_source_metadata=data_source_metadata,
    )


def _run_metadata(
    spec: SensitivityRunSpec,
    *,
    status: str,
    started_at: datetime,
    completed_at: datetime | None = None,
    elapsed_seconds: float | None = None,
    error: str | None = None,
    traceback_text: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": SENSITIVITY_SCHEMA_VERSION,
        "run_id": spec.run_id,
        "name": spec.name,
        "experiment_name": spec.experiment_name,
        "is_baseline": spec.is_baseline,
        "status": status,
        "start": spec.start,
        "end": spec.end,
        "universe": spec.universe,
        "universe_name": spec.universe_name,
        "config_hash": spec.config_hash,
        "backtest_version": spec.backtest_version,
        "git_sha": spec.git_sha,
        "data_source_metadata": spec.data_source_metadata,
        "started_at": started_at.isoformat(),
    }
    if completed_at is not None:
        payload["completed_at"] = completed_at.isoformat()
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = elapsed_seconds
    if error is not None:
        payload["error"] = error
    if traceback_text is not None:
        payload["traceback"] = traceback_text
    return payload


def _filter_specs_for_resume(
    specs: list[SensitivityRunSpec],
    *,
    runs_dir: Path,
    resume: bool,
    rerun_failed: bool,
    results: list[dict[str, Any]],
) -> list[SensitivityRunSpec]:
    planned: list[SensitivityRunSpec] = []
    for spec in specs:
        run_dir = runs_dir / spec.run_id
        state = run_directory_state(run_dir)
        if resume and state == "completed":
            results.append({"run_id": spec.run_id, "status": "skipped_resume"})
            continue
        if resume and state == "failed" and not rerun_failed:
            results.append({"run_id": spec.run_id, "status": "skipped_failed"})
            continue
        if state in {"partial", "failed"}:
            quarantine_run_dir(run_dir)
        planned.append(spec)
    return planned


def _run_specs_parallel(
    specs: list[SensitivityRunSpec],
    *,
    output_dir: Path,
    cache_dir: Path,
    raw_cache_dir: Path | None,
    indexed_cache_dir: Path | None,
    require_warm_cache: bool,
    workers: int,
    skip_cache_preflight: bool,
    run_options: dict[str, Any],
    all_specs: list[SensitivityRunSpec],
    existing_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    workers = max(1, min(workers, len(specs)))
    aggregate_every = max(1, min(5, workers))
    completions_since_aggregate = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                execute_run_worker,
                {
                    "spec": asdict(spec),
                    "output_dir": str(output_dir),
                    "cache_dir": str(cache_dir),
                    "raw_cache_dir": str(raw_cache_dir) if raw_cache_dir else None,
                    "indexed_cache_dir": str(indexed_cache_dir)
                    if indexed_cache_dir
                    else None,
                    "require_warm_cache": require_warm_cache,
                    "skip_cache_preflight": skip_cache_preflight,
                    "run_options": run_options,
                },
            ): spec
            for spec in specs
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = _write_parent_observed_failure(
                    output_dir=output_dir,
                    spec=spec,
                    error=repr(exc),
                    traceback_text=traceback.format_exc(),
                )
            results.append(result)
            combined = existing_results + results
            running = [
                spec.run_id
                for pending_future, spec in futures.items()
                if not pending_future.done()
            ]
            write_progress(output_dir, all_specs, combined, running=running)
            if result.get("status") == "completed":
                completions_since_aggregate += 1
            if completions_since_aggregate >= aggregate_every or not running:
                write_aggregate_outputs(output_dir)
                completions_since_aggregate = 0
    return results


def _write_parent_observed_failure(
    *,
    output_dir: Path,
    spec: SensitivityRunSpec,
    error: str,
    traceback_text: str,
) -> dict[str, Any]:
    run_dir = output_dir / "runs" / spec.run_id
    if run_directory_state(run_dir) == "completed":
        run_dir = _failed_rerun_dir(run_dir)
    elif run_dir.exists():
        quarantine_run_dir(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    observed_at = datetime.now(timezone.utc)
    write_json(
        run_dir / "run_metadata.json",
        _run_metadata(
            spec,
            status="failed",
            started_at=observed_at,
            completed_at=observed_at,
            elapsed_seconds=0.0,
            error=error,
            traceback_text=traceback_text,
        ),
    )
    write_json(run_dir / "parameter_patch.json", spec.parameter_patch)
    return {
        "run_id": spec.run_id,
        "status": "failed",
        "run_dir": str(run_dir),
        "error": error,
    }


def _move_failed_temp_dir(temp_dir: Path, final_dir: Path) -> Path:
    if run_directory_state(final_dir) == "completed":
        failure_dir = _failed_rerun_dir(final_dir)
        failure_dir.parent.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(failure_dir)
        return failure_dir
    if final_dir.exists():
        quarantine_run_dir(final_dir)
    temp_dir.rename(final_dir)
    return final_dir


def _failed_rerun_dir(final_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return (
        final_dir.parent
        / "_failed_reruns"
        / f"{final_dir.name}_{stamp}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    )


def _add_baseline_deltas(row: dict[str, Any], baseline: dict[str, Any]) -> None:
    for key in (
        "total_return_pct",
        "max_drawdown_pct",
        "opened_short_puts",
        "assigned",
        "opened_covered_calls",
        "called_away",
        "average_capital_utilization_pct",
        "max_capital_utilization_pct",
    ):
        if isinstance(row.get(key), (int, float)) and isinstance(baseline.get(key), (int, float)):
            row[f"delta_{key}"] = round(float(row[key]) - float(baseline[key]), 6)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return value


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _top_binding_filter_label(row: dict[str, Any]) -> str:
    filters = row.get("candidate_top_binding_filters")
    if not isinstance(filters, list) or not filters:
        return "-"
    first = filters[0]
    if not isinstance(first, dict):
        return "-"
    reason = first.get("reason")
    count = first.get("count")
    return f"{reason} ({count})" if reason else "-"
