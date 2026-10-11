"""Initialize storage, fill selected domains, and export database-only charts."""

import argparse
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg

from .database import initialize_schema
from .database.connection import DatabaseConfigurationError
from .filling import FillError
from .prices import DayAheadPriceRepository
from .prices.entsoe import PriceAPIError
from .usage import EnergyBalanceRepository
from .usage.fusionsolar import FusionSolarError


def _report_error(exc):
    if isinstance(exc, psycopg.Error):
        print("Database operation failed. Check PG* settings, PostgreSQL availability, "
              "and run home-energy init-db before filling or charting.", file=sys.stderr)
    else:
        print(f"Error: {exc}", file=sys.stderr)


def _threshold(value):
    try:
        threshold = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Threshold must be a finite number in EUR/MWh.") from None
    if not threshold.is_finite():
        raise argparse.ArgumentTypeError("Threshold must be a finite number in EUR/MWh.")
    return threshold


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage home electricity usage and day-ahead market prices.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Initialize all application tables transactionally")
    fill = commands.add_parser("fill", help="Fill a date or inclusive date range")
    fill.add_argument("domain", choices=("usage", "prices"))
    fill.add_argument("date", type=date.fromisoformat, help="YYYY-MM-DD in Europe/Brussels")
    fill.add_argument("--end-date", type=date.fromisoformat, help="Inclusive last date")
    chart = commands.add_parser("chart", help="Export a chart from PostgreSQL without fetching")
    chart.add_argument("domain", choices=("prices",))
    chart.add_argument("date", type=date.fromisoformat)
    chart.add_argument("--output", required=True, type=Path, help="PNG output path")
    chart.add_argument("--threshold", type=_threshold, default=Decimal("300"), help="Actual price threshold in EUR/MWh")
    args = parser.parse_args(argv)
    try:
        if args.command == "init-db":
            initialize_schema()
            print("Database schema initialized.")
        elif args.command == "fill":
            repository = EnergyBalanceRepository() if args.domain == "usage" else DayAheadPriceRepository()
            repository.fill(
                args.date, args.end_date,
                on_result=lambda r: print(f"{r.day}: {'stored' if r.inserted else 'already stored; skipped'}"),
            )
        else:
            from .charts import save_png
            from .prices.chart import create_price_chart

            day = DayAheadPriceRepository().load_day(args.date)
            if day is None:
                raise ValueError(f"No stored prices for {args.date}. Run home-energy fill prices {args.date} first.")
            figure = create_price_chart(day, threshold=args.threshold)
            try:
                save_png(figure, args.output)
            finally:
                figure.clear()
            print(f"Chart saved to {args.output}.")
        return 0
    except FillError as exc:
        print(f"Fill stopped at {exc.day}. Earlier committed days are retained; rerun to resume.", file=sys.stderr)
        _report_error(exc.cause)
    except (FusionSolarError, PriceAPIError, DatabaseConfigurationError, ValueError,
            KeyError, TypeError, OSError, psycopg.Error) as exc:
        _report_error(exc)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
