from __future__ import annotations

import unittest
from datetime import date

from wheels_copilot.historical_fundamentals import (
    HistoricalFundamentalStore,
    MassiveDividendProvider,
    MassiveSecFinancialsProvider,
    PriceDerivedFundamentalsProvider,
    UnusualWhalesEarningsProvider,
)
from wheels_copilot.models import PriceBar


class HistoricalFundamentalStoreTests(unittest.TestCase):
    def test_sec_financials_exclude_future_filing_and_derive_pe_from_known_quarters(self):
        as_of = date(2026, 5, 1)
        rows = [
            _filing("2025", "Q4", "2025-12-31", "2026-02-10", 100, 1.0),
            _filing("2025", "Q3", "2025-09-30", "2025-11-05", 90, 0.9),
            _filing("2025", "Q2", "2025-06-30", "2025-08-05", 80, 0.8),
            _filing("2025", "Q1", "2025-03-31", "2025-05-10", 70, 0.7),
            _filing("2026", "Q1", "2026-03-31", "2026-05-10", 200, 2.0),
        ]
        store = HistoricalFundamentalStore(
            [
                PriceDerivedFundamentalsProvider(),
                MassiveSecFinancialsProvider(seed_rows={"AAPL": rows}),
            ]
        )

        snapshot = store.snapshot("AAPL", as_of, _bars(close=33.0))

        self.assertEqual(snapshot.quarterly_net_income[:4], [100, 90, 80, 70])
        self.assertNotIn(200, snapshot.quarterly_net_income)
        self.assertAlmostEqual(snapshot.pe_ratio or 0, 33.0 / 3.4)
        self.assertEqual(
            snapshot.provenance["pe_ratio"].quality,
            "strict_pit_pending_validation",
        )

    def test_sec_financials_infer_q4_known_at_from_annual_filing(self):
        rows = [
            _q4_without_known_at("2025", "2025-12-31", 100, 1.0),
            _annual_filing("2025", "2025-12-31", "2026-02-10", 400, 4.0),
            _filing("2025", "Q3", "2025-09-30", "2025-11-05", 90, 0.9),
            _filing("2025", "Q2", "2025-06-30", "2025-08-05", 80, 0.8),
            _filing("2025", "Q1", "2025-03-31", "2025-05-10", 70, 0.7),
        ]
        store = HistoricalFundamentalStore(
            [
                PriceDerivedFundamentalsProvider(),
                MassiveSecFinancialsProvider(seed_rows={"AAPL": rows}),
            ]
        )

        before_annual = store.snapshot(
            "AAPL",
            date(2026, 2, 1),
            [
                PriceBar(
                    date=date(2026, 1, 30),
                    open=33.0,
                    high=33.0,
                    low=33.0,
                    close=33.0,
                )
            ],
        )
        after_annual = store.snapshot(
            "AAPL",
            date(2026, 3, 1),
            [
                PriceBar(
                    date=date(2026, 2, 27),
                    open=33.0,
                    high=33.0,
                    low=33.0,
                    close=33.0,
                )
            ],
        )

        self.assertIsNone(before_annual.pe_ratio)
        self.assertAlmostEqual(after_annual.pe_ratio or 0, 33.0 / 3.4)
        self.assertEqual(after_annual.provenance["pe_ratio"].known_at, date(2026, 2, 10))
        self.assertIn(
            "q4_known_at_inferred_from_annual",
            after_annual.provenance["pe_ratio"].notes,
        )

    def test_sec_financials_do_not_build_ttm_from_non_contiguous_quarters(self):
        rows = [
            _filing("2025", "Q4", "2025-12-31", "2026-02-10", 100, 1.0),
            _filing("2025", "Q3", "2025-09-30", "2025-11-05", 90, 0.9),
            _filing("2025", "Q1", "2025-03-31", "2025-05-10", 70, 0.7),
            _filing("2024", "Q4", "2024-12-31", "2025-02-10", 60, 0.6),
        ]
        store = HistoricalFundamentalStore(
            [
                PriceDerivedFundamentalsProvider(),
                MassiveSecFinancialsProvider(seed_rows={"AAPL": rows}),
            ]
        )

        snapshot = store.snapshot("AAPL", date(2026, 5, 1), _bars(close=33.0))

        self.assertIsNone(snapshot.pe_ratio)
        self.assertIn("ttm_non_contiguous_quarters", snapshot.provenance["pe_ratio"].notes)

    def test_dividend_provider_requires_declaration_before_as_of(self):
        as_of = date(2026, 5, 1)
        provider = MassiveDividendProvider(
            seed_rows={
                "AAPL": [
                    {
                        "ticker": "AAPL",
                        "declaration_date": "2026-05-02",
                        "ex_dividend_date": "2026-05-10",
                        "cash_amount": 0.25,
                    },
                    {
                        "ticker": "AAPL",
                        "declaration_date": "2026-04-15",
                        "ex_dividend_date": "2026-05-20",
                        "cash_amount": 0.25,
                    },
                ]
            }
        )
        store = HistoricalFundamentalStore([provider])

        snapshot = store.snapshot("AAPL", as_of, _bars(close=100.0))

        self.assertEqual(snapshot.ex_dividend_date, date(2026, 5, 20))
        self.assertEqual(snapshot.provenance["ex_dividend_date"].known_at, date(2026, 4, 15))

    def test_earnings_provider_uses_heuristic_known_window(self):
        provider = UnusualWhalesEarningsProvider(
            seed_rows={
                "AAPL": [
                    {"report_date": "2026-05-30"},
                ]
            },
            known_days_before=21,
        )
        store = HistoricalFundamentalStore([provider])

        before_window = store.snapshot("AAPL", date(2026, 5, 1), [])
        inside_window = store.snapshot("AAPL", date(2026, 5, 10), [])

        self.assertIsNone(before_window.next_earnings_date)
        self.assertIsNone(before_window.provenance["next_earnings_date"].effective_date)
        self.assertEqual(inside_window.next_earnings_date, date(2026, 5, 30))
        self.assertEqual(
            inside_window.provenance["next_earnings_date"].quality,
            "approximate",
        )

    def test_earnings_provider_records_previous_report_date(self):
        provider = UnusualWhalesEarningsProvider(
            seed_rows={
                "AAPL": [
                    {"report_date": "2026-02-01"},
                    {"report_date": "2026-05-30"},
                ]
            },
            known_days_before=21,
        )
        store = HistoricalFundamentalStore([provider])

        snapshot = store.snapshot("AAPL", date(2026, 2, 5), [])

        self.assertEqual(snapshot.previous_earnings_date, date(2026, 2, 1))
        self.assertEqual(
            snapshot.provenance["previous_earnings_date"].quality,
            "approximate",
        )


