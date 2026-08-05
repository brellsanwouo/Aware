"""Time conversion tools for BuildSpec generation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC_PLUS_8 = timezone(timedelta(hours=8))
UTC_PLUS_8_LABEL = "UTC+08:00"


def datetime_to_unix_utc8(date_value: str, time_value: str) -> int:
    """Convert YYYY-MM-DD + HH:MM:SS to UNIX seconds using UTC+08."""
    dt = datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=UTC_PLUS_8
    )
    return int(dt.timestamp())


def time_range_to_unix_utc8(
    date_value: str,
    start_time: str,
    end_time: str,
    *,
    timezone_offset_minutes: int = 480,
) -> tuple[int, int]:
    """Convert a same-day time range using an explicit UTC offset.

    The historical default remains UTC+08 (480 minutes) for Bank compatibility.
    Nezha timestamps use UTC and therefore pass zero.
    """
    selected_timezone = timezone(timedelta(minutes=int(timezone_offset_minutes)))
    start_dt = datetime.strptime(f"{date_value} {start_time}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=selected_timezone
    )
    end_dt = datetime.strptime(f"{date_value} {end_time}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=selected_timezone
    )
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(minutes=30)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def seconds_of_day_to_unix_utc8(date_value: str, seconds_since_midnight: int) -> int:
    """Convert seconds since day start into UNIX seconds for date at UTC+08."""
    day_start = datetime.strptime(date_value, "%Y-%m-%d").replace(tzinfo=UTC_PLUS_8)
    return int(day_start.timestamp()) + int(seconds_since_midnight)


def normalize_unix_like_to_utc8(value: int, date_value: str) -> int:
    """Normalize ms epoch or seconds-of-day into UNIX seconds aligned to UTC+08."""
    # Milliseconds epoch -> seconds epoch.
    if abs(value) >= 1_000_000_000_000:
        return value // 1000
    # Seconds inside one day -> map onto date midnight in UTC+08.
    if 0 <= value < 86_400:
        return seconds_of_day_to_unix_utc8(date_value, value)
    return value
