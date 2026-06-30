"""
utils/date_helper.py

Reusable date-range helpers used across the analytics dashboard and export views.
All functions return (start_date, end_date) as datetime.date objects, inclusive.
"""

from datetime import date, timedelta


def last_n_days(n: int) -> tuple[date, date]:
    """
    Returns (start, end) for the last n calendar days, ending yesterday.

    Example: last_n_days(7) on 2024-06-14 → (2024-06-07, 2024-06-13)
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=n - 1)
    return start, end


def last_7_days() -> tuple[date, date]:
    """Convenience wrapper for last_n_days(7)."""
    return last_n_days(7)


def last_30_days() -> tuple[date, date]:
    """Convenience wrapper for last_n_days(30)."""
    return last_n_days(30)


def this_month() -> tuple[date, date]:
    """
    Returns (first day of current month, today).
    """
    today = date.today()
    start = today.replace(day=1)
    return start, today


def this_week() -> tuple[date, date]:
    """
    Returns (Monday of current ISO week, today).
    """
    today = date.today()
    start = today - timedelta(days=today.weekday())  # Monday
    return start, today


def date_range(start: date, end: date):
    """
    Generator that yields each date from start to end (inclusive).

    Useful for filling gaps in time-series data:

        for d in date_range(start, end):
            data[str(d)] = metrics.get(d, 0)
    """
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_date_param(param: str, fallback: date) -> date:
    """
    Safely parse a YYYY-MM-DD string from a query parameter.
    Returns fallback on any error.
    """
    if not param:
        return fallback
    try:
        return date.fromisoformat(param)
    except (ValueError, TypeError):
        return fallback
