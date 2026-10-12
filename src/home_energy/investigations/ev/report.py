"""Export reproducible EV investigation tables, figures, PDF and a local index."""

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib.backends.backend_pdf import PdfPages
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ...charts import COLORS, chart_style
from .analysis import ChargingRegion, EVDetectionConfig, EVInvestigation, day_bounds, investigate
from ...usage.repository import EnergyBalanceRepository
from ...usage.ingestion import UsageIngestionService


def load_investigation_days(repository, start, end, *, fetch=False):
    """Load completed stored days, including both adjacent context days."""
    previous, following = start - timedelta(days=1), end + timedelta(days=1)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Brussels")).date()
    if start > end or following >= today:
        raise ValueError("Investigate dates at least two days ago so both context days have finished.")
    if fetch:
        UsageIngestionService(repository).fill(previous, following,
                        on_result=lambda r: print(f"{r.day}: {'stored' if r.inserted else 'already stored'}", flush=True))
    days = repository.load_range(previous, following)
    present = {d.day for d in days}
    required = {previous + timedelta(days=i) for i in range((following - previous).days + 1)}
    missing = required - present
    if missing:
        raise ValueError(f"Missing stored days: {', '.join(map(str, sorted(missing)))}. Run with --fetch.")
    return days


def _subtitle(fig, text):
    fig.text(0.08, 0.935, text, color=COLORS["text_secondary"], fontsize=9)


def daily_figure(result: EVInvestigation, day: date):
    lo, hi = day_bounds(day, result.timezone)
    frame = result.samples.loc[(result.samples.index >= lo) & (result.samples.index < hi)]
    x = frame.index.tz_convert(result.timezone)
    figures = plt.subplots(2, 1, figsize=(12, 6), sharex=True,
                          gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08})
    fig, (ax, state) = figures
    row = result.daily.loc[result.daily["date"] == day].iloc[0]
    preferred = result.shared_ev_kw is not None
    floor = result.estimation.baseline_kw if result.estimation.baseline_kw is not None else result.baseline_kw
    interval_x = x.append(pd.DatetimeIndex([frame["interval_end"].iloc[-1].tz_convert(result.timezone)]))
    ax.step(interval_x, np.r_[frame["total_kw"], frame["total_kw"].iloc[-1]], where="post",
            color=COLORS["primary"], linewidth=1.6, label="Total consumption")
    ax.axhline(floor, color=COLORS["muted"], linewidth=1, linestyle="--",
               label=f"Quiet-night baseload proxy: {floor:.2f} kW")
    trigger = result.shared_threshold_kw if preferred else result.config.threshold_kw
    ax.axhline(trigger, color=COLORS["warning"], linewidth=0.8, alpha=0.6, linestyle=":",
               label=f"EV trigger: > {trigger:.3f} kW for >= {result.config.minimum_minutes:g} min")
    ax.set_ylabel("Power (kW)")
    ax.set_ylim(bottom=0)
    for region in result.regions:
        if region.start >= hi or region.end <= lo:
            continue
        begin, finish = max(pd.Timestamp(region.start), lo).tz_convert(result.timezone), min(pd.Timestamp(region.end), hi).tz_convert(result.timezone)
        ax.plot([begin, finish], [region.ev_kw] * 2, color=COLORS["muted"], linewidth=1, linestyle=":")
    shown_regions = result.shared_regions if preferred else result.regions
    for region in shown_regions:
        if region.start >= hi or region.end <= lo:
            continue
        begin = max(pd.Timestamp(region.start), lo).tz_convert(result.timezone)
        finish = min(pd.Timestamp(region.end), hi).tz_convert(result.timezone)
        height = result.shared_ev_kw if preferred else region.ev_kw
        color = COLORS["accent"] if region.consistent else COLORS["warning"]
        ax.fill_between([begin, finish], height, 0, color=color, alpha=0.27, linewidth=0)
        ax.plot([begin, finish], [height] * 2, color=color, linewidth=1.8)
        state.fill_between([begin, finish], 1, 0, color=color, alpha=0.65, linewidth=0)
        ax.text(begin + (finish - begin) / 2, height + 0.12, f"#{region.region_id}", color=color, ha="center", fontsize=8)
    if preferred:
        # The shared block must use its own EV-level trigger, not a looser mask.
        shared_values = frame["shared_ev_kw"].to_numpy()
        ax.step(interval_x, np.r_[shared_values, shared_values[-1]], where="post", linewidth=1,
                linestyle="--", color=COLORS["secondary"],
                label=f"Ending-calibrated EV block: {result.shared_ev_kw:.3f} kW")
    state.set_yticks([0, 1], ["Off", "On"])
    state.set_ylabel("EV hypothesis")
    state.set_ylim(0, 1.25)
    state.set_xlim(lo.tz_convert(result.timezone), hi.tz_convert(result.timezone))
    state.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 4, 8, 12, 16, 20], tz=result.timezone))
    state.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=result.timezone))
    state.set_xlabel(f"Local time ({result.timezone})")
    for stamp in frame.index[~frame["observed"]]:
        state.axvspan(stamp, stamp + pd.Timedelta(seconds=result.summary["cadence_seconds"]),
                      color=COLORS["error"], alpha=0.5)
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    fig.suptitle(f"Consumption and candidate EV regions | {day}", x=0.08, ha="left", y=0.985, color=COLORS["text"])
    energy = row.shared_ev_kwh if preferred else row.ev_kwh
    on_column = "shared_region_id" if preferred else "region_id"
    hours = float(frame.loc[frame[on_column].notna(), "duration_hours"].sum())
    model_caption = ("Colored blocks: ending-calibrated model | Slate dotted lines: reference percentile blocks"
                     if preferred else "Calibration unavailable | Colored blocks: reference percentile model")
    _subtitle(fig, f"EV estimate {energy:.1f} kWh | {hours:.2f} h on this day | "
              + model_caption)
    fig.subplots_adjust(top=0.87, bottom=0.12, left=0.08, right=0.98)
    return fig


