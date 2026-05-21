from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wheels_copilot.scan import (
    classify_scan_result,
    render_markdown_report,
    resolve_output_dir,
    scan_watchlist,
    write_scan_outputs,
)


class ScanWorkflowTests(unittest.TestCase):
    def test_classify_auto_trade_watch_reject_error(self):
        self.assertEqual(classify_scan_result({"error": "boom"}), "ERROR")
        self.assertEqual(
            classify_scan_result({"candidate": {"auto_trade": True}}),
            "AUTO_TRADE",
        )
        self.assertEqual(
            classify_scan_result({"candidate": {"auto_trade": False}}),
            "WATCH",
        )
        self.assertEqual(classify_scan_result({"support_tradable": True}), "WATCH")
        self.assertEqual(classify_scan_result({"support_tradable": False}), "REJECT")

    def test_write_scan_outputs_creates_json_markdown_csv(self):
        scan = _sample_scan()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_scan_outputs(scan, Path(tmp))

            self.assertTrue(paths["json"].exists())
            self.assertTrue(paths["markdown"].exists())
            self.assertTrue(paths["csv"].exists())
            loaded = json.loads(paths["json"].read_text())
            self.assertEqual(loaded["summary"], {"AUTO_TRADE": 1})
            self.assertIn("Markus Wheel Daily Dry Run", paths["markdown"].read_text())
            self.assertIn("candidate_expiration", paths["csv"].read_text())

    def test_resolve_output_dir_avoids_existing_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "scan_results.json").write_text("{}")

            resolved = resolve_output_dir(base)

            self.assertNotEqual(resolved, base)
            self.assertEqual(resolved.parent, base)

    def test_resolve_output_dir_overwrite_uses_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "scan_results.json").write_text("{}")

            resolved = resolve_output_dir(base, overwrite=True)

            self.assertEqual(resolved, base)

    def test_markdown_report_contains_rejection_summary_for_watch(self):
        scan = _sample_scan(status="WATCH", candidate=None, rejection_summary={"bid_below_min": 3})

        report = render_markdown_report(scan)

        self.assertIn("bid_below_min:3", report)
        self.assertIn("WATCH", report)

    def test_markdown_report_escapes_table_pipes(self):
        scan = _sample_scan()
        scan["results"][0]["ticker"] = "BAD|PIPE"
        scan["results"][0]["rejection_summary"] = {"bad|reason": 1}

        report = render_markdown_report(scan)

        self.assertIn("BAD\\|PIPE", report)
        self.assertIn("bad\\|reason:1", report)

    def test_scan_watchlist_continues_when_one_ticker_raises(self):
        good = _sample_scan()["results"][0]

        def fake_scan_ticker(ticker, *_args, **_kwargs):
            if ticker == "BAD":
                raise RuntimeError("network broke")
            row = dict(good)
            row["ticker"] = ticker
            return row

        with patch("wheels_copilot.scan.scan_ticker", side_effect=fake_scan_ticker):
            scan = scan_watchlist({}, tickers=["BAD", "OK"])

        statuses = {row["ticker"]: row["status"] for row in scan["results"]}
        self.assertEqual(statuses["BAD"], "ERROR")
        self.assertEqual(statuses["OK"], "AUTO_TRADE")

    def test_scan_results_sort_same_status_score_by_ticker(self):
        row_b = _sample_scan()["results"][0]
        row_b["ticker"] = "BBB"
        row_a = dict(row_b)
        row_a["ticker"] = "AAA"

        def fake_scan_ticker(ticker, *_args, **_kwargs):
            return row_a if ticker == "AAA" else row_b

        with patch("wheels_copilot.scan.scan_ticker", side_effect=fake_scan_ticker):
            scan = scan_watchlist({}, tickers=["BBB", "AAA"])

        self.assertEqual([row["ticker"] for row in scan["results"]], ["AAA", "BBB"])


def _sample_scan(
    status: str = "AUTO_TRADE",
    candidate: dict | None = None,
    rejection_summary: dict | None = None,
) -> dict:
    candidate = candidate if candidate is not None else {
        "option": {
            "expiration": "2026-05-29",
            "strike": 90,
            "mid": 1.0,
            "executable_mid": 1.0,
        },
        "delta": -0.2,
        "delta_bucket": "strong_support",
        "auto_trade": True,
        "weekly_return_on_strike_pct": 0.77,
    }
    return {
        "scan_date": "2026-05-20",
        "generated_at": "2026-05-20T10:00:00",
        "period": "1y",
        "ticker_count": 1,
        "summary": {status: 1},
        "results": [
            {
                "status": status,
                "ticker": "TEST",
                "current_price": 100.0,
                "trend_passed": True,
                "support_tradable": status != "REJECT",
                "support_score": 88.0,
                "selected_support": {
                    "method": "pivot_cluster",
                    "bottom": 90.0,
                    "top": 92.0,
                    "score": 88.0,
                },
                "candidate": candidate,
                "option_count": 3,
                "rejection_summary": rejection_summary or {},
                "status_reason": "sample",
                "trend_reasons": [],
                "reasons": [],
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