def _filing(
    fiscal_year: str,
    fiscal_period: str,
    end_date: str,
    accepted: str,
    net_income: float,
    eps: float,
) -> dict:
    return {
        "timeframe": "quarterly",
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "end_date": end_date,
        "filing_date": accepted,
        "acceptance_datetime": f"{accepted}T10:00:00Z",
        "financials": {
            "income_statement": {
                "net_income_loss": {"value": net_income},
                "diluted_earnings_per_share": {"value": eps},
                "diluted_average_shares": {"value": 1_000_000},
            }
        },
    }


def _q4_without_known_at(
    fiscal_year: str,
    end_date: str,
    net_income: float,
    eps: float,
) -> dict:
    return {
        "timeframe": "quarterly",
        "fiscal_year": fiscal_year,
        "fiscal_period": "Q4",
        "end_date": end_date,
        "financials": {
            "income_statement": {
                "net_income_loss": {"value": net_income},
                "diluted_earnings_per_share": {"value": eps},
                "diluted_average_shares": {"value": 1_000_000},
            }
        },
    }


def _annual_filing(
    fiscal_year: str,
    end_date: str,
    accepted: str,
    net_income: float,
    eps: float,
) -> dict:
    return {
        "timeframe": "annual",
        "fiscal_year": fiscal_year,
        "fiscal_period": "FY",
        "end_date": end_date,
        "filing_date": accepted,
        "acceptance_datetime": f"{accepted}T10:00:00Z",
        "financials": {
            "income_statement": {
                "net_income_loss": {"value": net_income},
                "diluted_earnings_per_share": {"value": eps},
                "diluted_average_shares": {"value": 1_000_000},
            }
        },
    }


def _bars(close: float) -> list[PriceBar]:
    return [
        PriceBar(date=date(2026, 4, 27), open=close, high=close, low=close, close=close),
        PriceBar(date=date(2026, 4, 28), open=close, high=close, low=close, close=close),
        PriceBar(date=date(2026, 4, 29), open=close, high=close, low=close, close=close),
        PriceBar(date=date(2026, 4, 30), open=close, high=close, low=close, close=close),
    ]


if __name__ == "__main__":
    unittest.main()
