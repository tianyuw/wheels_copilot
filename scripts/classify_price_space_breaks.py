#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wheels_copilot.price_space_breaks import (
    DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR,
    build_price_space_break_classifier,
    classify_price_space_issues,
    expanded_date_bounds,
    issue_date_bounds,
    summarize_classifications,
)


def main() -> int:
    args = parse_args()
    backtest = read_json(Path(args.backtest_results))
    issues = [
        issue
        for issue in backtest.get("data_issues", [])
        if isinstance(issue, dict) and issue.get("type") == "price_space_break"
    ]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    bounds = issue_date_bounds(issues)
    if bounds is None:
        summary = {"count": 0, "category_counts": {}, "action_counts": {}}
        write_json(output_dir / "price_space_break_classification_summary.json", summary)
        (output_dir / "price_space_break_classification_report.md").write_text(
            "# Price-Space Break Classification\n\nNo price_space_break issues found.\n",
            encoding="utf-8",
        )
        print(json.dumps({"output_dir": str(output_dir), "summary": summary}, indent=2))
        return 0

    start, end = expanded_date_bounds(
        bounds[0],
        bounds[1],
        padding_days=max(0, int(args.date_padding_days)),
    )
    tickers = sorted({str(issue.get("ticker") or "").strip().upper() for issue in issues})
    classifier = build_price_space_break_classifier(
        mode=args.classifier,
        env_file=Path(args.env_file) if args.env_file else None,
        cache_dir=Path(args.cache_dir),
        timeout_seconds=args.timeout_seconds,
    )
    if classifier is None:
        raise SystemExit("--classifier off cannot classify price-space breaks")
    classifier.preload(tickers, start, end)
    rows = classify_price_space_issues(issues=issues, classifier=classifier)
    summary = summarize_classifications(rows)
    summary["classifier"] = args.classifier
    summary["split_lookup_range"] = {"start": start.isoformat(), "end": end.isoformat()}
    summary["classifier_diagnostics"] = classifier.diagnostics()

    write_json(output_dir / "price_space_break_classifications.json", rows)
    write_json(output_dir / "price_space_break_classification_summary.json", summary)
    write_csv(output_dir / "price_space_break_classifications.csv", rows)
    (output_dir / "price_space_break_classification_report.md").write_text(
        render_report(summary, rows),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "rows": len(rows),
                "summary": summary,
                "csv": str(output_dir / "price_space_break_classifications.csv"),
                "report": str(output_dir / "price_space_break_classification_report.md"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 0 diagnostic classifier for backtest price_space_break issues."
    )
    parser.add_argument(
        "--backtest-results",
        required=True,
        help="Path to a backtest_results.json containing data_issues.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for classification JSON/CSV/Markdown outputs.",
    )
    parser.add_argument(
        "--classifier",
        choices=["massive_splits"],
        default="massive_splits",
        help="Corporate-action source used for classification.",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR),
        help="Cache directory for Massive split corporate-action lookups.",
    )
    parser.add_argument("--env-file", help="Optional .env file containing Massive credentials.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--date-padding-days",
        type=int,
        default=0,
        help="Optional padding around break dates for split lookup diagnostics.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "date",
        "ticker",
        "observed_ratio",
        "ratio_basis",
        "category",
        "confidence",
        "action",
        "reason",
        "expected_ratio",
        "split_execution_date",
        "split_from",
        "split_to",
        "split_adjustment_type",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            classification = row.get("classification") or {}
            split = classification.get("split_event") or {}
            writer.writerow(
                {
                    "date": row.get("date"),
                    "ticker": row.get("ticker"),
                    "observed_ratio": classification.get("observed_ratio"),
                    "ratio_basis": classification.get("ratio_basis"),
                    "category": classification.get("category"),
                    "confidence": classification.get("confidence"),
                    "action": classification.get("action"),
                    "reason": classification.get("reason"),
                    "expected_ratio": classification.get("expected_ratio"),
                    "split_execution_date": split.get("execution_date"),
                    "split_from": split.get("split_from"),
                    "split_to": split.get("split_to"),
                    "split_adjustment_type": split.get("adjustment_type"),
                }
            )


def render_report(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Price-Space Break Classification",
        "",
        f"- Breaks classified: {summary['count']}",
        f"- Category counts: `{json.dumps(summary['category_counts'], sort_keys=True)}`",
        f"- Action counts: `{json.dumps(summary['action_counts'], sort_keys=True)}`",
        f"- Split lookup range: `{summary['split_lookup_range']['start']} -> {summary['split_lookup_range']['end']}`",
        "",
        "## Rows",
        "",
        "| Date | Ticker | Ratio | Category | Action | Confidence | Reason |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for row in rows:
        classification = row.get("classification") or {}
        ratio = classification.get("observed_ratio")
        ratio_text = f"{float(ratio):.6f}" if ratio is not None else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("date") or ""),
                    str(row.get("ticker") or ""),
                    ratio_text,
                    str(classification.get("category") or ""),
                    str(classification.get("action") or ""),
                    str(classification.get("confidence") or ""),
                    str(classification.get("reason") or ""),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