def overview_figure(result):
    fig, axes = plt.subplots(2, 1, figsize=(13, 11), sharex=True)
    # A 5-minute local-clock heatmap is for visual comparison only; calculations
    # retain UTC timestamps and actual elapsed durations.
    bins = 288
    power = np.full((len(result.daily), bins), np.nan)
    on = np.full_like(power, np.nan)
    for i, day in enumerate(result.daily["date"]):
        lo, hi = day_bounds(day, result.timezone)
        part = result.samples.loc[(result.samples.index >= lo) & (result.samples.index < hi)]
        for stamp, row in part.iterrows():
            local = stamp.tz_convert(result.timezone)
            col = (local.hour * 60 + local.minute) // 5
            power[i, col] = row.total_kw
            selected_id = row.shared_region_id if result.shared_ev_kw is not None else row.region_id
            on[i, col] = (1.0 if pd.notna(selected_id) else 0.0) if row.observed else np.nan
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("consumption", [COLORS["background"], COLORS["primary"], COLORS["accent"]])
    state_cmap = LinearSegmentedColormap.from_list("ev_state", [COLORS["background"], COLORS["accent"]])
    im = axes[0].imshow(power, aspect="auto", cmap=cmap, vmin=0, vmax=float(np.nanquantile(power, 0.99)),
                        extent=[0, 24, len(power), 0], interpolation="nearest")
    axes[1].imshow(on, aspect="auto", cmap=state_cmap, vmin=0, vmax=1,
                   extent=[0, 24, len(power), 0], interpolation="nearest")
    state_title = "Ending-calibrated EV on/off regions" if result.shared_ev_kw is not None else "Reference EV on/off regions"
    for ax, title in zip(axes, ["Total consumption (kW)", state_title]):
        ax.set_title(title, loc="left")
        ax.set_yticks(np.arange(len(power)) + 0.5, [str(d)[5:] for d in result.daily["date"]], fontsize=7)
        ax.grid(False)
    axes[1].set_xticks(range(0, 25, 3), [f"{h:02d}:00" for h in range(0, 25, 3)])
    axes[1].set_xlabel(f"Local time ({result.timezone})")
    fig.colorbar(im, ax=axes[0], fraction=0.015, pad=0.01)
    fig.suptitle(f"EV investigation | {result.start} to {result.end}", x=0.08, ha="left", color=COLORS["text"])
    fig.subplots_adjust(top=0.93, bottom=0.06, left=0.08, right=0.94, hspace=0.17)
    return fig


def region_profile(result, region):
    """Actual UTC intervals for a full region plus exactly one hour each side.

    Missing context remains NaN. Both elapsed-time coordinates refer to the same
    source readings; only charging intervals have a normalized 0-100% position.
    """
    start, end = pd.Timestamp(region.start), pd.Timestamp(region.end)
    grid = pd.date_range(start - pd.Timedelta(hours=1), end + pd.Timedelta(hours=1),
                         freq=pd.Timedelta(seconds=result.summary["cadence_seconds"]), inclusive="left")
    frame = result.samples.reindex(grid)[["total_kw"]].copy()
    frame.index.name = "timestamp"
    frame.insert(0, "region_id", region.region_id)
    frame["section"] = np.where(grid < start, "before", np.where(grid < end, "charging", "after"))
    frame["hours_from_start"] = (grid - start).total_seconds() / 3600
    frame["hours_from_end"] = (grid - end).total_seconds() / 3600
    frame["normalized_percent"] = np.where(frame["section"] == "charging",
                                            frame["hours_from_start"] / region.duration_hours * 100, np.nan)
    frame["total_minus_baseline_kw"] = frame["total_kw"] - result.baseline_kw
    frame["total_minus_region_quantile_kw"] = frame["total_kw"] - region.block_total_kw
    return frame


def normalized_region_parts(result, region):
    """Three distinct axes: real pre-minutes, normalized charging, real post-minutes.

    Use held readings, preserving gaps and discontinuities. The right edge of
    each section holds its final interval; it never borrows the next section.
    """
    profile = region_profile(result, region)
    sections = []
    cadence_minutes = result.summary["cadence_seconds"] / 60
    for section, coordinate, grid in (
        ("before", "hours_from_start", np.r_[np.arange(-60, 0, cadence_minutes), 0]),
        ("charging", "normalized_percent", np.linspace(0, 100, 101)),
        ("after", "hours_from_end", np.r_[np.arange(0, 60, cadence_minutes), 60]),
    ):
        part = profile.loc[profile["section"] == section]
        source_x = part[coordinate].to_numpy() * (60 if section != "charging" else 1)
        indices = np.searchsorted(source_x, grid, side="right") - 1
        values = part[["total_minus_baseline_kw", "total_minus_region_quantile_kw"]].to_numpy()
        held = np.full((len(grid), 2), np.nan)
        valid = indices >= 0
        held[valid] = values[indices[valid]]
        sections.append((grid, held))
    return sections


def overlay_figure(result):
    fig, axes = plt.subplots(2, 3, figsize=(14, 7), sharey="row",
                             gridspec_kw={"width_ratios": [1, 3, 1]})
    curves = [[], [], []]
    consistent = [[], [], []]
    grids = [np.array([-60, 0]), np.linspace(0, 100, 101), np.array([0, 60])]
    for region in result.regions:
        for column, (grid, values) in enumerate(normalized_region_parts(result, region)):
            grids[column] = grid
            curves[column].append(values)
            if region.consistent:
                consistent[column].append(values[:, 0])
            for row in range(2):
                axes[row, column].step(grid, values[:, row], where="post",
                                       color=COLORS["primary"] if region.consistent else COLORS["warning"],
                                       alpha=0.25, linewidth=0.8)
    for column in range(3):
        if curves[column]:
            # Pandas quantiles ignore missing context without filling it in.
            for row in range(2):
                stack = pd.DataFrame(np.array(curves[column])[:, :, row])
                low, middle, high = (stack.quantile(q).to_numpy() for q in (0.1, 0.5, 0.9))
                ax = axes[row, column]
                ax.fill_between(grids[column], low, high, step="post", color=COLORS["accent"],
                                alpha=0.15, label="Across-region p10-p90")
                ax.step(grids[column], middle, where="post", color=COLORS["accent"], label="Across-region median")
        if consistent[column]:
            axes[0, column].step(grids[column], pd.DataFrame(consistent[column]).median().to_numpy(),
                                 where="post", color=COLORS["secondary"], linewidth=1.5,
                                 linestyle=":", label="Consistent-region median")
        if result.shared_ev_kw is not None and column == 1:
            axes[0, column].axhline(result.shared_ev_kw, linestyle="--", color=COLORS["secondary"], label="Shared block estimate")
        axes[1, column].axhline(0, color=COLORS["muted"], linewidth=1)
        for row in range(2):
            axes[row, column].set_xlim(grids[column][0], grids[column][-1])
    axes[0, 0].set_title("1 hour before", loc="left", fontsize=12)
    axes[0, 1].set_title("Normalized charging region", loc="left", fontsize=12)
    axes[0, 2].set_title("1 hour after", loc="left", fontsize=12)
    axes[0, 0].set_ylabel("Total minus baseline (kW)")
    axes[1, 0].set_ylabel("Total minus region quantile (kW)")
    for row in range(2):
        axes[row, 0].set_xticks([-60, -30, 0])
        axes[row, 1].set_xticks([0, 25, 50, 75, 100])
        axes[row, 2].set_xticks([0, 30, 60])
    axes[1, 0].set_xlabel("Minutes before start")
    axes[1, 1].set_xlabel("Region duration (%)")
    axes[1, 2].set_xlabel("Minutes after end")
    axes[0, 1].legend(fontsize=8, loc="upper right")
    fig.suptitle("Normalized regions with one hour of context", x=0.08, ha="left", color=COLORS["text"])
    _subtitle(fig, "Only the charging region is stretched | Side panels retain real minutes | Amber: variable total load")
    fig.subplots_adjust(top=0.85, bottom=0.1, left=0.08, right=0.98, hspace=0.2, wspace=0.12)
    return fig


