"""Price analysis separated from storage and generic chart rendering."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

from .models import DayAheadPriceDay


DEFAULT_THRESHOLD = Decimal("300")
DEFAULT_HYSTERESIS = Decimal("25")
# User-supplied comparison profile, not a statistical estimate computed here.
# Its historical period and provenance are unknown. Slots are local 00:00–23:45.
SUPPLIED_REFERENCE_PROFILE = (
    118.2352, 112.5027, 108.1866, 105.2870, 105.1024, 104.3262, 103.8735, 101.6473,
    101.3949, 100.4488, 99.9914, 98.4413, 98.0242, 97.6427, 97.2286, 96.4234,
    96.3083, 97.0349, 97.5284, 97.8497, 98.4656, 100.5308, 102.3865, 105.5518,
    108.7636, 111.4180, 115.9445, 121.4428, 124.5968, 121.7613, 123.0098, 123.2674,
    123.5175, 117.0672, 113.9066, 106.7883, 104.7273, 97.6013, 93.0501, 83.7898,
    80.2727, 74.1659, 69.5636, 62.1081, 60.1670, 57.2616, 55.6164, 51.4180,
    49.8897, 47.4071, 45.2809, 42.1195, 40.5867, 38.6116, 37.0224, 35.6294,
    36.2548, 39.8808, 42.3358, 44.1906, 45.9361, 51.2847, 53.9696, 57.8516,
    60.2945, 67.7228, 72.1177, 78.7257, 82.5065, 92.8995, 100.1769, 110.0281,
    114.5571, 121.8091, 126.8687, 135.5083, 138.1718, 143.1329, 146.3227, 151.3231,
    152.4936, 152.3054, 152.7029, 155.3851, 154.0983, 147.4717, 143.2153, 139.5832,
    137.6558, 133.7162, 130.7637, 124.6846, 122.3206, 118.1029, 113.5398, 108.6313,
)


@dataclass(frozen=True)
class PriceRange:
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class PriceAnalysis:
    reference_prices: tuple[float, ...] | None
    high_mask: tuple[bool, ...]
    high_price_ranges: tuple[PriceRange, ...]


def validate_price_thresholds(threshold, hysteresis) -> tuple[Decimal, Decimal]:
    """Normalize the trigger and nonnegative range-expansion margin."""
    values = []
    for name, value in (("Threshold", threshold), ("Hysteresis", hysteresis)):
        try:
            value = Decimal(str(value))
        except ArithmeticError:
            raise ValueError(f"{name} must be a finite number in EUR/MWh.") from None
        if not value.is_finite():
            raise ValueError(f"{name} must be a finite number in EUR/MWh.")
        values.append(value)
    if values[1] < 0:
        raise ValueError("Hysteresis must be nonnegative.")
    return values[0], values[1]


def analyse_prices(day: DayAheadPriceDay, *, threshold: Decimal = DEFAULT_THRESHOLD,
                   hysteresis: Decimal = DEFAULT_HYSTERESIS,
                   reference_profile: Sequence | None = SUPPLIED_REFERENCE_PROFILE) -> PriceAnalysis:
    """Flag runs above threshold minus margin that contain a price above threshold."""
    day.validate()
    threshold, hysteresis = validate_price_thresholds(threshold, hysteresis)
    reference = None
    if reference_profile is not None:
        if len(reference_profile) != 96:
            raise ValueError("Reference profile must contain 96 finite quarter-hour prices.")
        try:
            profile = tuple(Decimal(str(value)) for value in reference_profile)
        except ArithmeticError:
            raise ValueError("Reference profile must contain finite numbers.") from None
        if any(not value.is_finite() for value in profile):
            raise ValueError("Reference profile must contain finite numbers.")
        aligned = []
        zone = ZoneInfo(day.timezone)
        for interval in day.intervals:
            cursor, slots = interval.start_at, []
            while cursor < interval.end_at:
                local = cursor.astimezone(zone)
                slots.append(profile[local.hour * 4 + local.minute // 15])
                cursor += timedelta(minutes=15)
            aligned.append(float(sum(slots) / len(slots)))
        reference = tuple(aligned)
    lower = threshold - hysteresis
    high_mask = [False] * len(day.intervals)
    ranges = []
    start, triggered = None, False
    # A sentinel closes a run at the requested day's final interval.
    for index in range(len(day.intervals) + 1):
        if index < len(day.intervals) and day.intervals[index].price_eur_per_mwh > lower:
            if start is None:
                start = index
            triggered |= day.intervals[index].price_eur_per_mwh > threshold
        elif start is not None:
            if triggered:
                high_mask[start:index] = [True] * (index - start)
                ranges.append(PriceRange(day.intervals[start].start_at, day.intervals[index - 1].end_at))
            start, triggered = None, False
    return PriceAnalysis(reference, tuple(high_mask), tuple(ranges))
