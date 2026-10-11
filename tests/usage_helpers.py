"""Synthetic five-minute consumption snapshots without investigation imports."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from home_energy.usage.constants import PLANT_ID
from home_energy.usage.energy_balance import parse_energy_balance


ZONE = ZoneInfo("Europe/Brussels")
DAY = date(2026, 9, 2)


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