def elapsed_overlay_figure(result):
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharey=True)
    longest = max((r.duration_hours for r in result.regions), default=2)
    for region in result.regions:
        profile = region_profile(result, region)
        for ax, coordinate in zip(axes, ("hours_from_start", "hours_from_end")):
            x, y = profile[coordinate].to_numpy(), profile["total_minus_baseline_kw"].to_numpy()
            ax.step(np.r_[x, x[-1] + result.summary["cadence_seconds"] / 3600], np.r_[y, y[-1]],
                    where="post", color=COLORS["primary"] if region.consistent else COLORS["warning"],
                    alpha=0.35, linewidth=1)
    for ax, side in zip(axes, ("start", "end")):
        ax.axvline(0, color=COLORS["accent"], linewidth=1)
        ax.set_title(f"All {side}s aligned at 0", loc="left", fontsize=12)
        ax.set_xlabel(f"Actual hours from detected {side}")
        ax.set_ylabel("Total minus baseline (kW)")
    axes[0].set_xlim(-1, longest + 1)
    axes[1].set_xlim(-longest - 1, 1)
    fig.suptitle("Regions aligned without stretching time", x=0.08, ha="left", color=COLORS["text"])
    _subtitle(fig, "Complete sessions plus exactly 1 hour before and after | Every hour has the same width | Amber: variable total load")
    fig.subplots_adjust(top=0.86, bottom=0.09, left=0.08, right=0.98, hspace=0.34)
    return fig


def edges_figure(result):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), sharey=True)
    for side, ax in zip(("start", "end"), axes):
        left_minutes, right_minutes = (-60, 120) if side == "start" else (-120, 60)
        for region in result.regions:
            stamp = pd.Timestamp(getattr(region, side))
            part = region_profile(result, region)
            part = part.loc[(part.index >= stamp + pd.Timedelta(minutes=left_minutes))
                            & (part.index < stamp + pd.Timedelta(minutes=right_minutes))]
            x = (part.index - stamp).total_seconds().to_numpy() / 60
            y = part["total_kw"].to_numpy()
            ax.step(np.r_[x, right_minutes], np.r_[y, y[-1]], where="post",
                    color=COLORS["primary"], alpha=0.3, linewidth=1)
        ax.axvline(0, color=COLORS["accent"], linewidth=1)
        ax.set_title("Starts: household activity varies" if side == "start" else "Stops: isolate the load drop", loc="left")
        ax.set_xlabel(f"Minutes from detected {side}")
        ax.set_xlim(left_minutes, right_minutes)
        ax.set_xticks(np.arange(left_minutes, right_minutes + 1, 30))
    axes[0].set_ylabel("Total consumption (kW)")
    fig.suptitle("Use the stop to estimate charging power", x=0.08, y=1.02, ha="left", color=COLORS["text"])
    _subtitle(fig, f"{len(result.regions)} candidate regions | Same elapsed-time scale | No time stretching")
    fig.subplots_adjust(top=0.82, bottom=0.14, left=0.08, right=0.98)
    return fig


def diagnostics_figure(result):
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    daily = result.daily
    x = np.arange(len(daily))
    axes[0, 0].plot(x, daily["daily_q10_baseline_kw"], color=COLORS["primary"], label="Daily p10")
    axes[0, 0].axhline(result.baseline_kw, color=COLORS["accent"], linestyle="--", label="All-days p10")
    axes[0, 0].set_title("Always-on baseline estimate", loc="left")
    axes[0, 0].set_ylabel("Power (kW)")
    axes[0, 0].legend(fontsize=8)
    axes[1, 0].bar(x, daily["ev_kwh"], color=COLORS["accent"], alpha=0.7, label="Per-region blocks")
    if result.shared_ev_kw is not None:
        axes[1, 0].plot(x, daily["shared_ev_kwh"], color=COLORS["secondary"], linewidth=1.5, label="Ending-calibrated EV model")
    axes[1, 0].set_title("Estimated daily EV energy", loc="left")
    axes[1, 0].set_ylabel("Energy (kWh)")
    axes[1, 0].legend(fontsize=8)
    for ax in axes[:, 0]:
        ax.set_xticks(x[::5], [str(d)[5:] for d in daily["date"].iloc[::5]], fontsize=8)
    if result.regions:
        ids = [r.region_id for r in result.regions]
        axes[0, 1].scatter(ids, [r.ev_kw for r in result.regions], color=COLORS["primary"], s=22, label="Bounded q10 minus baseline")
        local = [r for r in result.regions if r.ev_above_local_background_kw is not None]
        axes[0, 1].scatter([r.region_id for r in local], [r.ev_above_local_background_kw for r in local],
                           color=COLORS["accent"], marker="x", s=22, label="q10 minus local background")
        if result.shared_ev_kw is not None:
            axes[0, 1].axhline(result.shared_ev_kw, color=COLORS["secondary"], linestyle="--", label="Mean of clean ending drops")
        axes[1, 1].scatter([r.duration_hours for r in result.regions], [r.spread_kw for r in result.regions],
                           color=[COLORS["accent"] if r.consistent else COLORS["warning"] for r in result.regions])
    axes[0, 1].set_title("Does one EV power level fit?", loc="left")
    axes[0, 1].set_xlabel("Region ID")
    axes[0, 1].set_ylabel("Estimated EV power (kW)")
    axes[0, 1].legend(fontsize=7)
    axes[1, 1].set_title("Consistency of total load", loc="left")
    axes[1, 1].set_xlabel("Region duration (hours)")
    axes[1, 1].set_ylabel("Within-region p90 - p10 (kW)")
    fig.suptitle("Baseline and shared-load diagnostics", x=0.08, ha="left", color=COLORS["text"])
    _subtitle(fig, "Lower-envelope estimates are hypotheses | Consistency does not identify the appliance")
    fig.subplots_adjust(top=0.87, bottom=0.09, left=0.08, right=0.98, wspace=0.23, hspace=0.38)
    return fig


