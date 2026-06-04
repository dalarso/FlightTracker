"""API usage / billing-period helpers — extracted from server.py.

Pure date-math + JSON usage-file readers behind /api/usage and /api/usage/adjust.
Self-contained (stdlib + a local Pacific tz) — no injection needed; server.py just
re-imports the three helpers so the route handlers call them unchanged.
"""
import calendar
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _billing_period_start(reset_day):
    """Return the current billing period start as YYYY-MM-DD."""
    today = datetime.now(_PACIFIC)
    if today.day >= reset_day:
        # Clamp to actual days in this month (e.g. reset_day=31 in February)
        this_month_days = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=min(reset_day, this_month_days)).strftime("%Y-%m-%d")
    first_of_month = today.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    # Clamp reset_day to actual days in last month (defensive for reset_day > 28)
    actual_day = min(reset_day, calendar.monthrange(last_month.year, last_month.month)[1])
    return last_month.replace(day=actual_day).strftime("%Y-%m-%d")


def _billing_period_end(reset_day):
    """Return the last day of the current billing period as YYYY-MM-DD."""
    today = datetime.now(_PACIFIC)
    # Determine year/month of the next reset date
    if today.day >= reset_day:
        y = today.year + 1 if today.month == 12 else today.year
        m = 1 if today.month == 12 else today.month + 1
    else:
        y, m = today.year, today.month
    # Clamp reset_day to actual days in that month (defensive for reset_day > 28)
    actual_day = min(reset_day, calendar.monthrange(y, m)[1])
    next_reset = today.replace(year=y, month=m, day=actual_day)
    return (next_reset - timedelta(days=1)).strftime("%Y-%m-%d")


def _read_usage_file(path, reset_day):
    """Read a usage JSON file, resetting if the billing period has rolled over."""
    period = _billing_period_start(reset_day)
    try:
        data = json.loads(Path(path).read_text())
        if data.get("period_start") == period:
            return data
    except Exception:
        pass
    return {"period_start": period, "value": 0.0}
