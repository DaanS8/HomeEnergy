"""Prepare a persisted price day and an in-memory report, independently of HTTP."""

import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from threading import Lock
from zoneinfo import ZoneInfo

from ..charts import COLORS, chart_style
from .analysis import DEFAULT_THRESHOLD, PriceRange, analyse_prices
from .chart import create_price_chart
from .repository import DayAheadPriceRepository, validate_price_range


_RENDER_LOCK = Lock()


class ReportRequestError(ValueError):
    """Invalid caller input; distinct from errors processing stored/provider data."""


@dataclass(frozen=True)
class PriceReport:
    day: date
    market: str
    timezone: str
    threshold: Decimal
    fetched_at: datetime
    high_price_ranges: tuple[PriceRange, ...]
    warning: str | None
    png: bytes


def _time_label(instant: datetime, day: date, zone: ZoneInfo) -> str:
    local = instant.astimezone(zone)
    label = local.strftime("%H:%M")
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        label += local.strftime(" (%z)")
    if local.date() != day:
        label = f"{local.date()} {label}"
    return label


def warning_summary(ranges: tuple[PriceRange, ...], day: date, timezone: str) -> str | None:
    zone = ZoneInfo(timezone)
    labels = [f"van {_time_label(r.start_at, day, zone)} tot {_time_label(r.end_at, day, zone)}"
              for r in ranges]
    if not labels:
        return None
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " en " + labels[-1]


def _render_png(day, threshold: Decimal) -> bytes:
    # rcParams and figure rendering are shared process state even with Agg.
    with _RENDER_LOCK:
        figure = create_price_chart(day, threshold=threshold)
        try:
            with io.BytesIO() as buffer, chart_style():
                figure.savefig(buffer, format="png", dpi=180, bbox_inches="tight",
                               facecolor=COLORS["background"])
                return buffer.getvalue()
        finally:
            figure.clear()


class DayAheadReportService:
    def __init__(self, repository: DayAheadPriceRepository | None = None):
        self.repository = repository if repository is not None else DayAheadPriceRepository()

    def __call__(self, selected_date: date, threshold: Decimal = DEFAULT_THRESHOLD) -> PriceReport:
        try:
            validate_price_range(selected_date)
        except ValueError as exc:
            raise ReportRequestError(str(exc)) from None
        try:
            if isinstance(threshold, bool):
                raise ValueError
            threshold = Decimal(str(threshold))
            if not threshold.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ReportRequestError("Threshold must be a finite number in EUR/MWh.") from None

        self.repository.fill(selected_date)
        day = self.repository.load_day(selected_date)
        if day is None:
            raise RuntimeError("A completed price fill did not produce a stored day.")
        analysis = analyse_prices(day, threshold=threshold)
        png = _render_png(day, threshold)
        return PriceReport(day.day, day.market, day.timezone, threshold, day.fetched_at,
                           analysis.high_price_ranges,
                           warning_summary(analysis.high_price_ranges, day.day, day.timezone), png)
