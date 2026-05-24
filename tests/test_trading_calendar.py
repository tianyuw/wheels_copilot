from __future__ import annotations

from datetime import date

from wheels_copilot.trading_calendar import (
    is_nyse_trading_day,
    nyse_trading_days,
    nyse_trading_days_after,
)


def test_nyse_calendar_excludes_good_friday_and_special_closure() -> None:
    assert is_nyse_trading_day(date(2025, 4, 17)) is True
    assert is_nyse_trading_day(date(2025, 4, 18)) is False
    assert is_nyse_trading_day(date(2025, 1, 9)) is False


def test_nyse_trading_days_skips_market_holidays() -> None:
    days = nyse_trading_days(date(2025, 4, 17), date(2025, 4, 21))

    assert days == [date(2025, 4, 17), date(2025, 4, 21)]


def test_nyse_trading_days_after_counts_sessions_not_weekends_or_holidays() -> None:
    assert nyse_trading_days_after(date(2026, 1, 2), date(2026, 1, 5)) == 1
    assert nyse_trading_days_after(date(2025, 4, 17), date(2025, 4, 21)) == 1
    assert nyse_trading_days_after(date(2026, 1, 5), date(2026, 1, 5)) == 0
