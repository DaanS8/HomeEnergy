"""Plain objects for Belgian day-ahead prices in EUR/MWh."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..filling import validate_range


MARKET = "BE"
TIMEZONE = "Europe/Brussels"
# SDAC changed delivery resolution on this business date; entsoe-py uses
# the same boundary to select the native hourly/quarter-hourly series.
QUARTER_HOURLY_FROM = date(2025, 10, 1)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    validate_range(day)
    zone = ZoneInfo(TIMEZONE)
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start, end


def interval_duration(day: date) -> timedelta:
    return timedelta(minutes=15 if day >= QUARTER_HOURLY_FROM else 60)


@dataclass(frozen=True)
class PriceInterval:
    start_at: datetime
    end_at: datetime
    price_eur_per_mwh: Decimal


@dataclass
class DayAheadPriceDay:
    day: date
    market: str
    timezone: str
    fetched_at: datetime
    intervals: list[PriceInterval]

    def validate(self) -> None:
        start, end = day_bounds(self.day)
        if self.market != MARKET or self.timezone != TIMEZONE:
            raise ValueError("Day-ahead prices must use BE and Europe/Brussels.")
        if not isinstance(self.fetched_at, datetime) or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware.")
        duration = interval_duration(self.day)
        expected = int((end - start) / duration)
        if len(self.intervals) != expected:
            raise ValueError(f"A complete price day requires {expected} native intervals.")
        cursor = start
        for interval in self.intervals:
            if any(not isinstance(ts, datetime) or ts.utcoffset() != timedelta(0)
                   for ts in (interval.start_at, interval.end_at)):
                raise ValueError("Price interval timestamps must be aware UTC instants.")
            if interval.start_at != cursor or interval.end_at != cursor + duration:
                raise ValueError("Price intervals must cover the day exactly without gaps or overlaps.")
            if not isinstance(interval.price_eur_per_mwh, Decimal) or not interval.price_eur_per_mwh.is_finite():
                raise ValueError("Prices must be finite Decimals; negative and zero prices are valid.")
            cursor = interval.end_at
        if cursor != end:
            raise ValueError("Price intervals must end at the next local midnight.")
