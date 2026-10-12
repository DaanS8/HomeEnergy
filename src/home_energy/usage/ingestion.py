"""Ingest complete FusionSolar snapshots through the shared filling loop."""

from contextlib import ExitStack
from datetime import date, datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from ..filling import FillResult, fill_missing_days, validate_range
from .constants import PLANT_TIMEZONE
from .consumption import validate_complete_day
from .energy_balance import parse_energy_balance
from .fusionsolar import FusionSolarClient
from .repository import EnergyBalanceRepository


class UsageConfigurationError(ValueError):
    """FusionSolar client configuration could not be loaded."""


def validate_usage_range(start, end=None, now=None):
    end = validate_range(start, end)
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(PLANT_TIMEZONE)).date()
    if end >= today:
        raise ValueError("Only past dates in the plant timezone can be filled.")
    return end


class UsageIngestionService:
    def __init__(self, repository=None, *, client_factory=None,
                 now: Callable[[], datetime] | None = None):
        self.repository = repository if repository is not None else EnergyBalanceRepository()
        self.client_factory = client_factory if client_factory is not None else FusionSolarClient.from_config
        self.now = now if now is not None else lambda: datetime.now(timezone.utc)

    def fill(self, start: date, end: date | None = None,
             on_result: Callable[[FillResult], None] | None = None) -> list[FillResult]:
        end = validate_usage_range(start, end, self.now())
        with ExitStack() as stack:
            client = None

            def fetch_and_save(selected):
                nonlocal client
                if client is None:
                    try:
                        client = stack.enter_context(self.client_factory())
                    except ValueError:
                        raise UsageConfigurationError("Invalid FusionSolar configuration.") from None
                day = parse_energy_balance(
                    client.get_energy_balance(selected), plant_id=client.plant_id, day=selected,
                    timezone_name=client.timezone, fetched_at=self.now(), units=client.config.get("units"),
                )
                validate_complete_day(day)
                return self.repository.save_day(day)

            return fill_missing_days(start, end, has_day=self.repository.has_day,
                                     fetch_and_save=fetch_and_save, on_result=on_result)
