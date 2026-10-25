"""Compose a price chart from stored objects using shared chart primitives."""

from collections import Counter
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

from matplotlib.figure import Figure

from ..charts import LineSeries, build_line_chart
from .analysis import (DEFAULT_HYSTERESIS, DEFAULT_THRESHOLD, SUPPLIED_REFERENCE_PROFILE,
                       PriceAnalysis, analyse_prices)
from .models import DayAheadPriceDay


def create_price_chart(day: DayAheadPriceDay, *, threshold: Decimal = DEFAULT_THRESHOLD,
                       hysteresis: Decimal = DEFAULT_HYSTERESIS,
                       reference_profile: Sequence | None = SUPPLIED_REFERENCE_PROFILE,
                       analysis: PriceAnalysis | None = None) -> Figure:
    """Plot actual interval prices, optionally reusing the report's analysis."""
    if analysis is None:
        analysis = analyse_prices(day, threshold=threshold, hysteresis=hysteresis,
                                  reference_profile=reference_profile)
    x = [stamp for interval in day.intervals for stamp in (interval.start_at, interval.end_at)]
    curve = [interval.price_eur_per_mwh for interval in day.intervals for _ in range(2)]
    lines = []
    if analysis.reference_prices is not None:
        lines.append(LineSeries(x, [price for price in analysis.reference_prices for _ in range(2)],
                                label="Reference profile", role="muted"))
    lines.append(LineSeries(x, curve, label=str(day.day), linewidth=2.5))
    if analysis.high_price_ranges:
        trigger = Decimal(str(threshold))
        lower = trigger - Decimal(str(hysteresis))
        label = f"> €{trigger:g} peak · > €{lower:g} range (€/MWh)"
        high_curve = [interval.price_eur_per_mwh if high else None
                      for interval, high in zip(day.intervals, analysis.high_mask) for _ in range(2)]
        lines.append(LineSeries(x, high_curve, label=label, role="error", linewidth=2.8))
    zone = ZoneInfo(day.timezone)
    tick_times = [instant for instant in x[::2]
                  if instant.astimezone(zone).minute == 0 and instant.astimezone(zone).hour % 2 == 0]
    local_ticks = [instant.astimezone(zone) for instant in tick_times]
    counts = Counter(local.strftime("%H:%M") for local in local_ticks)
    # Brussels has whole-hour UTC offsets; compact +02/+01 labels avoid
    # collisions between repeated clock hours separated by one elapsed hour.
    tick_labels = [local.strftime("%H:%M") +
                   ("\n" + local.strftime("%z")[:3] if counts[local.strftime("%H:%M")] > 1 else "")
                   for local in local_ticks]
    return build_line_chart(lines, title="Day-ahead electricity prices",
                            subtitle=f"{day.day} · {day.market} · {day.timezone} · Actual interval prices",
                            ylabel="€/MWh", figsize=(12, 5),
                            xticks=[*tick_times, x[-1]], xticklabels=[*tick_labels, "24:00"],
                            xlim=(x[0], x[-1]))
