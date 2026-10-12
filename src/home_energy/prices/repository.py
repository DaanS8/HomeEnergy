"""Atomic price snapshots and database-only Python object queries."""

from datetime import date, timezone

from ..database.schema import Repository
from ..filling import validate_range
from .models import MARKET, DayAheadPriceDay, PriceInterval


class DayAheadPriceRepository(Repository):
    def has_day(self, day: date) -> bool:
        validate_range(day)
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM day_ahead_price_days WHERE market = %s AND selected_date = %s",
                (MARKET, day),
            ).fetchone() is not None

    def save_day(self, day: DayAheadPriceDay) -> bool:
        day.validate()
        with self._connection() as connection:
            row = connection.execute(
                "INSERT INTO day_ahead_price_days (market, selected_date, timezone, fetched_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (market, selected_date) DO NOTHING RETURNING id",
                (day.market, day.day, day.timezone, day.fetched_at),
            ).fetchone()
            if row is None:
                return False
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO price_intervals (day_id, start_at, end_at, price_eur_per_mwh) "
                    "VALUES (%s, %s, %s, %s)",
                    [(row[0], i.start_at, i.end_at, i.price_eur_per_mwh) for i in day.intervals],
                )
        return True

    def load_day(self, day: date) -> DayAheadPriceDay | None:
        days = self.load_range(day, day)
        return days[0] if days else None

    def load_range(self, start: date, end: date) -> list[DayAheadPriceDay]:
        validate_range(start, end)
        # One joined statement keeps the parent and interval reads in one snapshot.
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT d.id, d.selected_date, d.market, d.timezone, d.fetched_at, "
                "i.start_at, i.end_at, i.price_eur_per_mwh FROM day_ahead_price_days d "
                "JOIN price_intervals i ON i.day_id = d.id "
                "WHERE d.market = %s AND d.selected_date BETWEEN %s AND %s "
                "ORDER BY d.selected_date, i.start_at", (MARKET, start, end),
            ).fetchall()
        days = {}
        for row in rows:
            if row[0] not in days:
                days[row[0]] = DayAheadPriceDay(*row[1:5], intervals=[])
            days[row[0]].intervals.append(PriceInterval(row[5].astimezone(timezone.utc),
                                                      row[6].astimezone(timezone.utc), row[7]))
        return list(days.values())

    def list_available_dates(self, start: date | None = None, end: date | None = None) -> list[date]:
        if start is not None:
            validate_range(start)
        if end is not None:
            validate_range(end)
        if start is not None and end is not None:
            validate_range(start, end)
        clauses, values = ["market = %s"], [MARKET]
        if start is not None:
            clauses.append("selected_date >= %s")
            values.append(start)
        if end is not None:
            clauses.append("selected_date <= %s")
            values.append(end)
        with self._connection() as connection:
            return [row[0] for row in connection.execute(
                "SELECT selected_date FROM day_ahead_price_days WHERE " + " AND ".join(clauses) +
                " ORDER BY selected_date", values,
            ).fetchall()]
