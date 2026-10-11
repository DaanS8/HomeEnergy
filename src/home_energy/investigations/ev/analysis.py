"""Consumption-only EV hypotheses, independent of fetching, storage and plotting.

Power readings are assumed to represent the following sampling interval (zero
order hold). Unknown readings/gaps break runs; they are never interpolated.
Regions are detected across midnight before daily energy is apportioned.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ...usage.models import EnergyBalanceDay
from ...usage.ev import find_regions
from .calibration import EVLoadEstimation, estimate_loads


@dataclass(frozen=True)
class EVDetectionConfig:
    threshold_kw: float = 2.13
    minimum_minutes: float = 120.0
    block_quantile: float = 0.1
    baseline_quantile: float = 0.1
    stability_absolute_kw: float = 0.75
    stability_relative: float = 0.25

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isfinite(value):
                raise ValueError(f"{name} must be finite.")
        if self.threshold_kw <= 0 or self.minimum_minutes <= 0:
            raise ValueError("Threshold and minimum duration must be positive.")
        if not 0 <= self.block_quantile <= 1 or not 0 < self.baseline_quantile < 0.5:
            raise ValueError("Invalid block or baseline quantile.")
        if min(self.stability_absolute_kw, self.stability_relative) < 0:
            raise ValueError("Stability tolerances cannot be negative.")


@dataclass(frozen=True)
class ChargingRegion:
    region_id: int
    start: datetime
    end: datetime  # exclusive
    sample_count: int
    duration_hours: float
    total_q10_kw: float
    total_median_kw: float
    total_q90_kw: float
    total_mean_kw: float
    block_total_kw: float
    baseline_kw: float
    quantile_ev_kw: float
    minimum_total_kw: float
    ev_kw: float
    block_limited: bool
    local_background_kw: float | None
    local_background_samples: int
    ev_above_local_background_kw: float | None
    spread_kw: float
    consistent: bool
    left_censored: bool
    right_censored: bool

    @property
    def censored(self):
        return self.left_censored or self.right_censored


@dataclass
class EVInvestigation:
    start: date
    end: date
    timezone: str
    config: EVDetectionConfig
    samples: pd.DataFrame
    regions: list[ChargingRegion]
    daily: pd.DataFrame
    baseline_kw: float
    shared_ev_kw: float | None
    shared_threshold_kw: float | None
    shared_regions: list[ChargingRegion]
    summary: dict
    estimation: EVLoadEstimation


def consumption_frame(days: list[EnergyBalanceDay], *, unit: str = "kW") -> pd.DataFrame:
    """One UTC timestamp per row; retain null readings and derive actual cadence.

    The caller explicitly declares the source power unit when metadata is absent.
    Source Decimals remain in PostgreSQL; float conversion is analysis-only.
    """
    if unit not in ("kW", "W"):
        raise ValueError("Consumption power unit must be kW or W.")
    if not days or len({d.plant_id for d in days}) != 1 or len({d.timezone for d in days}) != 1:
        raise ValueError("Analysis requires days from one plant and one timezone.")
    records = []
    for day in days:
        source = [s for s in day.samples if s.measurement == "usePower"]
        if not source:
            raise ValueError(f"No total consumption (usePower) for {day.day}.")
        for sample in source:
            if sample.observed_at is None:
                raise ValueError(f"Unresolved consumption timestamp on {day.day}: {sample.axis_label}.")
            if sample.unit is not None and sample.unit != unit:
                raise ValueError(f"Consumption unit {sample.unit!r} conflicts with declared {unit!r}.")
            value = float(sample.value) if sample.value is not None else np.nan
            if value < 0:
                raise ValueError("Total consumption cannot be negative.")
            records.append((sample.observed_at, day.day, sample.sample_index,
                            sample.axis_label, value / (1000 if unit == "W" else 1)))
    frame = pd.DataFrame(records, columns=["timestamp", "source_date", "sample_index", "axis_label", "total_kw"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").set_index("timestamp")
    if frame.index.has_duplicates:
        raise ValueError("Consumption timestamps must be unique.")
    differences = frame.index.to_series().diff().dt.total_seconds().dropna()
    if differences.empty:
        raise ValueError("At least two consumption timestamps are required.")
    cadence = float(differences.mode().min())
    if cadence <= 0 or cadence > 3600:
        raise ValueError("Cannot establish a suitable sampling cadence.")
    # Missing timestamps become explicit NaNs rather than extending the previous
    # reading over a hole. Reject irregular/off-grid source times.
    grid = pd.date_range(frame.index.min(), frame.index.max(), freq=pd.Timedelta(seconds=cadence))
    if not frame.index.isin(grid).all():
        raise ValueError("Consumption timestamps are not on one regular sampling grid.")
    frame = frame.reindex(grid)
    frame.index.name = "timestamp"
    frame["interval_end"] = frame.index + pd.Timedelta(seconds=cadence)
    frame["duration_hours"] = cadence / 3600
    frame["observed"] = frame["total_kw"].notna()
    frame.attrs["cadence_seconds"] = cadence
    return frame


def day_bounds(day: date, timezone: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    zone = ZoneInfo(timezone)
    # Construct separate local midnights: a DST day is not necessarily 24h.
    return (pd.Timestamp(datetime.combine(day, time(), zone)).tz_convert("UTC"),
            pd.Timestamp(datetime.combine(day + timedelta(days=1), time(), zone)).tz_convert("UTC"))


def _mask(frame, threshold):
    return frame["total_kw"].gt(threshold).to_numpy()


def detect_regions(frame: pd.DataFrame, config: EVDetectionConfig,
                   baseline_kw: float, *, threshold_kw: float | None = None) -> list[ChargingRegion]:
    """Each uninterrupted above-threshold run is independent; no gap bridging.

    Consistency is a diagnostic, not a rejection rule: a noisy background can
    sit on a constant EV draw. Censored regions are flagged for review.
    """
    threshold = config.threshold_kw if threshold_kw is None else threshold_kw
    mask = _mask(frame, threshold)
    regions = []
    if frame.empty:
        return regions
    runs = find_regions(zip(frame.index, frame["total_kw"]), threshold_kw=threshold,
                        sample_interval=timedelta(seconds=frame.attrs["cadence_seconds"]),
                        minimum_duration=timedelta(minutes=config.minimum_minutes))
    for start, end in runs:
        first, stop = frame.index.searchsorted([start, end])
        run = frame.iloc[first:stop]
        duration = (end - start).total_seconds() / 3600
        values = run["total_kw"]
        q10, median, q90 = (float(values.quantile(q)) for q in (0.1, 0.5, 0.9))
        block = float(values.quantile(config.block_quantile))
        quantile_ev = max(0.0, block - baseline_kw)
        minimum_total = float(values.min())
        # A quantile is not a lower bound. Preserve the quantile estimate but
        # constrain the constant EV block to fit every observed interval.
        ev = min(quantile_ev, minimum_total)
        # Local background is the lower quantile of OFF samples in the adjacent
        # hour on both sides. Exclude all high runs, even those shorter than 1h.
        adjacent = ((frame.index >= start - pd.Timedelta(hours=1)) & (frame.index < start)) | (
            (frame.index >= end) & (frame.index < end + pd.Timedelta(hours=1)))
        background = frame.loc[adjacent & ~mask, "total_kw"].dropna()
        local = float(background.quantile(config.baseline_quantile)) if len(background) >= 6 else None
        left_censored = first == 0 or not frame["observed"].iloc[first - 1]
        right_censored = stop == len(frame) or not frame["observed"].iloc[stop]
        spread = q90 - q10
        regions.append(ChargingRegion(
            len(regions) + 1, start, end, len(run), duration,
            q10, median, q90, float(values.mean()), block, baseline_kw, quantile_ev, minimum_total,
            ev, ev < quantile_ev,
            local, len(background), max(0.0, block - local) if local is not None else None,
            spread, spread <= max(config.stability_absolute_kw, config.stability_relative * q10),
            bool(left_censored), bool(right_censored),
        ))
    return regions


def _overlaps(region: ChargingRegion, start: pd.Timestamp, end: pd.Timestamp) -> bool:
    return region.start < end and region.end > start


def _assign_blocks(frame, regions, *, shared_kw=None, prefix=""):
    frame[prefix + "region_id"] = pd.Series(pd.NA, index=frame.index, dtype="Int64")
    frame[prefix + "ev_kw"] = 0.0
    for region in regions:
        mask = (frame.index >= region.start) & (frame.index < region.end)
        frame.loc[mask, prefix + "region_id"] = region.region_id
        frame.loc[mask, prefix + "ev_kw"] = region.ev_kw if shared_kw is None else shared_kw
    # Unknown consumption means unknown charging status, not OFF.
    frame.loc[~frame["observed"], prefix + "ev_kw"] = np.nan


def investigate(days: list[EnergyBalanceDay], start: date, end: date, *,
                config: EVDetectionConfig | None = None, unit="kW") -> EVInvestigation:
    if start > end:
        raise ValueError("Range start must not be after its end.")
    config = config or EVDetectionConfig()
    timezone = days[0].timezone if days else "Europe/Brussels"
    first, _ = day_bounds(start, timezone)
    _, last = day_bounds(end, timezone)
    frame = consumption_frame(days, unit=unit)
    # Keep complete fetched days for estimating full cross-midnight regions;
    # Boundary context tracks the qualifying duration, including custom values.
    context = pd.Timedelta(minutes=config.minimum_minutes)
    required = pd.date_range(first - context, last + context,
                             freq=pd.Timedelta(seconds=frame.attrs["cadence_seconds"]), inclusive="left")
    missing_context = required.difference(frame.index)
    if len(missing_context):
        raise ValueError(f"Missing boundary/day timestamps. Fetch at least {config.minimum_minutes:g} minutes "
                         "before the first day and after the last day.")
    target = frame.loc[(frame.index >= first) & (frame.index < last)]
    if target["total_kw"].notna().sum() == 0:
        raise ValueError("No observed consumption in the requested range.")
    baseline = float(target["total_kw"].quantile(config.baseline_quantile))
    regions = [r for r in detect_regions(frame, config, baseline) if _overlaps(r, first, last)]
    _assign_blocks(frame, regions)
    calibration = [r for r in regions if r.consistent and not r.censored
                   and r.ev_above_local_background_kw is not None]
    reference_shared = float(np.mean([r.ev_above_local_background_kw for r in calibration])) if calibration else None
    estimation = estimate_loads(frame, regions, start=start, end=end, timezone=timezone,
                                seed_threshold_kw=config.threshold_kw,
                                cadence_seconds=frame.attrs["cadence_seconds"])
    shared = estimation.ev_kw
    shared_threshold = shared
    shared_regions = ([r for r in detect_regions(frame, config, baseline, threshold_kw=shared_threshold)
                       if _overlaps(r, first, last)] if shared_threshold is not None else [])
    _assign_blocks(frame, shared_regions, shared_kw=shared, prefix="shared_")
    rows = []
    day_map = {d.day: d for d in days}
    selected = start
    while selected <= end:
        lo, hi = day_bounds(selected, timezone)
        part = frame.loc[(frame.index >= lo) & (frame.index < hi)]
        valid = part["total_kw"].dropna()
        touching = [r for r in regions if _overlaps(r, lo, hi)]
        ev_energy = sum(max(0.0, (min(pd.Timestamp(r.end), hi) - max(pd.Timestamp(r.start), lo)).total_seconds())
                        / 3600 * r.ev_kw for r in touching)
        rows.append({
            "date": selected, "coverage_fraction": float(part["observed"].mean()),
            "sampled_total_kwh": float((part["total_kw"] * part["duration_hours"]).sum()),
            "api_total_kwh": float(day_map[selected].consumed_energy) if selected in day_map
                and day_map[selected].consumed_energy is not None else None,
            "daily_q10_baseline_kw": float(valid.quantile(config.baseline_quantile)) if len(valid) else None,
            "region_count": len(touching),
            "charging_hours": sum((min(pd.Timestamp(r.end), hi) - max(pd.Timestamp(r.start), lo)).total_seconds()
                                  / 3600 for r in touching),
            "ev_kwh": ev_energy,
            "shared_ev_kwh": float((part["shared_ev_kw"] * part["duration_hours"]).sum()) if shared is not None else None,
            "preferred_ev_kwh": float((part["shared_ev_kw"] * part["duration_hours"]).sum()) if shared is not None else None,
            "preferred_charging_hours": float(part.loc[part["shared_region_id"].notna(), "duration_hours"].sum())
                if shared is not None else None,
            "preferred_region_count": sum(_overlaps(r, lo, hi) for r in shared_regions) if shared is not None else None,
        })
        selected += timedelta(days=1)
    daily = pd.DataFrame(rows)
    cv = (estimation.summary["ending_std_kw"] / shared
          if estimation.summary["ending_std_kw"] is not None and shared else None)
    observed_ev = np.concatenate([frame.loc[(frame.index >= r.start) & (frame.index < r.end), "total_kw"].to_numpy()
                                  - baseline for r in regions]) if regions else np.array([])
    shared_observed = frame.loc[frame["shared_region_id"].notna(), "total_kw"].to_numpy()
    both_on = frame["region_id"].notna() & frame["shared_region_id"].notna()
    either_on = frame["region_id"].notna() | frame["shared_region_id"].notna()
    summary = {
        "date_start": start.isoformat(), "date_end": end.isoformat(), "timezone": timezone,
        "source_measurement": "usePower", "declared_source_unit": unit,
        "source_unit_verified": all(s.unit == unit for d in days for s in d.samples if s.measurement == "usePower"),
        "config": asdict(config), "cadence_seconds": frame.attrs["cadence_seconds"],
        "requested_days": len(daily), "regions": len(regions),
        "consistent_regions": sum(r.consistent for r in regions),
        "limited_blocks": sum(r.block_limited for r in regions),
        "censored_regions": sum(r.censored for r in regions),
        "baseline_kw": baseline, "estimated_ev_kwh": float(daily["ev_kwh"].sum()),
        "charging_hours": float(daily["charging_hours"].sum()),
        "missing_target_samples": int(target["total_kw"].isna().sum()),
        "missing_required_context_samples": int(frame.reindex(required)["total_kw"].isna().sum()),
        "sampled_total_kwh": float(daily["sampled_total_kwh"].sum()),
        "api_total_kwh": float(daily["api_total_kwh"].sum()),
        "shared_ev_kw": shared, "shared_threshold_kw": shared_threshold,
        "shared_calibration_regions": estimation.summary["accepted_endings"], "shared_load_coefficient_of_variation": cv,
        "reference_shared_ev_kw": reference_shared,
        "preferred_method": estimation.summary["method"] if shared is not None else "unavailable",
        "quiet_night_baseload_kw": estimation.baseline_kw,
        "shared_regions": len(shared_regions),
        "shared_estimated_ev_kwh": float(daily["shared_ev_kwh"].sum()) if shared is not None else None,
        "preferred_estimated_ev_kwh": float(daily["shared_ev_kwh"].sum()) if shared is not None else None,
        "preferred_charging_hours": float(daily["preferred_charging_hours"].sum()) if shared is not None else None,
        "preferred_regions": len(shared_regions) if shared is not None else None,
        "shared_mask_jaccard": float(both_on.sum() / either_on.sum()) if either_on.any() and shared is not None else None,
        "shared_block_exceeds_observed_fraction": float(np.mean(shared_observed < shared))
            if len(shared_observed) and shared is not None else None,
        "shared_block_exceeds_baseline_adjusted_fraction": float(np.mean(observed_ev < shared))
            if len(observed_ev) and shared is not None else None,
    }
    return EVInvestigation(start, end, timezone, config, frame, regions, daily,
                           baseline, shared, shared_threshold, shared_regions, summary, estimation)
