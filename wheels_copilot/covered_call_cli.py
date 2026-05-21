from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from .alpaca import AlpacaConfigError, AlpacaRequestError, fetch_alpaca_portfolio_snapshot
from .config import load_config
from .covered_call_planner import (
    build_covered_call_proposals,
    build_covered_call_shadow_orders,
    render_covered_call_report,
)
from .oms import OrderLedger, oms_enabled
from .scan import resolve_output_dir
from .wheel_lifecycle import build_wheel_lifecycle_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build dry-run covered-call proposals for assigned wheel positions"
    )
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument(
        "--date",
        dest="scan_date",
        help="Planner date in YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory. Defaults to workspace/covered_calls/YYYY-MM-DD.",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    as_of = date.fromisoformat(args.scan_date) if args.scan_date else date.today()
    base_output = args.output or Path("workspace/covered_calls") / as_of.isoformat()
    output_dir = resolve_output_dir(base_output, overwrite=args.overwrite)
    try:
        portfolio = fetch_alpaca_portfolio_snapshot(cfg)
        ledger_positions = _ledger_positions(cfg)
        lifecycle = build_wheel_lifecycle_snapshot(
            portfolio,
            cfg,
            ledger_positions=ledger_positions,
            as_of=as_of,
        )
        proposals = build_covered_call_proposals(lifecycle, cfg, as_of=as_of)
        shadow_orders = build_covered_call_shadow_orders(proposals, cfg)
    except (AlpacaConfigError, AlpacaRequestError) as exc:
        result = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": {"ERROR": 1},
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.json_stdout:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"Error: {result['error']}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "lifecycle": output_dir / "wheel_lifecycle.json",
        "covered_call_proposals": output_dir / "covered_call_proposals.json",
        "covered_call_shadow_orders": output_dir / "covered_call_shadow_orders.json",
        "markdown": output_dir / "covered_call_report.md",
    }
    paths["lifecycle"].write_text(
        json.dumps(lifecycle, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    paths["covered_call_proposals"].write_text(
        json.dumps(proposals, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    paths["covered_call_shadow_orders"].write_text(
        json.dumps(shadow_orders, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    paths["markdown"].write_text(
        render_covered_call_report(lifecycle, proposals),
        encoding="utf-8",
    )
    if args.json_stdout:
        print(json.dumps(proposals, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"Output directory: {output_dir}")
        for name, path in paths.items():
            print(f"{name}: {path}")
        print(f"Lifecycle summary: {lifecycle['summary']}")
        print(f"Proposal summary: {proposals['summary']}")
        print(f"Shadow orders: {shadow_orders['order_count']}")
    return 0


def _ledger_positions(config: dict) -> list:
    if not oms_enabled(config):
        return []
    ledger = OrderLedger.from_config(config)
    try:
        return ledger.list_positions()
    finally:
        ledger.close()
