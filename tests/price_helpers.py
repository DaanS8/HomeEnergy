"""Synthetic complete Belgian price days; no API credentials or network."""

from datetime import date

import pandas as pd

from home_energy.prices.entsoe import normalize_prices
from home_energy.prices.models import day_bounds, interval_duration


def price_series(day=date(2026, 10, 10), value="123.4567"):
    start, end = day_bounds(day)
    index = pd.date_range(start, end, freq=interval_duration(day), inclusive="left")
    return pd.Series([value] * len(index), index=index)


def price_day(day=date(2026, 10, 10), value="123.4567"):
    return normalize_prices(price_series(day, value), day)
