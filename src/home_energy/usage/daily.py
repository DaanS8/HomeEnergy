"""Load daily consumption with boundary context and assemble an EV usage report."""

from contextlib import ExitStack
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from .constants import PLANT_ID, PLANT_TIMEZONE
from .energy_balance import EnergyBalanceValidationError, parse_energy_balance
from .ev import EV_POWER_KW, MINIMUM_EV_DURATION, SAMPLE_INTERVAL, detect_ev_blocks
from .fusionsolar import FusionSolarClient
from .models import DailyUsage, EnergyBalanceDay, UsageInterval
from .repository import EnergyBalanceRepository


ZONE = ZoneInfo(PLANT_TIMEZONE)


class DailyUsageRequestError(ValueError):
    """The selected day is not today or a past Brussels date."""


class UsageConfigurationError(ValueError):
    """FusionSolar client configuration could not be loaded."""


def validate_daily_date(selected: date, now: datetime) -> None:
    if (not isinstance(selected, date) or isinstance(selected, datetime)
            or selected <= date.min or selected > now.astimezone(ZONE).date()):
        raise DailyUsageRequestError("Select today or a past Brussels date.")


def _midnight(selected: date) -> datetime:
    return datetime.combine(selected, time.min, ZONE).astimezone(timezone.utc)


def _consumption_readings(days: list[EnergyBalanceDay], start: datetime, end: datetime) -> dict[datetime, Decimal | None]:
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


def _total_kwh(day: EnergyBalanceDay) -> Decimal | None:
    metric = next((m for m in day.metrics if m.name == "totalUsePower"), None)
    if metric is not None and (metric.unit not in (None, "kWh") or
                              metric.value is not None and (not metric.value.is_finite() or metric.value < 0)):
        raise EnergyBalanceValidationError("Expected daily consumption energy in kWh.")
    return metric.value if metric is not None else None


def _source_is_complete(day: EnergyBalanceDay) -> bool:
    """Check storage eligibility without detecting or estimating EV charging."""
    start, end = _midnight(day.day), _midnight(day.day + timedelta(days=1))
    try:
        readings = _consumption_readings([day], start, end)
        return (_total_kwh(day) is not None and len(readings) == int((end - start) / SAMPLE_INTERVAL)
                and all(power is not None for power in readings.values()))
    except EnergyBalanceValidationError:
        # Keep incomplete/invalid snapshots in memory. The report validates its
        # required window; later requests can retry fetching unstored days.
        return False


def analyse_daily_usage(days: list[EnergyBalanceDay], selected: date, *, now: datetime) -> DailyUsage:
    """Prepare context, detect EV blocks, then assemble the requested local day."""
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")
    validate_daily_date(selected, now)
    start, end = _midnight(selected), _midnight(selected + timedelta(days=1))
    context_start, context_end = start - MINIMUM_EV_DURATION, end + MINIMUM_EV_DURATION
    cutoff = min(now.astimezone(timezone.utc), context_end)
    target = next((day for day in days if day.day == selected), None)
    if target is None:
        raise EnergyBalanceValidationError("The requested consumption day is missing.")
    available = _consumption_readings(days, context_start, cutoff)
    total_kwh = _total_kwh(target)

    readings = []
    stamp = context_start
    while stamp + SAMPLE_INTERVAL <= cutoff:
        readings.append((stamp, available.get(stamp)))
        stamp += SAMPLE_INTERVAL
    blocks, pending_start = detect_ev_blocks(readings, start, end, open_end=cutoff < context_end)
    intervals = []
    for stamp, value in readings:
        if not start <= stamp < end:
            continue
        ev = EV_POWER_KW if any(b.start_at <= stamp < b.end_at for b in blocks) else Decimal(0)
        if value is None or pending_start is not None and stamp >= pending_start:
            ev = None
        intervals.append(UsageInterval(stamp, stamp + SAMPLE_INTERVAL, value, ev))
    complete = cutoff == context_end and total_kwh is not None and all(power is not None for _, power in readings)
    return DailyUsage(selected, target.fetched_at, complete, total_kwh,
                      sum((block.energy_kwh for block in blocks), Decimal(0)), tuple(intervals), blocks)


class DailyUsageService:
    """Reuse immutable past snapshots; fetch today's evolving snapshot in memory."""

    def __init__(self, repository=None, *, client_factory=None,
                 now: Callable[[], datetime] | None = None):
        self.repository = repository if repository is not None else EnergyBalanceRepository()
        self.client_factory = client_factory if client_factory is not None else FusionSolarClient.from_config
        self.now = now if now is not None else lambda: datetime.now(timezone.utc)

    def __call__(self, selected: date | None = None) -> DailyUsage:
        now = self.now()
        if now.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        today = now.astimezone(ZONE).date()
        selected = selected if selected is not None else today
        validate_daily_date(selected, now)
        days = []
        with ExitStack() as stack:
            client = None
            for offset in (-1, 0, 1):
                day = selected + timedelta(days=offset)
                if day > today:
                    continue
                stored = self.repository.load_day(day) if day < today else None
                if stored is None:
                    if client is None:
                        try:
                            client = stack.enter_context(self.client_factory())
                        except ValueError:
                            raise UsageConfigurationError("Invalid FusionSolar configuration.") from None
                    stored = parse_energy_balance(client.get_energy_balance(day), plant_id=PLANT_ID,
                                                  day=day, timezone_name=PLANT_TIMEZONE,
                                                  fetched_at=self.now(), units=client.config.get("units"))
                    if day < today and _source_is_complete(stored) and not self.repository.save_day(stored):
                        # A concurrent ingestion may already have committed this date.
                        stored = self.repository.load_day(day)
                        if stored is None:
                            raise EnergyBalanceValidationError("Stored consumption day is missing.")
                days.append(stored)
        return analyse_daily_usage(days, selected, now=now)