def estimation_figure(result):
    estimate = result.estimation
    fig, ax = plt.subplots(figsize=(9, 5.5))
    table = estimate.endings
    accepted = table.loc[table["accepted"].astype(bool)]
    count = len(accepted)
    positions = np.arange(1, count + 1)
    ax.scatter(positions, accepted["ev_kw"], color=COLORS["primary"], s=36,
                    label="Accepted stop drop", zorder=3)
    if count:
        ax.set_xlim(0.5, count + 0.5)
        ax.set_xticks(np.unique(np.r_[1, np.arange(5, count + 1, 5), count]))
    if estimate.ev_kw is not None:
        ax.axhline(estimate.summary["mean_ev_kw"], color=COLORS["accent"], linestyle="--",
                       label=f"Mean: {estimate.ev_kw:.3f} kW")
        low, high = estimate.summary["bootstrap_95pct_low_kw"], estimate.summary["bootstrap_95pct_high_kw"]
        if low is not None:
            ax.axhspan(low, high, color=COLORS["accent"], alpha=0.12,
                           label=f"95% interval for mean: {low:.3f}-{high:.3f} kW")
    ax.set_xlabel("Accepted stop, in chronological order")
    ax.set_ylabel("Estimated switched load (kW)")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.22), fontsize=9)
    if count:
        powers = accepted["ev_kw"].dropna()
        margin = max(0.05, (powers.max() - powers.min()) * 0.15)
        ax.set_ylim(powers.min() - margin, powers.max() + margin)
    title = f"EV power estimate: {estimate.ev_kw:.3f} kW" if estimate.ev_kw is not None else "EV power estimate unavailable"
    fig.suptitle(title, x=0.08, y=1.01, ha="left", color=COLORS["text"])
    _subtitle(fig, f"{count}/{len(table)} clean stops accepted | {result.start} to {result.end}")
    fig.subplots_adjust(top=0.86, bottom=0.26, left=0.08, right=0.98)
    return fig


def sensitivity_table(result):
    from .analysis import detect_regions
    lo, _ = day_bounds(result.start, result.timezone)
    _, hi = day_bounds(result.end, result.timezone)
    rows = []
    for threshold in sorted(set([1.5, 1.8, 2.0, result.config.threshold_kw, 2.2, 2.4,
                                 *([result.shared_threshold_kw] if result.shared_threshold_kw else [])])):
        regions = [r for r in detect_regions(result.samples, result.config, result.baseline_kw, threshold_kw=threshold)
                   if r.start < hi and r.end > lo]
        rows.append({"threshold_kw": threshold, "regions": len(regions),
                     "charging_hours": sum((min(pd.Timestamp(r.end), hi) - max(pd.Timestamp(r.start), lo)).total_seconds() / 3600 for r in regions),
                     "consistent_regions": sum(r.consistent for r in regions)})
    return pd.DataFrame(rows)


def region_shape_table(result):
    rows = []
    for region in result.regions:
        part = result.samples.loc[(result.samples.index >= region.start) & (result.samples.index < region.end)]
        phase = (part.index - pd.Timestamp(region.start)).total_seconds() / (region.duration_hours * 3600)
        values = part["total_kw"] - result.baseline_kw
        first = float(values.loc[phase < 0.1].median())
        middle = float(values.loc[(phase >= 0.2) & (phase < 0.8)].median())
        last = float(values.loc[phase >= 0.9].median())
        rows.append({"region_id": region.region_id, "consistent": region.consistent,
                     "first_10pct_median_kw": first, "middle_60pct_median_kw": middle,
                     "last_10pct_median_kw": last, "start_minus_middle_kw": first - middle,
                     "end_minus_middle_kw": last - middle})
    return pd.DataFrame(rows, columns=["region_id", "consistent", "first_10pct_median_kw", "middle_60pct_median_kw",
                                       "last_10pct_median_kw", "start_minus_middle_kw", "end_minus_middle_kw"])


def preferred_energy_sensitivity(result):
    from .analysis import detect_regions
    summary = result.estimation.summary
    lo, _ = day_bounds(result.start, result.timezone)
    _, hi = day_bounds(result.end, result.timezone)
    rows = []
    for hypothesis, value in [("bootstrap_low", summary["bootstrap_95pct_low_kw"]),
                              ("point_estimate", summary["ev_kw"]),
                              ("bootstrap_high", summary["bootstrap_95pct_high_kw"]),
                              ("window_sensitivity_low", summary["window_sensitivity_low_kw"]),
                              ("window_sensitivity_high", summary["window_sensitivity_high_kw"])]:
        if value is None:
            continue
        regions = [r for r in detect_regions(result.samples, result.config, result.baseline_kw, threshold_kw=value)
                   if r.start < hi and r.end > lo]
        hours = sum((min(pd.Timestamp(r.end), hi) - max(pd.Timestamp(r.start), lo)).total_seconds() / 3600 for r in regions)
        rows.append({"hypothesis": hypothesis, "ev_kw_and_threshold_kw": value, "regions": len(regions),
                     "charging_hours": hours, "estimated_ev_kwh": value * hours})
    return pd.DataFrame(rows, columns=["hypothesis", "ev_kw_and_threshold_kw", "regions", "charging_hours", "estimated_ev_kwh"])


