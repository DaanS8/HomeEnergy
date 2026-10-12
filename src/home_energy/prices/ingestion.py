"""Ingest immutable ENTSO-E price snapshots through the shared filling loop."""

from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from ..filling import FillResult, fill_missing_days, validate_range
from .entsoe import DayAheadPriceClient
from .models import TIMEZONE
from .repository import DayAheadPriceRepository


def validate_price_range(start, end=None, now=None):
    end = validate_range(start, end)
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE)).date()
    if end > today + timedelta(days=1):
        raise ValueError("Price fills support past dates, today, and tomorrow only.")
    return end


class DayAheadPriceIngestionService:
    def __init__(self, repository=None, *, client_factory=None,
                 now: Callable[[], datetime] | None = None):
        self.repository = repository if repository is not None else DayAheadPriceRepository()
        self.client_factory = client_factory if client_factory is not None else DayAheadPriceClient.from_config
        self.now = now if now is not None else lambda: datetime.now(timezone.utc)

    def fill(self, start: date, end: date | None = None,
             on_result: Callable[[FillResult], None] | None = None) -> list[FillResult]:
        end = validate_price_range(start, end, self.now())
        with ExitStack() as stack:
            client = None

            def fetch_and_save(selected):
                nonlocal client
                if client is None:
                    client = stack.enter_context(self.client_factory())
                day = client.get_day(selected)
                day.validate()
                return self.repository.save_day(day)

            return fill_missing_days(start, end, has_day=self.repository.has_day,
                                     fetch_and_save=fetch_and_save, on_result=on_result)
