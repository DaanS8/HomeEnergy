"""Offline calibration of EV power from guarded endings and quiet-night baseload.

Calibration uses fixed reference candidates, independently of the fitted power.
No startup samples contribute. Missing samples are never filled or interpolated.
"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class EndingCalibrationConfig:
    window_minutes: int = 10
    guard_samples: int = 2
    maximum_window_spread_kw: float = 0.2
    bootstrap_repetitions: int = 5000
    random_seed: int = 20261011
    night_start_hour: int = 0
    night_end_hour: int = 6
    minimum_night_samples: int = 12
    exclusion_margin_minutes: int = 30


@dataclass
class EVLoadEstimation:
    config: EndingCalibrationConfig
    endings: pd.DataFrame
    variants: pd.DataFrame
    sensitivity: pd.DataFrame
    nights: pd.DataFrame
    ev_kw: float | None
    baseline_kw: float | None
    summary: dict


def _bootstrap_interval(values, config, statistic=np.mean):
    values = np.asarray(values, dtype=float)
    if len(values) < 3:
        return None, None
    rng = np.random.default_rng(config.random_seed)
    estimates = statistic(rng.choice(values, size=(config.bootstrap_repetitions, len(values)), replace=True), axis=1)
    return tuple(float(x) for x in np.quantile(estimates, [0.025, 0.975]))


def ending_table(samples, regions, *, cadence_seconds, window_minutes, guard_samples,
                 timezone, maximum_spread_kw):
    count = window_minutes * 60 / cadence_seconds
    if count < 2 or not float(count).is_integer():
        raise ValueError("Ending windows require at least two readings and must match the source cadence.")
    count = int(count)
    before_skip = guard_samples // 2
    after_skip = guard_samples - before_skip
    rows = []
    for region in regions:
        stop = pd.Timestamp(region.end)
        before_offsets = range(-before_skip - count, -before_skip)
        after_offsets = range(after_skip, after_skip + count)
        pre_times = pd.DatetimeIndex([stop + pd.Timedelta(seconds=i * cadence_seconds) for i in before_offsets])
        post_times = pd.DatetimeIndex([stop + pd.Timedelta(seconds=i * cadence_seconds) for i in after_offsets])
        pre = samples.reindex(pre_times)["total_kw"].to_numpy()
        post = samples.reindex(post_times)["total_kw"].to_numpy()
        complete = bool(np.isfinite(pre).all() and np.isfinite(post).all())
        before = float(pre.mean()) if np.isfinite(pre).all() else None
        after = float(post.mean()) if np.isfinite(post).all() else None
        spread_before = float(np.ptp(pre)) if np.isfinite(pre).all() else None
        spread_after = float(np.ptp(post)) if np.isfinite(post).all() else None
        ev = before - after if complete else None
        reasons = []
        if region.censored:
            reasons.append("censored_region")
        if not complete:
            reasons.append("missing_window_samples")
        if complete:
            if spread_before > maximum_spread_kw:
                reasons.append("unstable_before")
            if spread_after > maximum_spread_kw:
                reasons.append("unstable_after")
            if ev <= 0:
                reasons.append("nonpositive_drop")
            # A short high-power household load ending inside a lower plateau
            # must not calibrate the alleged constant EV component.
            if ev > region.total_q10_kw:
                reasons.append("drop_exceeds_region_q10")
        rows.append({
            "region_id": region.region_id, "end_utc": stop,
            "end_local": stop.tz_convert(timezone),
            "nighttime_end": 0 <= stop.tz_convert(timezone).hour < 8,
            "window_minutes": window_minutes, "guard_samples": guard_samples,
            "before_first_utc": pre_times[0], "before_last_utc": pre_times[-1],
            "after_first_utc": post_times[0], "after_last_utc": post_times[-1],
            "before_kw": before, "after_background_kw": after, "ev_kw": ev,
            "before_spread_kw": spread_before, "after_spread_kw": spread_after,
            "complete": complete, "accepted": not reasons,
            "rejection_reason": ";".join(reasons),
        })
    return pd.DataFrame(rows, columns=["region_id", "end_utc", "end_local", "nighttime_end",
        "window_minutes", "guard_samples", "before_first_utc", "before_last_utc", "after_first_utc",
        "after_last_utc", "before_kw", "after_background_kw", "ev_kw", "before_spread_kw", "after_spread_kw",
        "complete", "accepted", "rejection_reason"])


def estimate_loads(samples, regions, *, start, end, timezone, seed_threshold_kw,
                   cadence_seconds, config=None):
    config = config or EndingCalibrationConfig()
    variants, sensitivities = [], []
    default = None
    for window in sorted({10, 15, 20, config.window_minutes}):
        for guard in sorted({1, 2, config.guard_samples}):
            if window * 60 / cadence_seconds < 2 or not float(window * 60 / cadence_seconds).is_integer():
                continue
            table = ending_table(samples, regions, cadence_seconds=cadence_seconds,
                                 window_minutes=window, guard_samples=guard, timezone=timezone,
                                 maximum_spread_kw=config.maximum_window_spread_kw)
            accepted = table.loc[table["accepted"].astype(bool), "ev_kw"].to_numpy(dtype=float)
            low, high = _bootstrap_interval(accepted, config)
            sensitivities.append({"window_minutes": window, "guard_samples": guard,
                "accepted_endings": len(accepted), "rejected_endings": len(table) - len(accepted),
                "mean_ev_kw": float(np.mean(accepted)) if len(accepted) else None,
                "bootstrap_low_kw": low, "bootstrap_high_kw": high})
            variants.append(table)
            if window == config.window_minutes and guard == config.guard_samples:
                default = table
    if default is None:
        raise ValueError("Source cadence is incompatible with the default ending calibration windows.")
    accepted = default.loc[default["accepted"].astype(bool), "ev_kw"].to_numpy(dtype=float)
    mean_power = float(np.mean(accepted)) if len(accepted) else None
    # Use the same three-decimal power as the daily API for detection and energy.
    power = round(mean_power, 3) if mean_power is not None else None
    low, high = _bootstrap_interval(accepted, config)
    sensitivity = pd.DataFrame(sensitivities)
    window_powers = sensitivity["mean_ev_kw"].dropna()

    # Equal weight per local night. Exclude candidate sessions and their nearby
    # context, as well as short high-power events; these are candidate-free,
    # not independently confirmed EV-off observations.
    local = samples.index.tz_convert(timezone)
    target = (local.date >= start) & (local.date <= end)
    excluded = np.zeros(len(samples), dtype=bool)
    for region in regions:
        excluded |= ((samples.index >= pd.Timestamp(region.start) - pd.Timedelta(minutes=config.exclusion_margin_minutes))
                     & (samples.index < pd.Timestamp(region.end) + pd.Timedelta(minutes=config.exclusion_margin_minutes)))
    quiet = samples.loc[target & ~excluded & (local.hour >= config.night_start_hour)
                        & (local.hour < config.night_end_hour) & samples["total_kw"].le(seed_threshold_kw), ["total_kw"]].copy()
    quiet["date"] = quiet.index.tz_convert(timezone).date
    night_rows = []
    for selected, part in quiet.groupby("date"):
        count = len(part)
        night_rows.append({"date": selected, "off_samples": count, "eligible": count >= config.minimum_night_samples,
                           **{f"q{int(q * 100):02d}_kw": float(part["total_kw"].quantile(q)) for q in (0.05, 0.1, 0.2)}})
    nights = pd.DataFrame(night_rows, columns=["date", "off_samples", "eligible", "q05_kw", "q10_kw", "q20_kw"])
    eligible = nights.loc[nights["eligible"].astype(bool)]
    baseline = float(eligible["q10_kw"].median()) if len(eligible) else None
    baseline_low, baseline_high = _bootstrap_interval(eligible["q10_kw"].to_numpy(), config, statistic=np.median)

    # Fit earlier complete sessions and compare with later independent sessions.
    # This evaluates ending agreement, not charger-ground-truth accuracy.
    ordered = default.sort_values("end_utc")
    split = int(np.ceil(len(ordered) * 2 / 3))
    train = ordered.iloc[:split].loc[lambda x: x["accepted"].astype(bool), "ev_kw"]
    test = ordered.iloc[split:].loc[lambda x: x["accepted"].astype(bool), "ev_kw"]
    fit = float(train.mean()) if len(train) else None
    quality_sensitivity = []
    for tolerance in (0.1, 0.2, 0.3):
        variant = ending_table(samples, regions, cadence_seconds=cadence_seconds,
                               window_minutes=config.window_minutes, guard_samples=config.guard_samples,
                               timezone=timezone, maximum_spread_kw=tolerance)
        values = variant.loc[variant["accepted"].astype(bool), "ev_kw"]
        quality_sensitivity.append({"maximum_window_spread_kw": tolerance, "accepted_endings": len(values),
                                    "mean_ev_kw": float(values.mean()) if len(values) else None})
    summary = {
        "method": "mean_clean_stop_drop", "parameters": asdict(config),
        "reference_candidates": len(regions), "accepted_endings": len(accepted),
        "rejected_endings": len(default) - len(accepted), "ev_kw": power, "mean_ev_kw": mean_power,
        "bootstrap_95pct_low_kw": low, "bootstrap_95pct_high_kw": high,
        "window_sensitivity_low_kw": float(window_powers.min()) if len(window_powers) else None,
        "window_sensitivity_high_kw": float(window_powers.max()) if len(window_powers) else None,
        "ending_std_kw": float(np.std(accepted, ddof=1)) if len(accepted) > 1 else None,
        "nighttime_accepted_endings": int((default["accepted"].astype(bool) & default["nighttime_end"].astype(bool)).sum()),
        "nighttime_ev_kw": float(default.loc[default["accepted"].astype(bool) & default["nighttime_end"].astype(bool), "ev_kw"].mean())
            if (default["accepted"].astype(bool) & default["nighttime_end"].astype(bool)).any() else None,
        "baseload_kw": baseline, "eligible_nights": len(eligible),
        "baseload_bootstrap_95pct_low_kw": baseline_low, "baseload_bootstrap_95pct_high_kw": baseline_high,
        "baseload_q05_kw": float(eligible["q05_kw"].median()) if len(eligible) else None,
        "baseload_q20_kw": float(eligible["q20_kw"].median()) if len(eligible) else None,
        "earlier_accepted_endings": len(train), "later_accepted_endings": len(test),
        "earlier_fit_ev_kw": fit,
        "later_ending_mae_kw": float(np.mean(np.abs(test.to_numpy() - fit))) if fit is not None and len(test) else None,
        "quality_sensitivity": quality_sensitivity,
    }
    return EVLoadEstimation(config, default, pd.concat(variants, ignore_index=True), sensitivity,
                            nights, power, baseline, summary)
