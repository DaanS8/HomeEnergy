"""Fetch a date, reload stored objects, analyse them, and save a price chart."""

import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from home_energy.charts import save_png
from home_energy.cli import _hysteresis, _threshold
from home_energy.prices import DayAheadPriceRepository
from home_energy.prices.ingestion import DayAheadPriceIngestionService
from home_energy.prices.analysis import DEFAULT_HYSTERESIS, DEFAULT_THRESHOLD, analyse_prices
from home_energy.prices.chart import create_price_chart


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", type=date.fromisoformat,
                        default=datetime.now(ZoneInfo("Europe/Brussels")).date() + timedelta(days=1))
    parser.add_argument("--db-only", action="store_true")
    parser.add_argument("--output", default="outputs/day_ahead_prices.png")
    parser.add_argument("--threshold", type=_threshold, default=DEFAULT_THRESHOLD)
    parser.add_argument("--hysteresis", type=_hysteresis, default=DEFAULT_HYSTERESIS,
                        help="Range expansion margin in EUR/MWh (default: 25; 0 disables expansion)")
    args = parser.parse_args()
    repository = DayAheadPriceRepository()
    if not args.db_only:
        DayAheadPriceIngestionService(repository).fill(args.date)
    day = repository.load_day(args.date)
    if day is None:
        raise SystemExit(f"No stored prices for {args.date}.")
    analysis = analyse_prices(day, threshold=args.threshold, hysteresis=args.hysteresis)
    zone = ZoneInfo(day.timezone)
    for expensive in analysis.high_price_ranges:
        print(f"High price: {expensive.start_at.astimezone(zone):%H:%M %z} – "
              f"{expensive.end_at.astimezone(zone):%H:%M %z}")
    figure = create_price_chart(day, threshold=args.threshold, hysteresis=args.hysteresis, analysis=analysis)
    try:
        print(save_png(figure, args.output))
    finally:
        figure.clear()


if __name__ == "__main__":
    main()
