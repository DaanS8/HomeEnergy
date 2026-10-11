"""Compose a price chart from stored objects using shared chart primitives."""

from collections import Counter
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

from matplotlib.figure import Figure

from ..charts import HighlightSegment, LineSeries, build_line_chart
from .analysis import DEFAULT_THRESHOLD, SUPPLIED_REFERENCE_PROFILE, analyse_prices
from .models import DayAheadPriceDay


def create_price_chart(day: DayAheadPriceDay, *, threshold: Decimal = DEFAULT_THRESHOLD,
                       reference_profile: Sequence | None = SUPPLIED_REFERENCE_PROFILE) -> Figure:
    analysis = analyse_prices(day, threshold=threshold, reference_profile=reference_profile)
    x = [interval.start_at for interval in day.intervals] + [day.intervals[-1].end_at]
    curve = [*analysis.smoothed_prices, analysis.smoothed_prices[-1]]
    lines = []
    if analysis.reference_prices is not None:
        lines.append(LineSeries(x, [*analysis.reference_prices, analysis.reference_prices[-1]],
                                label="Reference profile", role="muted"))
    lines.append(LineSeries(x, curve, label=str(day.day), linewidth=2.5))
    highlights = []
    for index, high in enumerate(analysis.high_mask):
        if high:
            label = f"> €{Decimal(str(threshold)):g}/MWh (actual)" if not highlights else None
            highlights.append(HighlightSegment(x[index], x[index + 1], curve[index], curve[index + 1],
                                               label=label, role="error"))
    zone = ZoneInfo(day.timezone)
    tick_times = [instant for instant in x[:-1]
                  if instant.astimezone(zone).minute == 0 and instant.astimezone(zone).hour % 2 == 0]
    local_ticks = [instant.astimezone(zone) for instant in tick_times]
    counts = Counter(local.strftime("%H:%M") for local in local_ticks)
    # Brussels has whole-hour UTC offsets; compact +02/+01 labels avoid
    # collisions between repeated clock hours separated by one elapsed hour.
    tick_labels = [local.strftime("%H:%M") +
                   ("\n" + local.strftime("%z")[:3] if counts[local.strftime("%H:%M")] > 1 else "")
                   for local in local_ticks]
    return build_line_chart(lines, title="Day-ahead electricity prices",
                            subtitle=f"{day.day} · {day.market} · {day.timezone} · Smoothed curve",
                            ylabel="€/MWh", figsize=(12, 5), highlights=highlights,
                            xticks=tick_times, xticklabels=tick_labels, xlim=(x[0], x[-1]))
