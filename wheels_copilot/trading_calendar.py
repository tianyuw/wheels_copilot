from __future__ import annotations

from datetime import date, timedelta


SPECIAL_NYSE_CLOSURES = {
    date(2025, 1, 9),  # National Day of Mourning for President Jimmy Carter.
}


def nyse_trading_days(start: date, end: date) -> list[date]:
    return [day for day in date_range(start, end) if is_nyse_trading_day(day)]


def nyse_trading_days_after(start: date, end: date) -> int:
    if end <= start:
        return 0
    return len(
        [
            day
            for day in date_range(start + timedelta(days=1), end)
            if is_nyse_trading_day(day)
        ]
    )


def is_nyse_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def nyse_holidays(year: int) -> set[date]:
    holidays = {
        observed_fixed_holiday(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        easter_sunday(year) - timedelta(days=2),  # Good Friday
        last_weekday(year, 5, 0),  # Memorial Day
        observed_fixed_holiday(date(year, 6, 19)),  # Juneteenth
        observed_fixed_holiday(date(year, 7, 4)),  # Independence Day
        nth_weekday(year, 9, 0, 1),  # Labor Day
        nth_weekday(year, 11, 3, 4),  # Thanksgiving
        observed_fixed_holiday(date(year, 12, 25)),  # Christmas
    }
    holidays = {holiday for holiday in holidays if holiday.year == year}
    holidays.update(holiday for holiday in SPECIAL_NYSE_CLOSURES if holiday.year == year)
    return holidays


def observed_fixed_holiday(holiday: date) -> date:
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    day = date(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + 7 * (occurrence - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    offset = (day.weekday() - weekday) % 7
    return day - timedelta(days=offset)


def easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
