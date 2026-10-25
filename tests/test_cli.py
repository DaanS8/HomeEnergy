"""CLI outcomes: stored dates, progress, readable exports, and actionable errors."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

import psycopg

from home_energy.cli import main
from home_energy.prices.ingestion import DayAheadPriceIngestionService
from home_energy.usage.ingestion import UsageIngestionService
from price_helpers import price_day
from service_helpers import MemoryRepository, RecordingProvider, assert_png
from usage_helpers import complete_energy_payload


class CliTests(unittest.TestCase):
    def test_initialization_never_constructs_api_client(self):
        with patch("home_energy.cli.initialize_schema") as initialize, \
                patch("home_energy.usage.ingestion.FusionSolarClient") as client, \
                patch("home_energy.prices.ingestion.DayAheadPriceClient") as prices, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init-db"]), 0)
            initialize.assert_called_once()
            client.from_config.assert_not_called()
            prices.from_config.assert_not_called()

    def test_fill_in_both_domains_stores_selected_range_and_reports_skips(self):
        for domain, service_type, fetch in (("usage", UsageIngestionService, complete_energy_payload),
                                             ("prices", DayAheadPriceIngestionService, price_day)):
            with self.subTest(domain=domain):
                repository = MemoryRepository()
                provider = RecordingProvider(fetch)
                service = service_type(repository, client_factory=lambda: provider)
                service.fill(date(2000, 1, 2))
                provider.requests.clear()
                output = io.StringIO()
                with patch("home_energy.cli." + service_type.__name__, return_value=service), redirect_stdout(output):
                    self.assertEqual(main(["fill", domain, "2000-01-01", "--end-date", "2000-01-02"]), 0)
                self.assertIn("2000-01-01: stored", output.getvalue())
                self.assertIn("2000-01-02: already stored", output.getvalue())
                self.assertEqual(sorted(repository.days), [date(2000, 1, 1), date(2000, 1, 2)])
                self.assertEqual(provider.requests, [date(2000, 1, 1)])

    def test_database_failure_is_redacted_and_nonzero(self):
        error = io.StringIO()
        with patch("home_energy.cli.initialize_schema") as initialize, redirect_stderr(error):
            initialize.side_effect = psycopg.OperationalError("password=private-secret")
            self.assertEqual(main(["init-db"]), 1)
        self.assertIn("PG*", error.getvalue())
        self.assertNotIn("private-secret", error.getvalue())

    def test_fill_error_reports_date_and_redacts_database_error(self):
        class UnavailableRepository(MemoryRepository):
            def save_day(self, day):
                raise psycopg.Error("private-secret")

        repository = UnavailableRepository()
        provider = RecordingProvider(complete_energy_payload)
        service = UsageIngestionService(repository, client_factory=lambda: provider)
        error = io.StringIO()
        with patch("home_energy.cli.UsageIngestionService", return_value=service), redirect_stderr(error):
            self.assertEqual(main(["fill", "usage", "2000-01-01"]), 1)
        self.assertIn("2000-01-01", error.getvalue())
        self.assertIn("rerun to resume", error.getvalue())
        self.assertNotIn("private-secret", error.getvalue())
        self.assertEqual(repository.days, {})

    def test_chart_exports_stored_prices_to_the_requested_path_without_provider(self):
        repository = MemoryRepository([price_day(date(2000, 1, 1))])
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch("home_energy.cli.DayAheadPriceRepository", return_value=repository), \
                patch("home_energy.prices.ingestion.DayAheadPriceClient") as provider, redirect_stdout(output):
            path = Path(directory) / "nested" / "prices.png"
            self.assertEqual(main(["chart", "prices", "2000-01-01", "--output", str(path), "--threshold", "250"]), 0)
            assert_png(self, path.read_bytes())
            self.assertIn(str(path), output.getvalue())
            provider.from_config.assert_not_called()

    def test_chart_write_failure_is_actionable_and_returns_nonzero(self):
        error = io.StringIO()
        repository = MemoryRepository([price_day(date(2000, 1, 1))])
        with patch("home_energy.cli.DayAheadPriceRepository", return_value=repository), \
                patch("home_energy.charts.save_png", side_effect=OSError("Cannot write output")), \
                redirect_stderr(error):
            self.assertEqual(main(["chart", "prices", "2000-01-01", "--output", "outputs/prices.png"]), 1)
        self.assertIn("Cannot write output", error.getvalue())

    def test_missing_prices_explain_how_to_fill_and_leave_no_output(self):
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch("home_energy.cli.DayAheadPriceRepository", return_value=MemoryRepository()), redirect_stderr(error):
            path = Path(directory) / "prices.png"
            self.assertEqual(main(["chart", "prices", "2000-01-01", "--output", str(path)]), 1)
            self.assertFalse(path.exists())
        self.assertIn("home-energy fill prices 2000-01-01", error.getvalue())

    def test_invalid_threshold_fails_in_argument_parsing_without_traceback_or_database(self):
        for option, values in (("--threshold", ("invalid", "NaN", "Infinity")),
                               ("--hysteresis", ("invalid", "NaN", "Infinity", "-0.001"))):
            for value in values:
                with self.subTest(option=option, value=value), \
                        patch("home_energy.cli.DayAheadPriceRepository") as repository, \
                        redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(["chart", "prices", "2026-10-10", "--output", "prices.png", option, value])
                self.assertEqual(error.exception.code, 2)
                repository.assert_not_called()
