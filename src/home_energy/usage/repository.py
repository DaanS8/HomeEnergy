"""Persist immutable usage snapshots and query stored Python objects."""

from datetime import date

from psycopg.types.json import Jsonb

from .constants import PLANT_ID
from .consumption import validate_complete_day
from ..filling import validate_range
from .models import DailyMetric, EnergyBalanceDay, MeasurementSample
from ..database.schema import Repository


class EnergyBalanceRepository(Repository):
    """Store and query our fixed plant; reads only access PostgreSQL.

    connection_factory can supply connections to an isolated database for tests.
    Stored days are never updated. Direct SQL administration is outside this API.
    """

    def has_day(self, day: date) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM energy_balance_days WHERE plant_id = %s AND selected_date = %s",
                (PLANT_ID, day),
            ).fetchone() is not None

    def save_day(self, day: EnergyBalanceDay) -> bool:
        """Return True for insertion, False for an existing immutable day.

        Structural validation and complete consumption readings with a daily
        total are required before any database access. The parent and all children
        commit in a single transaction; any exception removes the entire new day.
        """
        day.validate()
        if day.plant_id != PLANT_ID:
            raise ValueError("A stored day must belong to our plant.")
        validate_complete_day(day)
        with self._connection() as connection:
            row = connection.execute(
                "INSERT INTO energy_balance_days "
                "(plant_id, selected_date, timezone, fetched_at, sample_count, available_series, empty_series, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (plant_id, selected_date) DO NOTHING RETURNING id",
                (day.plant_id, day.day, day.timezone, day.fetched_at, day.sample_count,
                 day.available_series, day.empty_series, Jsonb(day.metadata)),
            ).fetchone()
            if row is None:
                return False
            day_id = row[0]
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO measurement_samples "
                    "(day_id, sample_index, axis_label, observed_at, measurement, value, original_value, unit) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [(day_id, s.sample_index, s.axis_label, s.observed_at, s.measurement,
                      s.value, s.original_value, s.unit) for s in day.samples],
                )
                if day.metrics:
                    cursor.executemany(
                        "INSERT INTO daily_metrics (day_id, name, value, original_value, unit) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [(day_id, m.name, m.value, m.original_value, m.unit) for m in day.metrics],
                    )
        return True

    def load_day(self, day: date) -> EnergyBalanceDay | None:
        days = self.load_range(day, day)
        return days[0] if days else None

    def load_range(self, start: date, end: date) -> list[EnergyBalanceDay]:
        validate_range(start, end)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, plant_id, selected_date, timezone, fetched_at, sample_count, "
                "available_series, empty_series, metadata FROM energy_balance_days "
                "WHERE plant_id = %s AND selected_date BETWEEN %s AND %s ORDER BY selected_date",
                (PLANT_ID, start, end),
            ).fetchall()
            if not rows:
                return []
            days = {r[0]: EnergyBalanceDay(*r[1:8], samples=[], metadata=r[8]) for r in rows}
            samples = connection.execute(
                "SELECT day_id, sample_index, axis_label, observed_at, measurement, value, original_value, unit "
                "FROM measurement_samples WHERE day_id = ANY(%s) ORDER BY day_id, sample_index, measurement",
                (list(days),),
            ).fetchall()
            metrics = connection.execute(
                "SELECT day_id, name, value, original_value, unit FROM daily_metrics "
                "WHERE day_id = ANY(%s) ORDER BY day_id, name", (list(days),),
            ).fetchall()
            for row in samples:
                days[row[0]].samples.append(MeasurementSample(*row[1:]))
            for row in metrics:
                days[row[0]].metrics.append(DailyMetric(*row[1:]))
            # Restore the source's series order as well as sample order.
            for day in days.values():
                order = {name: index for index, name in enumerate(day.available_series)}
                day.samples.sort(key=lambda s: (order[s.measurement], s.sample_index))
            return list(days.values())

    def list_available_dates(self, start: date | None = None, end: date | None = None) -> list[date]:
        if start is not None and end is not None and start > end:
            raise ValueError("Range start must not be after its end.")
        clauses, values = ["plant_id = %s"], [PLANT_ID]
        if start is not None:
            clauses.append("selected_date >= %s")
            values.append(start)
        if end is not None:
            clauses.append("selected_date <= %s")
            values.append(end)
        with self._connection() as connection:
            return [row[0] for row in connection.execute(
                "SELECT selected_date FROM energy_balance_days WHERE " + " AND ".join(clauses) +
                " ORDER BY selected_date", values,
            ).fetchall()]
