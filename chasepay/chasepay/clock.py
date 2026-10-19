"""One place that decides what 'today' means (Nigeria time by default)."""
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo


def _tz():
    return ZoneInfo(os.environ.get("APP_TZ", "Africa/Lagos"))


def today() -> date:
    return datetime.now(_tz()).date()


def now_iso() -> str:
    return datetime.now(_tz()).isoformat(timespec="seconds")
