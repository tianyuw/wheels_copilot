from __future__ import annotations

import argparse
import json
from pathlib import Path

from .alpaca import AlpacaConfigError, AlpacaRequestError
from .config import load_config
from .oms import reconcile_orders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile Wheels Copilot OMS orders and positions against Alpaca paper"
    )
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON file for reconciliation results.",
    )
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    try:
        result = reconcile_orders(cfg)
    except (AlpacaConfigError, AlpacaRequestError) as exc:
        result = {
            "summary": {"ERROR": 1},
            "error": f"{type(exc).__name__}: {exc}",
            "orders": [],
        }
        if args.json_stdout:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"Error: {result['error']}")
        return 1
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if args.json_stdout:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if args.output:
            print(f"reconciliation_results: {args.output}")
        print(f"Summary: {result['summary']}")
    return 1 if result.get("errors") else 0
