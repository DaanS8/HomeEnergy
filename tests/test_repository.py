"""Opt-in PostgreSQL tests, contained in disposable per-test schemas."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql

from home_energy.usage import EnergyBalanceRepository
from home_energy.database import connect
from home_energy.usage.constants import PLANT_ID, PLANT_TIMEZONE
from home_energy.usage.filling import DataFetcher
from home_energy.filling import FillError
from helpers import energy_payload
from test_energy_balance import parsed


@unittest.skipUnless(os.environ.get("HOMEENERGY_TEST_DATABASE") == "1",
                     "Start PostgreSQL and set HOMEENERGY_TEST_DATABASE=1")
class RepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.schema = "home_energy_test_" + uuid4().hex
        namespace = patch("home_energy.database.schema.SCHEMA_NAME", self.schema)
        namespace.start()
        self.addCleanup(namespace.stop)
        self.addCleanup(self.drop_schema)
        self.repository = EnergyBalanceRepository(self.open_connection)
        self.repository.initialize_schema()

    def open_connection(self):
        connection = connect()
        try:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    def drop_schema(self):
        # Only the unique schema created by this test is ever dropped.
        if not self.schema.startswith("home_energy_test_"):
            raise ValueError("Unexpected integration-test schema")
        with connect() as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    def test_initialization_creates_project_namespace_and_all_tables(self):
        with connect() as connection:
            tables = connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
                (self.schema,),
            ).fetchall()
        self.assertEqual(tables, [("daily_metrics",), ("day_ahead_price_days",), ("energy_balance_days",), ("measurement_samples",), ("price_intervals",)])

    def test_repository_uses_its_namespace_even_with_a_default_connection(self):
        source = parsed()
        self.repository.save_day(source)
        repository = EnergyBalanceRepository(connect)
        self.assertEqual(repository.load_day(source.day).samples, source.samples)

    def test_idempotent_initialization_and_full_round_trip(self):
        self.repository.initialize_schema()
        source = parsed(units={"productPower": "verified-unit"})
        self.assertTrue(self.repository.save_day(source))
        loaded = self.repository.load_day(source.day)
        self.assertEqual(loaded.samples, source.samples)
        self.assertEqual({m.name: m for m in loaded.metrics}, {m.name: m for m in source.metrics})
        for field in ("plant_id", "day", "timezone", "fetched_at", "sample_count",
                      "available_series", "empty_series", "metadata"):
            self.assertEqual(getattr(loaded, field), getattr(source, field))

    def test_existing_day_is_never_overwritten(self):
        source = parsed()
        self.repository.save_day(source)
        changed = energy_payload()
        changed["data"]["productPower"][0] = "99"
        self.assertFalse(self.repository.save_day(parsed(changed)))
        self.assertEqual(self.repository.load_day(source.day).samples, source.samples)

    def test_child_insert_failure_rolls_back_parent_and_all_children(self):
        with self.open_connection() as connection:
            connection.execute("ALTER TABLE daily_metrics ADD CONSTRAINT injected_failure CHECK (value < 1)")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.repository.save_day(parsed())
        self.assertFalse(self.repository.has_day(date(2026, 10, 10)))
        with self.open_connection() as connection:
            for table in ("energy_balance_days", "measurement_samples", "daily_metrics"):
                count = connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
                self.assertEqual(count, 0)
            connection.execute("ALTER TABLE daily_metrics DROP CONSTRAINT injected_failure")
        self.assertTrue(self.repository.save_day(parsed()))

    def test_validation_prevents_a_partial_day(self):
        source = parsed()
        source.samples.pop()
        with self.assertRaises(ValueError):
            self.repository.save_day(source)
        self.assertFalse(self.repository.has_day(source.day))

    def test_concurrent_inserts_commit_one_complete_winner(self):
        barrier = Barrier(2)
        def save(value):
            payload = energy_payload()
            payload["data"]["productPower"][0] = value
            source = parsed(payload)
            barrier.wait(timeout=10)
            return value, self.repository.save_day(source)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["1", "9"]))
        self.assertEqual(sorted(inserted for _, inserted in results), [False, True])
        winner = next(value for value, inserted in results if inserted)
        loaded = self.repository.load_day(date(2026, 10, 10))
        self.assertEqual(loaded.samples[0].original_value, winner)
        self.assertEqual(len(loaded.samples), 6)
        self.assertEqual(len(loaded.metrics), 3)

    def test_database_queries_return_ordered_dates_and_gaps_without_api(self):
        for day in (date(2026, 10, 10), date(2026, 10, 8)):
            self.repository.save_day(parsed(day=day))
        self.assertIsNone(self.repository.load_day(date(2026, 10, 9)))
        days = self.repository.load_range(date(2026, 10, 8), date(2026, 10, 10))
        self.assertEqual([d.day for d in days], [date(2026, 10, 8), date(2026, 10, 10)])
        self.assertEqual(self.repository.list_available_dates(), [d.day for d in days])
        self.assertEqual(self.repository.list_available_dates(start=date(2026, 10, 9)), [date(2026, 10, 10)])
        self.assertEqual(self.repository.list_available_dates(end=date(2026, 10, 9)), [date(2026, 10, 8)])

    def test_range_failure_resume_and_queries_use_committed_days(self):
        from datetime import datetime, timezone
        client = MagicMock()
        client.plant_id = PLANT_ID
        client.timezone = PLANT_TIMEZONE
        client.config = {}
        def fetch(day):
            if day == date(2026, 10, 9):
                return {"data": {}}
            return energy_payload(day)
        client.get_energy_balance.side_effect = fetch
        fetcher = DataFetcher(client, self.repository, now=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))
        with self.assertRaises(FillError):
            fetcher.fill(date(2026, 10, 8), date(2026, 10, 10))
        self.assertEqual(self.repository.list_available_dates(), [date(2026, 10, 8)])
        client.get_energy_balance.reset_mock()
        client.get_energy_balance.side_effect = lambda day: energy_payload(day)
        self.assertEqual([r.inserted for r in fetcher.fill(date(2026, 10, 8), date(2026, 10, 10))],
                         [False, True, True])
        client.get_energy_balance.assert_any_call(date(2026, 10, 9))
        self.assertEqual(client.get_energy_balance.call_count, 2)
        client.get_energy_balance.reset_mock()
        self.repository.load_day(date(2026, 10, 10))
        self.repository.load_range(date(2026, 10, 8), date(2026, 10, 10))
        self.repository.list_available_dates()
        client.get_energy_balance.assert_not_called()

    def test_fixed_plant_fill_load_and_repeat_without_api_request(self):
        selected = date(2000, 6, 10)
        payload = energy_payload(selected)
        payload["data"]["totalUsePower"] = "14.060"
        repository = EnergyBalanceRepository(self.open_connection)
        with patch("home_energy.usage.repository.FusionSolarClient") as api:
            client = api.from_config.return_value.__enter__.return_value
            client.plant_id = PLANT_ID
            client.timezone = PLANT_TIMEZONE
            client.config = {}
            client.get_energy_balance.return_value = payload
            self.assertTrue(repository.fill(selected)[0].inserted)
            loaded = repository.load_day(selected)
            self.assertEqual(loaded.consumed_energy, Decimal("14.060"))
            self.assertEqual(len(loaded.samples), 6)
            client.get_energy_balance.assert_called_once_with(selected)
            client.get_energy_balance.reset_mock()
            self.assertFalse(repository.fill(selected)[0].inserted)
            client.get_energy_balance.assert_not_called()

    def test_reads_need_no_api_configuration_or_client(self):
        self.repository.save_day(parsed())
        repository = EnergyBalanceRepository(self.open_connection)
        with patch("home_energy.usage.fusionsolar.load_config", side_effect=AssertionError("No API config needed")), \
                patch("home_energy.usage.repository.FusionSolarClient") as api:
            self.assertEqual(repository.load_day(date(2026, 10, 10)).plant_id, PLANT_ID)
            api.from_config.assert_not_called()

    def test_save_rejects_another_plant_before_writing(self):
        source = parsed()
        source.plant_id = "OTHER"
        with self.assertRaisesRegex(ValueError, "belong to our plant"):
            self.repository.save_day(source)
        self.assertEqual(self.repository.list_available_dates(), [])
