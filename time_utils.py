"""UTC storage and business-local time helpers for Makhzoon."""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc


def business_zone(timezone_name):
    return ZoneInfo(timezone_name)


def local_today(timezone_name, now=None):
    """Return the business-local date, independently of the host timezone."""
    if now is None:
        return datetime.now(business_zone(timezone_name)).date()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(business_zone(timezone_name)).date()


def local_date_range_to_utc(start_date, end_date_exclusive, timezone_name):
    """Convert local calendar-date boundaries to naive UTC database values."""
    zone = business_zone(timezone_name)
    start = datetime.combine(start_date, time.min, tzinfo=zone)
    end = datetime.combine(end_date_exclusive, time.min, tzinfo=zone)
    return (
        start.astimezone(UTC).replace(tzinfo=None),
        end.astimezone(UTC).replace(tzinfo=None),
    )


def utc_to_local(value, timezone_name):
    """Interpret a database datetime as UTC and convert it for presentation."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.astimezone(business_zone(timezone_name))


def utc_to_local_date(value, timezone_name):
    local_value = utc_to_local(value, timezone_name)
    return local_value.date() if local_value else None
