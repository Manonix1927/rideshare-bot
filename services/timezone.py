"""
Single source of truth for "now" across the bot.
All user-visible times (departure_time) are stored as naive Kyiv datetimes,
so all server-side "now" comparisons must use the same timezone.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def now() -> datetime:
    """Current datetime in Kyiv timezone, naive (no tzinfo), for DB/FSM comparison."""
    return datetime.now(KYIV_TZ).replace(tzinfo=None)


def today():
    """Current date in Kyiv timezone."""
    return datetime.now(KYIV_TZ).date()
