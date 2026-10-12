"""Prepare daily EV analysis and a PNG independently of HTTP."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from ..charts import render_png
from .chart import create_ev_usage_chart
from .consumption import ZONE
from .daily import analyse_daily_usage, validate_daily_date
from .ingestion import UsageIngestionService
from .models import DailyUsage
from .repository import EnergyBalanceRepository


@dataclass(frozen=True)
class EVUsageReport:
    usage: DailyUsage
    png: bytes


class EVUsageReportService:
    def __init__(self, repository=None, *, ingestion_service=None,
                 now: Callable[[], datetime] | None = None):
        if repository is None:
            repository = ingestion_service.repository if ingestion_service is not None else EnergyBalanceRepository()
        if ingestion_service is not None and ingestion_service.repository is not repository:
            raise ValueError("Report and ingestion must use the same repository.")
        self.repository = repository
        self.now = now if now is not None else (
            ingestion_service.now if ingestion_service is not None else lambda: datetime.now(timezone.utc)
        )
        self.ingestion_service = ingestion_service if ingestion_service is not None else UsageIngestionService(
            repository, now=self.now,
        )

    def __call__(self, selected: date | None = None) -> EVUsageReport:
        now = self.now()
        if now.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        selected = selected if selected is not None else now.astimezone(ZONE).date() - timedelta(days=2)
        validate_daily_date(selected, now)
        previous, following = selected - timedelta(days=1), selected + timedelta(days=1)
        self.ingestion_service.fill(previous, following)
        # Always use committed snapshots, including when a concurrent insert wins.
        usage = analyse_daily_usage(self.repository.load_range(previous, following), selected, now=now)
        return EVUsageReport(usage, render_png(lambda: create_ev_usage_chart(usage)))
