"""Daily measured consumption with calibrated EV blocks, ready for sharing."""

from collections import Counter

from matplotlib.figure import Figure

from ..charts import COLORS, LineSeries, build_line_chart
from .consumption import ZONE
from .models import DailyUsage


def create_ev_usage_chart(report: DailyUsage) -> Figure:
    """Preserve five-minute intervals and cross-midnight clipping without smoothing."""
    x = [stamp for interval in report.consumption for stamp in (interval.start_at, interval.end_at)]
    total = [interval.power_kw for interval in report.consumption for _ in range(2)]
    ev = [interval.ev_power_kw for interval in report.consumption for _ in range(2)]
    ticks = [interval.start_at for interval in report.consumption
             if interval.start_at.astimezone(ZONE).minute == 0
             and interval.start_at.astimezone(ZONE).hour % 2 == 0]
    local_ticks = [stamp.astimezone(ZONE) for stamp in ticks]
    counts = Counter(local.strftime("%H:%M") for local in local_ticks)
    labels = [local.strftime("%H:%M") +
              ("\n" + local.strftime("%z")[:3] if counts[local.strftime("%H:%M")] > 1 else "")
              for local in local_ticks]
    detail = f"{len(report.ev_blocks)} EV block(s)" if report.ev_blocks else "No EV charging detected"
    figure = build_line_chart(
        [LineSeries(x, total, label="Total consumption"),
         LineSeries(x, ev, label="Estimated EV charging", role="accent")],
        title="Energy consumption and EV charging",
        subtitle=f"{report.day} · Europe/Brussels · Total {report.total_kwh:.2f} kWh"
                 f" · EV {report.ev_total_kwh:.2f} kWh · {detail}",
        ylabel="kW", figsize=(12, 5), xticks=[*ticks, x[-1]],
        xticklabels=[*labels, "24:00"], xlim=(x[0], x[-1]),
    )
    ax = figure.axes[0]
    ax.fill_between(x, 0, [float(power) for power in ev], color=COLORS["accent"], alpha=0.18)
    ax.set_ylim(bottom=0)
    return figure
