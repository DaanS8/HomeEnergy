"""Pure daily consumption and EV analysis over stored snapshots."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from .consumption import ZONE, consumption_readings, midnight, total_kwh
from .energy_balance import EnergyBalanceValidationError
from .ev import EV_POWER_KW, MINIMUM_EV_DURATION, SAMPLE_INTERVAL, detect_ev_blocks
from .models import DailyUsage, EnergyBalanceDay, UsageInterval


class DailyUsageRequestError(ValueError):
    """The selected day or its following context day has not finished."""


def validate_daily_date(selected: date, now: datetime) -> None:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")
    if (not isinstance(selected, date) or isinstance(selected, datetime)
            or selected <= date.min or selected > now.astimezone(ZONE).date() - timedelta(days=2)):
        raise DailyUsageRequestError("Select a Brussels date at least two days ago.")


def analyse_daily_usage(days: list[EnergyBalanceDay], selected: date, *, now: datetime) -> DailyUsage:
    """Require complete consumption and context before detecting EV blocks."""
    validate_daily_date(selected, now)
    start, end = midnight(selected), midnight(selected + timedelta(days=1))
    context_start, context_end = start - MINIMUM_EV_DURATION, end + MINIMUM_EV_DURATION
    target = next((day for day in days if day.day == selected), None)
    if target is None:
        raise EnergyBalanceValidationError("The requested consumption day is missing.")
    available = consumption_readings(days, context_start, context_end)
    total = total_kwh(target)
    if (total is None or len(available) != int((context_end - context_start) / SAMPLE_INTERVAL)
            or any(power is None for power in available.values())):
        raise EnergyBalanceValidationError("Expected complete consumption, daily total, and boundary context.")

    readings = sorted(available.items())
    blocks = detect_ev_blocks(readings, start, end)
    intervals = tuple(UsageInterval(
        stamp, stamp + SAMPLE_INTERVAL, value,
        EV_POWER_KW if any(b.start_at <= stamp < b.end_at for b in blocks) else Decimal(0),
    ) for stamp, value in readings if start <= stamp < end)
    return DailyUsage(selected, target.fetched_at, total,
                      sum((block.energy_kwh for block in blocks), Decimal(0)), intervals, blocks)
