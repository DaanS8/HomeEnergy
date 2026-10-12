"""Synthetic five-minute consumption snapshots without investigation imports."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from home_energy.usage.constants import PLANT_ID
from home_energy.usage.energy_balance import parse_energy_balance
from home_energy.usage.models import DailyMetric


ZONE = ZoneInfo("Europe/Brussels")
DAY = date(2026, 9, 2)
NOW = datetime(2026, 9, 4, 12, tzinfo=ZONE)


def _utc(text):
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZONE)
    return stamp.astimezone(timezone.utc)


def source_days(first=DAY - timedelta(days=1), count=3, loads=(), missing=(), unit=None):
    windows = [(_utc(begin), _utc(finish), str(power)) for begin, finish, power in loads]
    days = []
    for offset in range(count):
        day = first + timedelta(days=offset)
        stamp = datetime.combine(day, time.min, ZONE).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time.min, ZONE).astimezone(timezone.utc)
        labels, values = [], []
        while stamp < end:
            value = "0.2"
            for begin, finish, power in windows:
                if begin <= stamp < finish:
                    value = power
            labels.append(stamp.astimezone(ZONE).isoformat())
            values.append(None if stamp in missing else value)
            stamp += timedelta(minutes=5)
        payload = {"data": {"stationDn": PLANT_ID, "xAxis": labels, "usePower": values,
                            "productPower": ["99"] * len(labels)}}
        days.append(parse_energy_balance(payload, plant_id=PLANT_ID, day=day, timezone_name=str(ZONE),
                                         units={"usePower": unit} if unit else None))
    return days


def complete_energy_payload(day, *, loads=()):
    """A complete provider day for ingestion tests, including DST-length days."""
    source = source_days(first=day, count=1, loads=loads)[0]
    consumption = [s for s in source.samples if s.measurement == "usePower"]
    return {"success": True, "data": {
        "stationDn": PLANT_ID, "xAxis": [s.axis_label for s in consumption],
        "usePower": [s.original_value for s in consumption], "totalUsePower": "42.750",
    }}


def complete_source_days(**kwargs):
    """Consumption plus the independent provider daily total."""
    result = source_days(**kwargs)
    for day in result:
        day.metrics.append(DailyMetric("totalUsePower", Decimal("42.750"), "42.750", "kWh"))
    return result