def export_report(result: EVInvestigation, days, output: Path):
    output = Path(output)
    (output / "daily").mkdir(parents=True, exist_ok=True)
    (output / "source").mkdir(exist_ok=True)
    # All fetched columns remain available for subsequent investigations, even
    # though this detector exclusively uses total consumption.
    source_rows = [{"source_date": d.day, **asdict(s)} for d in days for s in d.samples]
    pd.DataFrame(source_rows).to_csv(output / "source" / "measurement_samples.csv", index=False)
    pd.DataFrame([{"source_date": d.day, **asdict(m)} for d in days for m in d.metrics]).to_csv(
        output / "source" / "daily_metrics.csv", index=False)
    source_meta = [{"day": str(d.day), "timezone": d.timezone, "fetched_at": d.fetched_at.isoformat(),
                    "sample_count": d.sample_count, "available_series": d.available_series,
                    "empty_series": d.empty_series, "metadata": d.metadata} for d in days]
    (output / "source" / "days.json").write_text(json.dumps(source_meta, indent=2), encoding="utf-8")
    samples = result.samples.copy()
    samples.insert(0, "local_timestamp", samples.index.tz_convert(result.timezone))
    samples.to_csv(output / "consumption_samples.csv")
    from dataclasses import fields
    columns = [f.name for f in fields(ChargingRegion)]
    pd.DataFrame([asdict(r) for r in result.regions], columns=columns).to_csv(output / "regions.csv", index=False)
    pd.DataFrame([asdict(r) for r in result.shared_regions], columns=columns).to_csv(output / "shared_regions.csv", index=False)
    result.daily.to_csv(output / "daily_summary.csv", index=False)
    estimate = result.estimation
    estimate.endings.to_csv(output / "ending_estimates.csv", index=False)
    estimate.variants.to_csv(output / "ending_estimate_variants.csv", index=False)
    estimate.sensitivity.to_csv(output / "ending_window_sensitivity.csv", index=False)
    estimate.nights.to_csv(output / "night_baseload.csv", index=False)
    pd.DataFrame(estimate.summary["quality_sensitivity"]).to_csv(output / "ending_quality_sensitivity.csv", index=False)
    preferred_energy_sensitivity(result).to_csv(output / "ending_energy_sensitivity.csv", index=False)
    (output / "estimation.json").write_text(json.dumps(estimate.summary, indent=2, allow_nan=False), encoding="utf-8")
    shapes = region_shape_table(result)
    shapes.to_csv(output / "region_shapes.csv", index=False)
    profiles = [region_profile(result, region) for region in result.regions]
    if profiles:
        pd.concat(profiles).to_csv(output / "region_profiles.csv")
    else:
        pd.DataFrame(columns=["region_id", "section", "total_kw", "hours_from_start", "hours_from_end",
                              "normalized_percent", "total_minus_baseline_kw", "total_minus_region_quantile_kw"]).to_csv(
            output / "region_profiles.csv", index_label="timestamp")
    sensitivity = sensitivity_table(result)
    sensitivity.to_csv(output / "threshold_sensitivity.csv", index=False)
    (output / "summary.json").write_text(json.dumps(result.summary, indent=2, allow_nan=False), encoding="utf-8")
    figures = [("ending_calibration.png", estimation_figure), ("overview.png", overview_figure), ("normalized_regions.png", overlay_figure),
               ("elapsed_regions.png", elapsed_overlay_figure), ("starts_and_endings.png", edges_figure),
               ("baseline_and_shared_load.png", diagnostics_figure)]
    with chart_style(), PdfPages(output / "investigation.pdf") as pdf:
        for filename, builder in figures:
            fig = builder(result)
            fig.savefig(output / filename)
            pdf.savefig(fig)
            plt.close(fig)
        for day in result.daily["date"]:
            fig = daily_figure(result, day)
            fig.savefig(output / "daily" / f"{day}.png")
            pdf.savefig(fig)
            plt.close(fig)
            print(f"Graph exported: {day}", flush=True)
    write_index(result, output, sensitivity)
    write_findings(result, output, shapes)
    write_estimation_notes(result, output)
    return output / "index.html"


