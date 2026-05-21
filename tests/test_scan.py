from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from wheels_copilot.config import load_config
from wheels_copilot.models import (
    BrokerAccountSnapshot,
    BrokerPosition,
    FundamentalSnapshot,
    OptionQuote,
    PortfolioSnapshot,
    PriceBar,
    SupportAnalysis,
    SupportZone,
    TrendCheck,
)
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
            classify_scan_result(
                {"candidate": {"auto_trade": True}, "manual_review_required": True}
            ),
            "WATCH",
        )
        self.assertEqual(
            classify_scan_result(
                {"fundamental_gate": {"status": "REJECT"}, "support_tradable": True}
            ),
            "REJECT",
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

    def test_scan_ticker_stops_on_fundamental_reject(self):
        from wheels_copilot.scan import scan_ticker

        bars = [
            _price_bar(100),
            _price_bar(101),
        ]
        snapshot = FundamentalSnapshot(
            ticker="BAD",
            quote_type="EQUITY",
            market_cap=1,
            pe_ratio=10,
            quarterly_net_income=[1, 1, 1, 1, 1],
            annual_net_income=[1, 1, 1, 1, 1],
            recent_move_pct=0,
        )

        with (
            patch("wheels_copilot.scan.fetch_daily_bars", return_value=bars),
            patch("wheels_copilot.scan.fetch_fundamental_snapshot", return_value=snapshot),
            patch("wheels_copilot.scan.analyze_support") as analyze_support,
        ):
            row = scan_ticker("BAD", load_config_like(), as_of=date_like())

        self.assertEqual(row["status"], "REJECT")
        self.assertEqual(row["fundamental_gate"]["status"], "REJECT")
        analyze_support.assert_not_called()

    def test_scan_ticker_auto_trade_when_gates_pass(self):
        from wheels_copilot.scan import scan_ticker

        config = load_config("config/markus_wheel.yaml")
        bars = [_price_bar(100), _price_bar(101)]
        snapshot = _snapshot(next_earnings_date=date(2026, 8, 1))
        support = _tradable_support()
        option = _option(date(2026, 5, 22))

        with (
            patch("wheels_copilot.scan.fetch_daily_bars", return_value=bars),
            patch("wheels_copilot.scan.fetch_fundamental_snapshot", return_value=snapshot),
            patch("wheels_copilot.scan.analyze_support", return_value=support),
            patch("wheels_copilot.scan.fetch_put_chain", return_value=[option]),
        ):
            row = scan_ticker("GOOD", config, as_of=date(2026, 5, 20))

        self.assertEqual(row["status"], "AUTO_TRADE")
        self.assertFalse(row["manual_review_required"])
        self.assertEqual(row["fundamental_gate"]["status"], "PASS")
        self.assertEqual(row["earnings_gate"]["status"], "PASS")

    def test_scan_ticker_rejects_when_portfolio_gate_rejects(self):
        from wheels_copilot.scan import scan_ticker

        config = load_config("config/markus_wheel.yaml")
        portfolio = _portfolio(
            positions=[BrokerPosition(symbol="GOOD", qty=100, asset_class="us_equity")]
        )

        with (
            patch("wheels_copilot.scan.fetch_daily_bars", return_value=[_price_bar(100)]),
            patch(
                "wheels_copilot.scan.fetch_fundamental_snapshot",
                return_value=_snapshot(next_earnings_date=date(2026, 8, 1)),
            ),
            patch("wheels_copilot.scan.analyze_support", return_value=_tradable_support()),
            patch("wheels_copilot.scan.fetch_put_chain", return_value=[_option(date(2026, 5, 22))]),
        ):
            row = scan_ticker(
                "GOOD",
                config,
                as_of=date(2026, 5, 20),
                portfolio_snapshot=portfolio,
                portfolio_required=True,
            )

        self.assertEqual(row["status"], "REJECT")
        self.assertEqual(row["portfolio_gate"]["status"], "REJECT")
        self.assertIn(
            "covered_call_workflow_required_existing_100_shares",
            row["portfolio_gate"]["reasons"],
        )

    def test_scan_watchlist_with_missing_required_portfolio_demotes_candidate_to_watch(self):
        good = _sample_scan()
        candidate_row = good["results"][0]

        def fake_scan_ticker(ticker, *_args, **kwargs):
            row = dict(candidate_row)
            row["ticker"] = ticker
            if kwargs.get("portfolio_required"):
                row["portfolio_gate"] = {
                    "status": "WARN",
                    "reasons": ["portfolio_review_required"],
                    "warnings": ["AlpacaRequestError: down"],
                }
                row["manual_review_required"] = True
                row["status"] = classify_scan_result(row)
            return row

        with patch("wheels_copilot.scan.scan_ticker", side_effect=fake_scan_ticker):
            scan = scan_watchlist(
                {},
                tickers=["GOOD"],
                portfolio_required=True,
                portfolio_error="AlpacaRequestError: down",
            )

        self.assertEqual(scan["portfolio"]["error"], "AlpacaRequestError: down")
        self.assertEqual(scan["results"][0]["status"], "WATCH")

    def test_scan_ticker_rejects_when_all_options_hit_earnings_window(self):
        from wheels_copilot.scan import scan_ticker

        config = load_config("config/markus_wheel.yaml")
        bars = [_price_bar(100), _price_bar(101)]
        snapshot = _snapshot(next_earnings_date=date(2026, 5, 22))
        support = _tradable_support()
        options = [_option(date(2026, 5, 22)), _option(date(2026, 5, 29))]

        with (
            patch("wheels_copilot.scan.fetch_daily_bars", return_value=bars),
            patch("wheels_copilot.scan.fetch_fundamental_snapshot", return_value=snapshot),
            patch("wheels_copilot.scan.analyze_support", return_value=support),
            patch("wheels_copilot.scan.fetch_put_chain", return_value=options),
        ):
            row = scan_ticker("GOOD", config, as_of=date(2026, 5, 20))

        self.assertEqual(row["status"], "REJECT")
        self.assertEqual(row["earnings_gate"]["status"], "REJECT")
        self.assertIsNone(row["candidate"])


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
                "fundamental_snapshot": {
                    "market_cap": 10_000_000_000,
                    "pe_ratio": 20,
                    "dividend_yield": 0.01,
                    "next_earnings_date": "2026-08-01",
                },
                "fundamental_gate": {"status": "PASS", "reasons": ["ok"], "warnings": []},
                "earnings_gate": {"status": "PASS", "reasons": ["ok"], "warnings": []},
                "portfolio_gate": None,
                "portfolio_risk": None,
                "manual_review_required": False,
                "candidate": candidate,
                "option_count": 3,
                "rejection_summary": rejection_summary or {},
                "status_reason": "sample",
                "trend_reasons": [],
                "reasons": [],
            }
        ],
    }


