from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .execution import execute_validated_shadow_orders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Submit validated wheel shadow orders to Alpaca paper"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to validated_shadow_orders.json",
    )
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument(
        "--output",
        type=Path,
        help="Defaults to execution_results.json next to the input file.",
    )
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    validated = json.loads(args.input.read_text(encoding="utf-8"))
    output = args.output or args.input.with_name("execution_results.json")
    previous = None
    if output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))

    result = execute_validated_shadow_orders(
        validated,
        cfg,
        previous_execution_results=previous,
    )
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.json_stdout:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"execution_results: {output}")
        print(f"Summary: {result['summary']}")
    return 0