def write_estimation_notes(result, output):
    s = result.estimation.summary
    def number(value, precision=3):
        return f"{value:.{precision}f}" if value is not None else "unavailable"
    rejected = result.estimation.endings.loc[~result.estimation.endings["accepted"].astype(bool)]
    exclusions = "\n".join(f"- Reference region {row.region_id}, ending {row.end_local}: {row.rejection_reason}."
                            for row in rejected.itertuples()) or "No endings excluded."
    text = f"""# Ending-calibrated EV estimate: {result.start} through {result.end}

Preferred EV power: **{number(s['ev_kw'])} kW**. Accepted endings: **{s['accepted_endings']} / {s['reference_candidates']}**.
Preferred detected regions: {result.summary['preferred_regions']}; charging hours: {number(result.summary['preferred_charging_hours'], 2)}; estimated EV energy: **{number(result.summary['preferred_estimated_ev_kwh'], 1)} kWh**.
Previous shared power: {number(result.summary['reference_shared_ev_kw'])} kW. The reference percentile-block energy remains {result.summary['estimated_ev_kwh']:.1f} kWh.

## EV power calculation

Keep the fixed reference detector (> {result.config.threshold_kw:g} kW for at least {result.config.minimum_minutes:g} uninterrupted minutes) to identify candidate endings. Fit EV power once from these endings, then detect once at the fitted power; there is no repeated refitting to the new mask.

Let t=0 be the first interval below the reference threshold. On this five-minute dataset, use the readings at -15 and -10 minutes; omit -5 and 0 minutes; use +5 and +10 minutes. EV power for a session is mean(before) minus mean(after). The after-window mean is local household background at the ending, not the always-present baseload. Stability checks filter incomplete, noisy and implausible stop drops before averaging across sessions.

Exclude incomplete or censored windows, nonpositive drops, windows with max-minus-min exceeding 0.2 kW, and drops greater than the candidate region's raw p10. The last check rejects an ending caused by a short high-power load above a lower plateau. Acceptance depends on the ending, not how messy the session starts or its full consumption profile.

Use the unweighted arithmetic mean of accepted ending drops as shared EV power, rounded to three decimals for detection and energy. The unrounded mean is {number(s['mean_ev_kw'], 7)} kW. There are {s['nighttime_accepted_endings']} accepted endings between local midnight and 08:00; their mean is {number(s['nighttime_ev_kw'])} kW. All accepted endings contribute to the preferred estimate, avoiding an arbitrary exclusion of clean daytime endings.

Excluded endings:

{exclusions}

## Experiments and uncertainty

The default 10-minute windows and two skipped readings implement the agreed hypothesis; they were not selected by optimizing this dataset. Compare 10/15/20-minute windows and one/two skipped readings with the same acceptance criteria. Mean accepted estimates span **{number(s['window_sensitivity_low_kw'])}–{number(s['window_sensitivity_high_kw'])} kW**. Also compare window-spread tolerances of 0.1/0.2/0.3 kW.

Resample accepted complete ending estimates 5,000 times with replacement, using seed 20261011, and recompute the mean without rounding. Its conditional percentile-bootstrap 95% interval is **{number(s['bootstrap_95pct_low_kw'])}–{number(s['bootstrap_95pct_high_kw'])} kW**. With fewer than three accepted endings, omit the interval. Five-minute samples are not treated as independent calibration observations. This interval assumes the accepted endings represent independent sessions; it excludes systematic meter, background, detection and attribution errors.

Fit the mean on accepted endings in the first two-thirds of chronological reference sessions, then compare with accepted endings in the last third. Earlier fit: {number(s['earlier_fit_ev_kw'])} kW from {s['earlier_accepted_endings']} endings. Later mean absolute disagreement: {number(s['later_ending_mae_kw'])} kW over {s['later_accepted_endings']} endings. Entire sessions stay together. This tests consistency with later ending estimates, not charger-ground-truth accuracy; candidate generation still uses the fixed reference threshold derived during the earlier investigation.

`ending_energy_sensitivity.csv` reruns detection at the bootstrap and window-sensitivity bounds, using each hypothesized power as both trigger and block height. This demonstrates sensitivity of energy and fragmentation; these scenarios are not a formal confidence interval for total kWh.

## Independent quiet-night baseload

Use local 00:00–06:00 observations within the requested days. Exclude reference regions and 30 minutes on either side, missing readings, and observations above the reference threshold. Require at least 12 observed readings (one hour on this dataset) per eligible night. Calculate each night's p10, then take the median across nights, giving each night equal weight.

Quiet-night baseload proxy: **{number(s['baseload_kw'])} kW**, from {s['eligible_nights']} eligible nights. The whole-night bootstrap 95% interval is {number(s['baseload_bootstrap_95pct_low_kw'])}–{number(s['baseload_bootstrap_95pct_high_kw'])} kW. Using p05 or p20 instead gives {number(s['baseload_q05_kw'])} and {number(s['baseload_q20_kw'])} kW. These are candidate-free observations, not verified EV-off labels. The estimate is a lower consumption envelope, not proof of a physically constant appliance load.

Do not subtract this baseload from the ending-calibrated EV power again: local background has already been removed by the before-minus-after calculation. It also does not predict the household background throughout a long session. A time-varying background model would be a separate experiment.

## Report and data structures

Colored daily blocks and on/off strips use the preferred ending-calibrated power and its own >EV-power trigger, retaining the two-hour minimum and cross-midnight clipping. Thus the EV block fits below each measured consumption interval. Slate dotted lines preserve the reference percentile estimates. Reference regions, normalized shapes and real-time overlays remain available for inspecting the calibration candidates.

`EVLoadEstimation` stores the config, accepted/rejected endings, all variants, sensitivity table, nightly baseload table and summary. `EVInvestigation.estimation` carries it alongside the source-aligned data and reference regions.

Exports: `ending_estimates.csv` records exact window timestamps, means, ranges, acceptance and reasons; `ending_estimate_variants.csv` retains each window/guard variant; `ending_window_sensitivity.csv` compares mean powers and bootstrap intervals; `ending_quality_sensitivity.csv` compares spread tolerances; `night_baseload.csv` retains each night's readings count and quantiles; `estimation.json` preserves all parameters/results. In `daily_summary.csv`, `ev_kwh` remains the reference estimate; `preferred_ev_kwh` and `shared_ev_kwh` both contain the preferred model estimate. `preferred_charging_hours` and `preferred_region_count` refer to its mask. `shared_regions.csv` contains preferred regions, while `regions.csv` remains the reference catalog.

FusionSolar supplies five-minute timestamps, but its instantaneous-versus-averaged convention and power units remain unverified by metadata. Skipping transition readings is a precaution, not evidence of a specific averaging convention. A fixed two-hour rule misses real shorter sessions. Shared power and threshold can split genuine sessions when measurements dip below the fitted level. Household devices can imitate EV drops. Known charger times and kWh are needed to validate attribution and accuracy.
"""
    (output / "estimation.md").write_text(text, encoding="utf-8")


