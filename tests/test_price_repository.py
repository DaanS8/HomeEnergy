"""Opt-in price integration tests in isolated, disposable PostgreSQL schemas."""

import os
import io
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
from threading import Barrier
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

from home_energy.database import connect
from home_energy.cli import main
from home_energy.filling import FillError
from home_energy.prices import DayAheadPriceRepository
from home_energy.prices.entsoe import PricesUnavailableError
from price_helpers import price_day


@unittest.skipUnless(os.environ.get("HOMEENERGY_TEST_DATABASE") == "1",
                     "Start PostgreSQL and set HOMEENERGY_TEST_DATABASE=1")
class PriceRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.schema = "home_energy_test_" + uuid4().hex
        namespace = patch("home_energy.database.schema.SCHEMA_NAME", self.schema)
        namespace.start()
        self.addCleanup(namespace.stop)
        self.addCleanup(self.drop_schema)
        self.repository = DayAheadPriceRepository()
        self.repository.initialize_schema()

    def drop_schema(self):
        if not self.schema.startswith("home_energy_test_"):
            raise ValueError("Unexpected integration-test schema")
        with connect() as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    def test_decimal_round_trip_idempotence_negative_prices_and_dst(self):
        for day, value in [(date(2025, 9, 30), "-12.34000"), (date(2026, 3, 29), "0"),
                           (date(2026, 10, 25), "123.4567890123456789")]:
            source = price_day(day, value)
            self.assertTrue(self.repository.save_day(source))
            self.assertFalse(self.repository.save_day(price_day(day, "999")))
            self.assertEqual(self.repository.load_day(day), source)
        self.repository.initialize_schema()

    def test_failure_on_child_write_rolls_back_everything(self):
        with self.repository._connection() as connection:
            connection.execute("ALTER TABLE price_intervals ADD CONSTRAINT injected_failure CHECK (price_eur_per_mwh < 1)")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.repository.save_day(price_day())
        self.assertFalse(self.repository.has_day(date(2026, 10, 10)))
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM price_intervals").fetchone()[0], 0)
            connection.execute("ALTER TABLE price_intervals DROP CONSTRAINT injected_failure")
        self.assertTrue(self.repository.save_day(price_day()))

    def test_partial_model_is_rejected_before_creating_parent(self):
        source = price_day()
        source.intervals.pop()
        with self.assertRaises(ValueError):
            self.repository.save_day(source)
        self.assertFalse(self.repository.has_day(source.day))

    def test_real_cli_and_example_export_from_sql_without_api(self):
        self.repository.save_day(price_day())
        with tempfile.TemporaryDirectory() as directory, \
                patch("home_energy.prices.repository.DayAheadPriceClient") as api, redirect_stdout(io.StringIO()):
            cli_path = Path(directory) / "cli.png"
            self.assertEqual(main(["chart", "prices", "2026-10-10", "--output", str(cli_path)]), 0)
            example_path = Path(directory) / "example.png"
            with patch.object(sys, "argv", ["day_ahead_prices.py", "2026-10-10", "--db-only", "--output", str(example_path)]):
                runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/day_ahead_prices.py"), run_name="__main__")
            for path in (cli_path, example_path):
                self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            api.from_config.assert_not_called()

    def test_concurrent_transactions_commit_one_complete_winner(self):
        barrier = Barrier(2)

        def save(value):
            source = price_day(value=value)
            barrier.wait(timeout=10)
            return value, self.repository.save_day(source)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["1", "9"]))
        self.assertEqual(sorted(inserted for _, inserted in results), [False, True])
        winner = Decimal(next(value for value, inserted in results if inserted))
        loaded = self.repository.load_day(date(2026, 10, 10))
        self.assertEqual(len(loaded.intervals), 96)
        self.assertTrue(all(interval.price_eur_per_mwh == winner for interval in loaded.intervals))

    def test_database_queries_order_dates_omit_gaps_and_need_no_api_configuration(self):
        for day in (date(2026, 10, 10), date(2026, 10, 8)):
            self.repository.save_day(price_day(day))
        with patch("home_energy.prices.repository.DayAheadPriceClient") as client:
            self.assertIsNone(self.repository.load_day(date(2026, 10, 9)))
            days = self.repository.load_range(date(2026, 10, 8), date(2026, 10, 10))
            self.assertEqual([day.day for day in days], [date(2026, 10, 8), date(2026, 10, 10)])
            self.assertEqual(self.repository.list_available_dates(), [day.day for day in days])
            self.assertEqual(self.repository.list_available_dates(start=date(2026, 10, 9)), [date(2026, 10, 10)])
            self.assertEqual(self.repository.list_available_dates(end=date(2026, 10, 9)), [date(2026, 10, 8)])
            client.from_config.assert_not_called()

    def test_range_failure_resume_and_complete_skip_without_client(self):
        with patch("home_energy.prices.repository.DayAheadPriceClient") as api:
            client = api.from_config.return_value.__enter__.return_value

            def fetch(day):
                if day == date(2026, 10, 9):
                    raise PricesUnavailableError("Not published")
                return price_day(day)

            client.get_day.side_effect = fetch
            with self.assertRaises(FillError) as error:
                self.repository.fill(date(2026, 10, 8), date(2026, 10, 10))
            self.assertEqual(error.exception.day, date(2026, 10, 9))
            self.assertEqual(self.repository.list_available_dates(), [date(2026, 10, 8)])
            client.get_day.reset_mock()
            client.get_day.side_effect = price_day
            results = self.repository.fill(date(2026, 10, 8), date(2026, 10, 10))
            self.assertEqual([result.inserted for result in results], [False, True, True])
            self.assertEqual(client.get_day.call_count, 2)
            api.from_config.reset_mock()
            self.repository.fill(date(2026, 10, 8), date(2026, 10, 10))
            api.from_config.assert_not_called()
