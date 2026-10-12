"""Fill/load a usage day and plot source samples through the shared charts."""

import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from home_energy.usage import EnergyBalanceRepository
from home_energy.usage.ingestion import UsageIngestionService
from home_energy.charts import LineSeries, build_line_chart, save_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", type=date.fromisoformat,
                        default=datetime.now(ZoneInfo("Europe/Brussels")).date() - timedelta(days=1))
    parser.add_argument("--db-only", action="store_true")
    parser.add_argument("--output", default="outputs/daily_consumption.png")
    args = parser.parse_args()
    repository = EnergyBalanceRepository()
    if not args.db_only:
        UsageIngestionService(repository).fill(args.date)
    day = repository.load_day(args.date)
    if day is None:
        raise SystemExit(f"No stored usage for {args.date}.")
    print(day.consumed_energy)  # API daily total; never inferred from power samples.
    samples = [sample for sample in day.samples if sample.measurement == "usePower"]
    if not samples:
        raise SystemExit("The stored day has no populated usePower series.")
    ticks = samples[::max(1, len(samples) // 6)]
    zone = ZoneInfo(day.timezone)
    labels = [sample.observed_at.astimezone(zone).strftime("%H:%M") if sample.observed_at
              else sample.axis_label for sample in ticks]
    figure = build_line_chart(
        [LineSeries([sample.sample_index for sample in samples], [sample.value for sample in samples],
                    label="Consumption")],
        title="Energy consumption", subtitle=f"{day.day} · {day.timezone}",
        ylabel=samples[0].unit or "Source value", xticks=[sample.sample_index for sample in ticks],
        xticklabels=labels,
    )
    try:
        print(save_png(figure, args.output))
    finally:
        figure.clear()


if __name__ == "__main__":
    main()
