"""Prepare a persisted price day and an in-memory report, independently of HTTP."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..charts import render_png
from .analysis import (DEFAULT_HYSTERESIS, DEFAULT_THRESHOLD, PriceRange, analyse_prices,
                       validate_price_thresholds)
from .chart import create_price_chart
from .ingestion import DayAheadPriceIngestionService, validate_price_range
from .repository import DayAheadPriceRepository


class ReportRequestError(ValueError):
    """Invalid caller input; distinct from errors processing stored/provider data."""


@dataclass(frozen=True)
class PriceReport:
    day: date
    market: str
    timezone: str
    threshold: Decimal
    hysteresis: Decimal
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


def _render_png(day, threshold: Decimal, hysteresis: Decimal, analysis) -> bytes:
    return render_png(lambda: create_price_chart(day, threshold=threshold, hysteresis=hysteresis,
                                                analysis=analysis))


class DayAheadReportService:
    def __init__(self, repository: DayAheadPriceRepository | None = None, *, ingestion_service=None,
                 now=None):
        if repository is None:
            repository = ingestion_service.repository if ingestion_service is not None else DayAheadPriceRepository()
        if ingestion_service is not None and ingestion_service.repository is not repository:
            raise ValueError("Report and ingestion must use the same repository.")
        self.repository = repository
        self.now = now if now is not None else (
            ingestion_service.now if ingestion_service is not None else lambda: datetime.now(timezone.utc)
        )
        self.ingestion_service = ingestion_service if ingestion_service is not None else DayAheadPriceIngestionService(
            repository, now=self.now,
        )

    def __call__(self, selected_date: date, threshold: Decimal = DEFAULT_THRESHOLD, *,
                 hysteresis: Decimal = DEFAULT_HYSTERESIS) -> PriceReport:
        try:
            validate_price_range(selected_date, now=self.now())
        except ValueError as exc:
            raise ReportRequestError(str(exc)) from None
        try:
            threshold, hysteresis = validate_price_thresholds(threshold, hysteresis)
        except ValueError as exc:
            raise ReportRequestError(str(exc)) from None

        self.ingestion_service.fill(selected_date)
        day = self.repository.load_day(selected_date)
        if day is None:
            raise RuntimeError("A completed price fill did not produce a stored day.")
        analysis = analyse_prices(day, threshold=threshold, hysteresis=hysteresis)
        png = _render_png(day, threshold, hysteresis, analysis)
        return PriceReport(day.day, day.market, day.timezone, threshold, hysteresis, day.fetched_at,
                           analysis.high_price_ranges,
                           warning_summary(analysis.high_price_ranges, day.day, day.timezone), png)
