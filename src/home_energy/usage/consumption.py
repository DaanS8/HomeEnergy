"""Validate complete consumption snapshots and the required EV context."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .constants import PLANT_ID, PLANT_TIMEZONE
from .energy_balance import EnergyBalanceValidationError
from .ev import SAMPLE_INTERVAL
from .models import EnergyBalanceDay


ZONE = ZoneInfo(PLANT_TIMEZONE)


def midnight(selected: date) -> datetime:
    return datetime.combine(selected, time.min, ZONE).astimezone(timezone.utc)


def consumption_readings(days: list[EnergyBalanceDay], start: datetime, end: datetime) -> dict[datetime, Decimal | None]:
    """Validate consumption only inside the required, fully elapsed window."""
    readings: dict[datetime, Decimal | None] = {}
    for day in days:
        if day.plant_id != PLANT_ID or day.timezone != PLANT_TIMEZONE or "usePower" not in day.available_series:
            raise EnergyBalanceValidationError("Expected our plant's total consumption series.")
        for sample in day.samples:
            if sample.measurement != "usePower":
                continue
            stamp = sample.observed_at
            if stamp is None:
                # A label can locate an unused reading without resolving an
                # ambiguous DST fold. Never choose a fold for required data.
                try:
                    label = datetime.fromisoformat(sample.axis_label.removesuffix(" DST"))
                except ValueError:
                    raise EnergyBalanceValidationError("Consumption timestamps must be unambiguous.") from None
                candidates = ([label.astimezone(timezone.utc)] if label.tzinfo is not None else
                              [label.replace(tzinfo=ZONE, fold=f).astimezone(timezone.utc) for f in (0, 1)])
                if all(s < start or s + SAMPLE_INTERVAL > end for s in candidates):
                    continue
                raise EnergyBalanceValidationError("Consumption timestamps must be unambiguous.")
            if stamp.utcoffset() is None:
                raise EnergyBalanceValidationError("Consumption timestamps must be timezone-aware.")
            stamp = stamp.astimezone(timezone.utc)
            if not start <= stamp or stamp + SAMPLE_INTERVAL > end:
                continue
            if stamp.astimezone(ZONE).date() != day.day:
                raise EnergyBalanceValidationError("Consumption timestamp is outside its local day.")
            if (stamp - start) % SAMPLE_INTERVAL:
                raise EnergyBalanceValidationError("Expected a five-minute consumption grid.")
            if sample.unit not in (None, "kW"):
                raise EnergyBalanceValidationError("Expected consumption power in kW.")
            if stamp in readings:
                raise EnergyBalanceValidationError("Duplicate consumption timestamp.")
            value = sample.value
            if value is not None and (not value.is_finite() or value < 0):
                raise EnergyBalanceValidationError("Expected finite nonnegative consumption.")
            readings[stamp] = value
    return readings


def total_kwh(day: EnergyBalanceDay) -> Decimal | None:
    metric = next((m for m in day.metrics if m.name == "totalUsePower"), None)
    if metric is not None and (metric.unit not in (None, "kWh") or
                              metric.value is not None and (not metric.value.is_finite() or metric.value < 0)):
        raise EnergyBalanceValidationError("Expected daily consumption energy in kWh.")
    return metric.value if metric is not None else None


def validate_complete_day(day: EnergyBalanceDay) -> None:
    """Require every five-minute consumption interval and the provider daily total."""
    start, end = midnight(day.day), midnight(day.day + timedelta(days=1))
    readings = consumption_readings([day], start, end)
    if (total_kwh(day) is None or len(readings) != int((end - start) / SAMPLE_INTERVAL)
            or any(power is None for power in readings.values())):
        raise EnergyBalanceValidationError("Expected complete daily consumption readings and total.")