def _price_bar(close: float):
    return PriceBar(
        date=date(2026, 5, 20),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1_000_000,
    )


def load_config_like():
    return {
        "fundamental_filters": {
            "market_cap_min_hard": 2_000_000_000,
            "market_cap_preferred": 5_000_000_000,
            "pe_max": 50,
            "min_positive_quarters_out_of_5": 4,
            "min_positive_years_out_of_5": 4,
            "prefer_dividend": True,
            "reject_biotech": True,
            "reject_chinese_adr": True,
            "reject_leveraged_etf": True,
            "reject_recent_100pct_movers": True,
        }
    }


def _snapshot(next_earnings_date: date) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        ticker="GOOD",
        quote_type="EQUITY",
        market_cap=10_000_000_000,
        pe_ratio=20,
        dividend_yield=0.01,
        quarterly_net_income=[1, 1, 1, 1],
        annual_net_income=[1, 1, 1, 1],
        next_earnings_date=next_earnings_date,
        recent_move_pct=0,
    )


def _tradable_support() -> SupportAnalysis:
    zone = SupportZone(
        method="pivot_cluster",
        center=91,
        bottom=90,
        top=92,
        touches=3,
        score=90,
    )
    return SupportAnalysis(
        trend=TrendCheck(
            passed=True,
            current_price=100,
            sma200=90,
            sma200_slope=0.1,
            reasons=[],
        ),
        zones=[zone],
        selected_zone=zone,
        atr14=2,
        current_price=100,
        min_score_to_trade=70,
        reasons=[],
    )


def _portfolio(
    positions: list[BrokerPosition] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account=BrokerAccountSnapshot(
            status="ACTIVE",
            equity=500000,
            cash=500000,
            buying_power=500000,
        ),
        positions=positions or [],
        open_orders=[],
        source="test",
    )


def _option(expiration: date) -> OptionQuote:
    return OptionQuote(
        symbol=f"GOOD{expiration.isoformat()}P85",
        expiration=expiration,
        dte=max((expiration - date(2026, 5, 20)).days, 1),
        strike=85,
        bid=1,
        ask=1.08,
        last=1,
        implied_volatility=0.3,
        open_interest=500,
        volume=50,
        delta=-0.2,
    )


def date_like():
    return date(2026, 5, 20)


if __name__ == "__main__":
    unittest.main()
