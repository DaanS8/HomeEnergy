"""Shared inclusive-range filling with immutable skips and per-day progress."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable


@dataclass(frozen=True)
class FillResult:
    day: date
    inserted: bool


class FillError(RuntimeError):
    def __init__(self, day: date, cause: Exception):
        self.day = day
        self.cause = cause
        super().__init__(f"Fill failed for {day.isoformat()} ({type(cause).__name__}).")


def validate_range(start: date, end: date | None = None) -> date:
    end = start if end is None else end
    if any(not isinstance(day, date) or isinstance(day, datetime) for day in (start, end)):
        raise ValueError("Dates must be datetime.date values.")
    if start > end:
        raise ValueError("Range start must not be after its end.")
    return end


def fill_missing_days(start: date, end: date, *, has_day: Callable[[date], bool],
                      fetch_and_save: Callable[[date], bool],
                      on_result: Callable[[FillResult], None] | None = None) -> list[FillResult]:
    validate_range(start, end)
    results = []
    selected = start
    while True:
        try:
            inserted = False if has_day(selected) else fetch_and_save(selected)
        except Exception as exc:
            raise FillError(selected, exc) from exc
        result = FillResult(selected, inserted)
        results.append(result)
        if on_result is not None:
            on_result(result)
        if selected == end:
            return results
        selected += timedelta(days=1)
