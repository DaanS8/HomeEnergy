"""ENTSO-E adapter: fetch native prices and validate a complete local day."""

import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from dotenv import dotenv_values
from entsoe import EntsoePandasClient
from entsoe.exceptions import NoMatchingDataError

from .models import MARKET, TIMEZONE, DayAheadPriceDay, PriceInterval, day_bounds, interval_duration


class PriceAPIError(RuntimeError):
    """Safe public API error; upstream request URLs can contain credentials."""


class PricesUnavailableError(PriceAPIError):
    """The selected day has no published prices yet."""


class PriceConfigurationError(ValueError):
    """Missing or unreadable provider settings, without credential values."""


class InvalidPriceDataError(PriceAPIError):
    """A provider response failed complete-day validation."""


def normalize_prices(prices, day: date, *, fetched_at: datetime | None = None) -> DayAheadPriceDay:
    start, end = day_bounds(day)
    if isinstance(prices, pd.DataFrame):
        if prices.shape[1] != 1:
            raise ValueError("Expected a single price column.")
        prices = prices.iloc[:, 0]
    if not isinstance(prices, pd.Series) or prices.empty:
        raise PricesUnavailableError(f"No published prices available for {day}.")
    if not isinstance(prices.index, pd.DatetimeIndex) or prices.index.tz is None:
        raise ValueError("Price timestamps must be timezone-aware; naive times cannot be resolved safely.")
    prices = prices.copy()
    prices.index = prices.index.tz_convert("UTC")
    prices = prices[(prices.index >= start) & (prices.index < end)].sort_index()
    if prices.empty:
        raise PricesUnavailableError(f"No published prices available for {day}.")
    if prices.index.has_duplicates:
        raise ValueError("Duplicate price timestamps are not a valid day.")
    duration = interval_duration(day)
    intervals = []
    for timestamp, value in prices.items():
        if isinstance(value, bool):
            raise ValueError("Price readings must be numeric.")
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("Invalid numeric price reading.") from None
        if not price.is_finite():
            raise ValueError("Missing or nonfinite price reading.")
        instant = timestamp.to_pydatetime(warn=False)
        if timestamp.nanosecond:
            raise ValueError("Price timestamps must align with native interval boundaries.")
        intervals.append(PriceInterval(instant, instant + duration, price))
    result = DayAheadPriceDay(day, MARKET, TIMEZONE, fetched_at or datetime.now(timezone.utc), intervals)
    result.validate()
    return result


class DayAheadPriceClient:
    def __init__(self, api_key: str, *, timeout: int = 30):
        if not isinstance(api_key, str) or not api_key.strip():
            raise PriceConfigurationError("Set ENTSOE_API_KEY to fetch day-ahead prices.")
        self.client = EntsoePandasClient(api_key=api_key, timeout=timeout, retry_count=3, retry_delay=1)

    @classmethod
    def from_config(cls):
        try:
            settings = dotenv_values(Path(".env"), interpolate=False, encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise PriceConfigurationError("Cannot read ENTSO-E configuration. Check .env.") from None
        settings.update(os.environ)
        return cls(settings.get("ENTSOE_API_KEY"))

    def get_day(self, day: date) -> DayAheadPriceDay:
        start, end = day_bounds(day)
        try:
            prices = self.client.query_day_ahead_prices(
                country_code=MARKET, start=pd.Timestamp(start).tz_convert(TIMEZONE),
                end=pd.Timestamp(end).tz_convert(TIMEZONE),
            )
        except NoMatchingDataError:
            raise PricesUnavailableError(f"No published prices available for {day}; retry later.") from None
        except Exception as exc:
            # Never expose an upstream exception: requests URLs contain securityToken.
            raise PriceAPIError(f"ENTSO-E request failed for {day} ({type(exc).__name__}).") from None
        try:
            return normalize_prices(prices, day)
        except ValueError:
            raise InvalidPriceDataError(f"Invalid ENTSO-E price data for {day}.") from None

    def close(self):
        self.client.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
