"""Opt-in PostgreSQL tests, contained in disposable per-test schemas."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Barrier
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql

from home_energy.usage import EnergyBalanceRepository
from home_energy.database import connect
from home_energy.usage.constants import PLANT_ID
from home_energy.usage.ingestion import UsageIngestionService
from home_energy.filling import FillError
from home_energy.usage.energy_balance import EnergyBalanceValidationError
from helpers import energy_payload, parsed_energy_day as parsed
from usage_helpers import complete_energy_payload
from service_helpers import RecordingProvider


def stored_payload(day=date(2026, 10, 10)):
    payload = energy_payload(day)
    payload["data"].update(complete_energy_payload(day)["data"])
    payload["data"]["productPower"] += ["0"] * (len(payload["data"]["xAxis"]) - 3)
    return payload


def stored_day(payload=None, day=date(2026, 10, 10), **kwargs):
    return parsed(stored_payload(day) if payload is None else payload, day=day, **kwargs)


class RepositoryValidationTests(unittest.TestCase):
    def test_structurally_valid_partial_days_fail_before_database_access(self):
        for mode in ("three_samples", "missing_interval", "null_reading", "missing_total", "null_total"):
            with self.subTest(mode=mode):
                payload = complete_energy_payload(date(2026, 10, 10))
                if mode == "three_samples":
                    payload = energy_payload()
                elif mode == "missing_interval":
                    payload["data"]["xAxis"].pop()
                    payload["data"]["usePower"].pop()
                elif mode == "null_reading":
                    payload["data"]["usePower"][10] = None
                elif mode == "missing_total":
                    del payload["data"]["totalUsePower"]
                else:
                    payload["data"]["totalUsePower"] = "--"
                source = parsed(payload)
                source.validate()
                connection_factory = MagicMock()
                repository = EnergyBalanceRepository(connection_factory)
                with self.assertRaises(EnergyBalanceValidationError):
                    repository.save_day(source)
                connection_factory.assert_not_called()


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

    def test_zero_consumption_round_trips_on_normal_short_and_long_days(self):
        for selected, count in ((date(2026, 10, 10), 288), (date(2026, 3, 29), 276),
                                (date(2026, 10, 25), 300)):
            with self.subTest(selected=selected):
                payload = complete_energy_payload(selected)
                payload["data"]["usePower"] = ["0"] * count
                payload["data"]["totalUsePower"] = "0"
                source = parsed(payload, day=selected)
                self.assertTrue(self.repository.save_day(source))
                loaded = self.repository.load_day(selected)
                self.assertEqual(loaded.consumed_energy, Decimal("0"))
                self.assertEqual(len(loaded.samples), count)
                self.assertEqual(loaded.samples, source.samples)

    def test_repository_uses_its_namespace_even_with_a_default_connection(self):
        source = stored_day()
        self.repository.save_day(source)
        repository = EnergyBalanceRepository(connect)
        self.assertEqual(repository.load_day(source.day).samples, source.samples)

    def test_full_round_trip_survives_reinitialization_and_cannot_be_overwritten(self):
        self.repository.initialize_schema()
        source = stored_day(units={"productPower": "verified-unit"})
        self.assertTrue(self.repository.save_day(source))
        loaded = self.repository.load_day(source.day)
        self.assertEqual(loaded.samples, source.samples)
        self.assertEqual({m.name: m for m in loaded.metrics}, {m.name: m for m in source.metrics})
        for field in ("plant_id", "day", "timezone", "fetched_at", "sample_count",
                      "available_series", "empty_series", "metadata"):
            self.assertEqual(getattr(loaded, field), getattr(source, field))
        self.repository.initialize_schema()
        changed = stored_payload()
        changed["data"]["productPower"][0] = "99"
        self.assertFalse(self.repository.save_day(stored_day(changed)))
        self.assertEqual(self.repository.load_day(source.day).samples, source.samples)

    def test_child_insert_failure_rolls_back_parent_and_all_children(self):
        with self.open_connection() as connection:
            connection.execute("ALTER TABLE daily_metrics ADD CONSTRAINT injected_failure CHECK (value < 1)")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.repository.save_day(stored_day())
        self.assertFalse(self.repository.has_day(date(2026, 10, 10)))
        with self.open_connection() as connection:
            for table in ("energy_balance_days", "measurement_samples", "daily_metrics"):
                count = connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
                self.assertEqual(count, 0)
            connection.execute("ALTER TABLE daily_metrics DROP CONSTRAINT injected_failure")
        self.assertTrue(self.repository.save_day(stored_day()))

    def test_incomplete_consumption_or_missing_total_leaves_day_available_to_fill(self):
        for mode in ("missing_interval", "missing_total", "missing_sample"):
            with self.subTest(mode=mode):
                payload = stored_payload()
                if mode == "missing_interval":
                    for name in ("xAxis", "usePower", "productPower"):
                        payload["data"][name].pop()
                elif mode == "missing_total":
                    del payload["data"]["totalUsePower"]
                source = stored_day(payload)
                if mode == "missing_sample":
                    source.samples.pop()
                with self.assertRaises(ValueError):
                    self.repository.save_day(source)
                self.assertFalse(self.repository.has_day(source.day))
                self.assertIsNone(self.repository.load_day(source.day))
        self.assertTrue(self.repository.save_day(stored_day()))

    def test_concurrent_inserts_commit_one_complete_winner(self):
        barrier = Barrier(2)
        def save(value):
            payload = stored_payload()
            payload["data"]["productPower"][0] = value
            source = stored_day(payload)
            barrier.wait(timeout=10)
            return value, self.repository.save_day(source)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["1", "9"]))
        self.assertEqual(sorted(inserted for _, inserted in results), [False, True])
        winner = next(value for value, inserted in results if inserted)
        loaded = self.repository.load_day(date(2026, 10, 10))
        self.assertEqual(loaded.samples[0].original_value, winner)
        self.assertEqual(len(loaded.samples), 576)
        self.assertEqual(len(loaded.metrics), 4)

    def test_database_queries_return_ordered_dates_and_gaps_without_api(self):
        for day in (date(2026, 10, 10), date(2026, 10, 8)):
            self.repository.save_day(stored_day(day=day))
        self.assertIsNone(self.repository.load_day(date(2026, 10, 9)))
        days = self.repository.load_range(date(2026, 10, 8), date(2026, 10, 10))
        self.assertEqual([d.day for d in days], [date(2026, 10, 8), date(2026, 10, 10)])
        self.assertEqual(self.repository.list_available_dates(), [d.day for d in days])
        self.assertEqual(self.repository.list_available_dates(start=date(2026, 10, 9)), [date(2026, 10, 10)])
        self.assertEqual(self.repository.list_available_dates(end=date(2026, 10, 9)), [date(2026, 10, 8)])

    def test_range_failure_resume_and_queries_use_committed_days(self):
        def complete(day):
            payload = complete_energy_payload(day)
            payload["data"]["totalUsePower"] = "14.060"
            return payload

        def fetch(day):
            if day == date(2026, 10, 9):
                return {"data": {}}
            return complete(day)
        client = RecordingProvider(fetch)
        fetcher = UsageIngestionService(self.repository, client_factory=lambda: client, now=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))
        with self.assertRaises(FillError):
            fetcher.fill(date(2026, 10, 8), date(2026, 10, 10))
        self.assertEqual(self.repository.list_available_dates(), [date(2026, 10, 8)])
        client.requests.clear()
        client.fetch = complete
        self.assertEqual([r.inserted for r in fetcher.fill(date(2026, 10, 8), date(2026, 10, 10))],
                         [False, True, True])
        self.assertEqual(client.requests, [date(2026, 10, 9), date(2026, 10, 10)])
        repository = EnergyBalanceRepository(self.open_connection)
        with patch("home_energy.usage.ingestion.FusionSolarClient.from_config", side_effect=AssertionError("No API config needed")):
            loaded = repository.load_day(date(2026, 10, 10))
            self.assertEqual(loaded.plant_id, PLANT_ID)
            self.assertEqual(loaded.consumed_energy, Decimal("14.060"))
            self.assertEqual(len(loaded.samples), 288)
            self.assertEqual([day.day for day in repository.load_range(date(2026, 10, 8), date(2026, 10, 10))],
                             [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)])
            replay = UsageIngestionService(repository, now=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))
            self.assertEqual([r.inserted for r in replay.fill(date(2026, 10, 8), date(2026, 10, 10))],
                             [False] * 3)

    def test_save_rejects_another_plant_before_writing(self):
        source = stored_day()
        source.plant_id = "OTHER"
        with self.assertRaisesRegex(ValueError, "belong to our plant"):
            self.repository.save_day(source)
        self.assertEqual(self.repository.list_available_dates(), [])