def write_findings(result, output, shapes):
    s = result.summary
    stable_shapes = shapes.loc[shapes["consistent"]]
    edges = (f"Among consistent regions, the median first-10%-minus-middle difference is "
             f"{stable_shapes['start_minus_middle_kw'].median():+.3f} kW and the last-10%-minus-middle difference is "
             f"{stable_shapes['end_minus_middle_kw'].median():+.3f} kW. These are total-minus-baseline profiles, "
             "so background activity can also explain the changes."
             if len(stable_shapes) else "There are no consistent regions to summarize start/end shape.")
    shared = (f"The preferred EV estimate is **{result.shared_ev_kw:.3f} kW**, using "
              f"{s['shared_calibration_regions']} accepted guarded ending drops. Its between-ending coefficient "
              f"of variation is {s['shared_load_coefficient_of_variation']:.1%}. "
              f"With a trigger of >{result.shared_threshold_kw:.3f} kW for {result.config.minimum_minutes:g} minutes, it finds "
              f"{s['shared_regions']} regions and estimates {s['shared_estimated_ev_kwh']:.1f} kWh. "
              f"Mask overlap with the original trigger is {s['shared_mask_jaccard']:.1%}. "
              "A shared load is a plausible simplification, but this comparison fits and evaluates on the same data."
              if result.shared_ev_kw is not None and s['shared_load_coefficient_of_variation'] is not None
              else "There are too few consistent regions to evaluate a shared load.")
    text = f"""# Findings: {result.start} through {result.end}

All {s['requested_days']} requested local days have complete {s['cadence_seconds']/60:g}-minute consumption observations. The required {result.config.minimum_minutes:g}-minute boundary context is present. Only total consumption (`usePower`) was analyzed.

The reference candidate trigger is **>{result.config.threshold_kw:.2f} kW for {result.config.minimum_minutes:g} uninterrupted minutes**. The preferred colored blocks use the ending-calibrated EV power as their trigger and height. See [the estimation method and results](estimation.md).

There are **{s['regions']} reference candidate regions**, spanning **{s['charging_hours']:.2f} hours** within the requested dates. The reference percentile method estimates **{s['estimated_ev_kwh']:.1f} kWh** of EV energy. Blocks are bounded by the minimum observed consumption in each region; {s['limited_blocks']} required this bound. {s['consistent_regions']} reference regions meet the exploratory consistency rule; {s['regions']-s['consistent_regions']} are variable and remain flagged for review. No regions were silently joined across interruptions.

The reference all-sample lower-envelope baseline is **{result.baseline_kw:.3f} kW**. Daily reference p10 estimates range from {result.daily['daily_q10_baseline_kw'].min():.3f} to {result.daily['daily_q10_baseline_kw'].max():.3f} kW. The preferred quiet-night estimate and its uncertainty are in [estimation.md](estimation.md). An always-present load cannot be verified from these consumption observations alone.

{shared}

{edges}

Power is declared as {s['declared_source_unit']}; source metadata verification is {s['source_unit_verified']}. Sample integration gives {s['sampled_total_kwh']:.1f} kWh versus {s['api_total_kwh']:.1f} kWh in API daily totals. The detector does not rescale consumption or estimated EV energy to force agreement. Sampling, transition timing and API aggregation can affect this comparison.

Review the variable regions and the longest regions against known charge dates. A household load can imitate this trigger, and another load may keep total consumption above threshold after the EV stops. Confirmation needs charger logs or a validation set of known charging sessions.

Open [the report](index.html), [all graphs as PDF](investigation.pdf), or [the reference regions](regions.csv). Daily graphs show the preferred ending-calibrated EV blocks and matching on/off strip; slate dotted lines preserve the reference percentile estimates. The overview includes calibration diagnostics, all dates, normalized reference regions with exact one-hour side panels, full reference sessions aligned by start/end without stretching time, close-ups of starts/endings, and baseline/shared-load diagnostics. `region_profiles.csv` preserves actual UTC intervals and elapsed coordinates. Missing context stays unknown.
"""
    (output / "findings.md").write_text(text, encoding="utf-8")


