"""Validate the observed daily API schema without guessing units or intervals."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
import re
from zoneinfo import ZoneInfo

from .models import DailyMetric, EnergyBalanceDay, MeasurementSample


DAILY_METRIC_FIELDS = frozenset({
    "totalBusinessChargePower", "totalProductPower", "totalSelfUsePower",
    "totalOnGridPower", "totalBuyPower", "totalUsePower", "selfProvide",
    "totalGeneratorPower", "totalBusinessGeneratorPower", "onGridPowerRatio",
    "selfUsePowerRatioByProduct", "buyPowerRatio", "selfUsePowerRatioByUse",
    "generatorEnergyRatioByUse", "businessGeneratorEnergyRatioByUse",
})
SERIES_FIELDS = frozenset({
    "productPower", "dieselProductPower", "mainsUsePower", "onGridPower",
    "disGridPower", "usePower", "maxDemand", "selfUsePower", "chargePower",
    "dischargePower", "radiationDosePower", "chargeAndDisChargePower",
    "meterActivePower", "generatorPower", "businessGeneratorPower", "soc",
    "businessChargePower",
})


class EnergyBalanceValidationError(ValueError):
    """A response cannot be stored as a complete daily snapshot."""


def _reading(value, field):
    if value is None or value in ("", "--"):
        return None, value
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise EnergyBalanceValidationError(f"{field} contains a nonnumeric reading.")
    original = str(value)
    try:
        numeric = Decimal(original)
    except InvalidOperation:
        raise EnergyBalanceValidationError(f"{field} contains an invalid numeric reading.") from None
    if not numeric.is_finite():
        raise EnergyBalanceValidationError(f"{field} contains a nonfinite reading.")
    return numeric, original


def _timestamp(label: str, zone: ZoneInfo, day: date) -> datetime | None:
    is_dst = label.endswith(" DST")
    text = label[:-4] if is_dst else label
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?", text):
        return None
    try:
        local = datetime.fromisoformat(text)
    except ValueError:
        return None
    if local.date() != day:
        raise EnergyBalanceValidationError("An axis label belongs to a different selected day.")
    if local.tzinfo is not None:
        instant = local.astimezone(timezone.utc)
        if instant.astimezone(zone).date() != day:
            raise EnergyBalanceValidationError("An axis timestamp belongs to a different plant-local day.")
        return instant
    candidates = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        instant = candidate.astimezone(timezone.utc)
        # Round-tripping rejects nonexistent spring-forward local times.
        if instant.astimezone(zone).replace(tzinfo=None) != local:
            continue
        if is_dst and candidate.dst() == timedelta(0):
            continue
        candidates.add(instant)
    return next(iter(candidates)) if len(candidates) == 1 else None


def parse_energy_balance(payload: dict, *, plant_id: str, day: date,
                         timezone_name: str, fetched_at: datetime | None = None,
                         units: dict[str, str] | None = None) -> EnergyBalanceDay:
    """Convert one daily response to queryable data, retaining source readings."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("stationDn") != plant_id:
        raise EnergyBalanceValidationError("Response data must match the requested plant.")
    if (payload.get("success") in (False, "false", "False") or
            payload.get("failCode") not in (None, 0, "0") or payload.get("failReason")):
        raise EnergyBalanceValidationError("An unsuccessful API response cannot be stored.")
    labels = data.get("xAxis")
    if not isinstance(labels, list) or not labels or any(not isinstance(x, str) or not x.strip() for x in labels):
        raise EnergyBalanceValidationError("Response requires nonempty data.xAxis labels.")
    zone = ZoneInfo(timezone_name)
    timestamps = [_timestamp(label, zone, day) for label in labels]
    units = units if units is not None else {}
    if not isinstance(units, dict) or any(not isinstance(u, str) or not u.strip() for u in units.values()):
        raise EnergyBalanceValidationError("Verified units must be a mapping of names to nonempty unit strings.")
    samples, metrics, available, empty, metadata = [], [], [], [], {}
    for name, values in data.items():
        if name in ("stationDn", "xAxis"):
            continue
        if name in DAILY_METRIC_FIELDS:
            value, original = _reading(values, name)
            metrics.append(DailyMetric(name, value, original, units.get(name)))
        elif isinstance(values, list):
            if not values:
                empty.append(name)
                continue
            if len(values) != len(labels):
                raise EnergyBalanceValidationError(f"{name} does not align with data.xAxis.")
            available.append(name)
            for index, raw in enumerate(values):
                value, original = _reading(raw, name)
                samples.append(MeasurementSample(index, labels[index], timestamps[index], name,
                                                 value, original, units.get(name)))
        elif name in SERIES_FIELDS:
            raise EnergyBalanceValidationError(f"{name} must be a measurement array.")
        elif values is None or isinstance(values, (str, bool, int, float)):
            if isinstance(values, float) and not isfinite(values):
                raise EnergyBalanceValidationError(f"{name} contains nonfinite metadata.")
            metadata[name] = deepcopy(values)
        else:
            raise EnergyBalanceValidationError(f"{name} contains unsupported metadata.")
    result = EnergyBalanceDay(plant_id, day, timezone_name, fetched_at or datetime.now(timezone.utc),
                              len(labels), available, empty, samples, metrics, metadata)
    try:
        result.validate()
    except ValueError as exc:
        raise EnergyBalanceValidationError(str(exc)) from None
    return result
