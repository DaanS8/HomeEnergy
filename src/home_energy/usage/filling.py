"""FusionSolar past-day policy and parsing around the shared filling loop."""

from datetime import date, datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from ..filling import FillResult, fill_missing_days, validate_range
from .constants import PLANT_TIMEZONE
from .energy_balance import parse_energy_balance
from .fusionsolar import FusionSolarClient


def validate_usage_range(start, end=None, now=None):
    end = validate_range(start, end)
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(PLANT_TIMEZONE)).date()
    if end >= today:
        raise ValueError("Only past dates in the plant timezone can be filled.")
    return end


class DataFetcher:
    def __init__(self, client: FusionSolarClient, repository,
                 now: Callable[[], datetime] | None = None):
        self.client = client
        self.repository = repository
        self.plant_id = client.plant_id
        self.timezone = client.timezone
        self.now = now or (lambda: datetime.now(timezone.utc))

    def save(self, selected):
        payload = self.client.get_energy_balance(selected)
        day = parse_energy_balance(
            payload, plant_id=self.plant_id, day=selected,
            timezone_name=self.timezone, fetched_at=self.now(),
            units=self.client.config.get("units"),
        )
        return self.repository.save_day(day)

    def fill(self, start: date, end: date | None = None,
             on_result: Callable[[FillResult], None] | None = None) -> list[FillResult]:
        end = validate_usage_range(start, end, self.now())
        return fill_missing_days(start, end, has_day=self.repository.has_day,
                                 fetch_and_save=self.save, on_result=on_result)