def write_index(result, output, sensitivity):
    summary = result.summary
    context = pd.Timedelta(minutes=result.config.minimum_minutes)
    reference_midnight = pd.Timestamp("2000-01-02")
    previous_start = (reference_midnight - context).strftime("%H:%M")
    following_end = (reference_midnight + context).strftime("%H:%M")
    shared_excess = (f"{summary['shared_block_exceeds_observed_fraction']:.1%}"
                     if summary['shared_block_exceeds_observed_fraction'] is not None else "unavailable")
    shared_overlap = (f"{summary['shared_mask_jaccard']:.1%}"
                      if summary['shared_mask_jaccard'] is not None else "unavailable")
    preferred = result.shared_ev_kw is not None
    preferred_text = (f"Preferred model: {summary['preferred_regions']} candidate regions · "
                      f"{summary['preferred_charging_hours']:.2f} hours · {summary['preferred_estimated_ev_kwh']:.1f} kWh · "
                      f"{result.shared_ev_kw:.3f} kW EV draw" if preferred else "Ending calibration unavailable; reference estimates are shown.")
    estimate_summary = result.estimation.summary
    calibration_text = ""
    if estimate_summary["bootstrap_95pct_low_kw"] is not None:
        calibration_text += (f"EV mean: {result.shared_ev_kw:.3f} kW; conditional 95% bootstrap interval "
                             f"{estimate_summary['bootstrap_95pct_low_kw']:.3f}–{estimate_summary['bootstrap_95pct_high_kw']:.3f} kW. ")
    if result.estimation.baseline_kw is not None:
        calibration_text += f"Quiet-night baseload proxy: {result.estimation.baseline_kw * 1000:.0f} W from {estimate_summary['eligible_nights']} nights."
    shared = (f"The preferred EV estimate is {result.shared_ev_kw:.3f} kW, fitted as the mean of "
              f"{summary['shared_calibration_regions']} accepted ending drops using 10-minute clean windows and two skipped readings. "
              f"Its trigger is &gt; {result.shared_threshold_kw:.3f} kW "
              f"(shared EV load alone, with no margin or baseline added). "
              f"This finds {len(result.shared_regions)} regions and estimates {summary['shared_estimated_ev_kwh']:.1f} kWh. "
              f"Between-ending coefficient of variation: {summary['shared_load_coefficient_of_variation']:.1%}."
              if result.shared_ev_kw is not None and summary['shared_load_coefficient_of_variation'] is not None
              else "Too few suitable regions to evaluate a shared load reliably.")
    cards = "".join(f'<article><h2>{escape(title)}</h2><img src="{filename}" loading="lazy"></article>' for filename, title in [
        ("ending_calibration.png", "Ending calibration, uncertainty and quiet-night baseload"),
        ("overview.png", "All days and detected regions"), ("normalized_regions.png", "Normalized regions with one-hour context"),
        ("elapsed_regions.png", "Full regions aligned by start and end in real time"),
        ("starts_and_endings.png", "Starts and endings"), ("baseline_and_shared_load.png", "Baseline and common EV load")])
    options = "".join(f'<option value="{day}">{day}</option>' for day in result.daily["date"])
    galleries = "".join(f'<details id="day-{day}"><summary>{day} · {row.ev_kwh:.1f} kWh · {row.charging_hours:.2f} h</summary>'
                        f'<img src="daily/{day}.png" loading="lazy"></details>'
                        for day, row in zip(result.daily["date"], result.daily.itertuples()))
    if preferred:
        galleries = "".join(f'<details id="day-{day}"><summary>{day} · {row.preferred_ev_kwh:.1f} kWh · {row.preferred_charging_hours:.2f} h</summary>'
                            f'<img src="daily/{day}.png" loading="lazy"></details>'
                            for day, row in zip(result.daily["date"], result.daily.itertuples()))
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>EV investigation {result.start} to {result.end}</title>
<style>body{{background:#0B1220;color:#E7EDF5;font:16px system-ui;max-width:1300px;margin:40px auto;padding:0 24px}}
h1{{font-size:28px}}h2{{font-size:21px}}p,li{{line-height:1.65}}a{{color:#67DDD1}}img{{width:100%;height:auto}}
article,details{{background:#131F31;padding:20px;margin:24px 0;border-radius:8px}}summary{{cursor:pointer}}
table{{border-collapse:collapse;font-size:13px;display:block;overflow:auto}}th,td{{padding:8px;border-bottom:1px solid #27364A}}
select{{background:#131F31;color:#E7EDF5;padding:10px;border:1px solid #718399}}.metric{{color:#67DDD1}}</style>
<h1>Consumption-only EV investigation</h1><p>{result.start} through {result.end} · {escape(result.timezone)} · five-minute observations</p>
<p class="metric">{preferred_text}</p>
<p>{calibration_text}</p>
<p>Reference: {len(result.regions)} candidate regions · {summary['charging_hours']:.2f} hours · {summary['estimated_ev_kwh']:.1f} kWh from percentile blocks. Colored daily blocks use the preferred ending-calibrated model.</p>
<p><a href="estimation.md">Estimation method and results</a> · <a href="estimation.json">Calibration summary</a> · <a href="ending_estimates.csv">Each accepted/rejected ending</a> · <a href="ending_window_sensitivity.csv">Window comparison</a> · <a href="night_baseload.csv">Quiet-night baseload samples</a></p>
<p><a href="investigation.pdf">All graphs as PDF</a> · <a href="regions.csv">Regions CSV</a> · <a href="region_profiles.csv">Profiles and one-hour context CSV</a> · <a href="daily_summary.csv">Daily totals CSV</a> · <a href="consumption_samples.csv">Consumption and masks CSV</a> · <a href="summary.json">Parameters and results JSON</a></p>
<h2>Detection rules and data</h2><p>Use only usePower (total consumption). Reference candidates are uninterrupted regions strictly above {result.config.threshold_kw:g} kW for at least {result.config.minimum_minutes:g} minutes. Ending calibration is fitted once from these candidates; the preferred model detects regions once using that calibrated EV power directly, keeping the same minimum duration. There is no iterative refitting.
Interruptions, missing readings and gaps split regions. Detect across midnight, then clip the block to each local day. At least the preceding {previous_start}–00:00 and following 00:00–{following_end} are included; available longer context preserves full region statistics and prevents duplicate session counts.
The source readings are preserved in PostgreSQL and source/; analysis uses UTC timestamps with local-date boundaries.</p>
<p>Source power is declared as {escape(summary['declared_source_unit'])}. API unit metadata verified: {summary['source_unit_verified']}. Each reading is held for the following sampling interval; transition times and integrated energy are approximate.
Sample integration totals {summary['sampled_total_kwh']:.1f} kWh versus {summary['api_total_kwh']:.1f} in API daily totals, a difference of {(summary['sampled_total_kwh']/summary['api_total_kwh'] - 1)*100 if summary['api_total_kwh'] else 0:.1f}%.
No rescaling is applied. Missing samples in target days: {summary['missing_target_samples']}; missing samples in required context: {summary['missing_required_context_samples']}.</p>
<h2>Baseload and block estimates</h2><p>The preferred baseload proxy is the median of nightly 10th percentiles from candidate-free 00:00–06:00 samples, excluding 30 minutes around reference regions. It is estimated separately from EV draw; ending subtraction already removes local background. See the calibration summary for its bootstrap interval and 5th/20th-percentile sensitivity.</p><p>The reference baseline is the {result.config.baseline_quantile:.0%} quantile of all requested consumption samples.
It is a lower-envelope proxy, not a verified continuously present appliance load. Daily quantiles test its stability.
Each EV block is max(0, region {result.config.block_quantile:.0%} quantile − {result.baseline_kw:.3f} kW baseline).
The constant block is bounded by the region's minimum observed total consumption so it fits under every interval; {summary['limited_blocks']} blocks needed this bound. The unconstrained quantile estimate remains in the CSV.
Local background uses the {result.config.baseline_quantile:.0%} quantile of below-threshold samples within one hour before/after a region, with at least six samples.
The CSV preserves both estimates, the raw total-load quantiles, and region uncertainty.</p>
<p>Mint regions have p90 − p10 ≤ max({result.config.stability_absolute_kw:g} kW, {result.config.stability_relative:.0%} × p10); amber regions exceed that diagnostic tolerance.
All qualifying regions remain visible: {summary['consistent_regions']} are consistent and {len(result.regions)-summary['consistent_regions']} variable. {summary['censored_regions']} touch missing data or a context edge.
This is an exploratory stability rule, not proof that a region is EV charging.</p>
<h2>Would a shared EV load help?</h2><p>{shared}</p><p>This fit uses the same dataset it is compared against. It is a hypothesis for a simpler detector, not validated performance.
Compare threshold sensitivity below and the daily curves. Each shared block uses its own EV-level trigger and exceeds observed total consumption in {shared_excess} of its detected-region samples
and the revised mask has {shared_overlap} overlap (intersection / union) with the original mask.</p>
{sensitivity.round(3).to_html(index=False, border=0)}
<p>Normalized graphs stretch only charging to 0–100%; the left and right panels retain exactly 60 real minutes of context. The full-duration overlays align either all starts or all ends at zero and preserve real elapsed hours. These overlays subtract the estimated baseline. Start close-ups show 60 minutes before and 120 minutes of charging after the start; end close-ups show 120 minutes of charging before and 60 minutes after the end. Close-ups show total consumption directly; context can contain other activity or another charging region. Missing context stays unknown.</p>
<p>Household loads can mimic EV regions; consumption alone cannot establish attribution. Charger logs or known charge dates would provide a useful validation set.</p>
{cards}<h2>Daily graphs</h2><label for="day">Jump to a day </label><select id="day">{options}</select>
{galleries}<script>document.getElementById('day').addEventListener('change',e=>{{const el=document.getElementById('day-'+e.target.value);el.open=true;el.scrollIntoView({{behavior:'smooth'}})}})</script></html>'''
    # Empty detections/shared calibration should still yield a readable report.
    (output / "index.html").write_text(html, encoding="utf-8")


def run_investigation(start, end, output, *, fetch=False, config=None, unit="kW", repository=None):
    if start > end:
        raise ValueError("Range start must not be after its end.")
    config = config or EVDetectionConfig()
    days = load_investigation_days(repository or EnergyBalanceRepository(), start, end, fetch=fetch)
    result = investigate(days, start, end, config=config, unit=unit)
    index = export_report(result, days, output)
    print(f"Report: {index.resolve()}", flush=True)
    print(json.dumps(result.summary, indent=2, allow_nan=False))
    return result
