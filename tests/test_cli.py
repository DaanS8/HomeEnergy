"""Command wiring, initialization without API credentials, and safe failures."""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from unittest.mock import patch

import psycopg

from home_energy.cli import main
from home_energy.filling import FillError, FillResult


class CliTests(unittest.TestCase):
    def test_initialization_never_constructs_api_client(self):
        with patch("home_energy.cli.initialize_schema") as initialize, \
                patch("home_energy.usage.repository.FusionSolarClient") as client, \
                patch("home_energy.prices.repository.DayAheadPriceClient") as prices, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init-db"]), 0)
            initialize.assert_called_once()
            client.from_config.assert_not_called()
            prices.from_config.assert_not_called()

    def test_fill_reports_per_day_progress(self):
        output = io.StringIO()
        def fill(start, end, on_result):
            on_result(FillResult(start, True))
            on_result(FillResult(end, False))
        with patch("home_energy.cli.EnergyBalanceRepository") as repository, redirect_stdout(output):
            repository.return_value.fill.side_effect = fill
            self.assertEqual(main(["fill", "usage", "2026-10-09", "--end-date", "2026-10-10"]), 0)
        self.assertIn("2026-10-09: stored", output.getvalue())
        self.assertIn("2026-10-10: already stored", output.getvalue())

    def test_database_failure_is_redacted_and_nonzero(self):
        error = io.StringIO()
        with patch("home_energy.cli.initialize_schema") as initialize, redirect_stderr(error):
            initialize.side_effect = psycopg.OperationalError("password=private-secret")
            self.assertEqual(main(["init-db"]), 1)
        self.assertIn("PG*", error.getvalue())
        self.assertNotIn("private-secret", error.getvalue())

    def test_fill_error_reports_date_and_redacts_database_error(self):
        error = io.StringIO()
        with patch("home_energy.cli.EnergyBalanceRepository") as repository, redirect_stderr(error):
            repository.return_value.fill.side_effect = FillError(date(2026, 10, 10), psycopg.Error("private-secret"))
            self.assertEqual(main(["fill", "usage", "2026-10-10"]), 1)
        self.assertIn("2026-10-10", error.getvalue())
        self.assertIn("rerun to resume", error.getvalue())
        self.assertNotIn("private-secret", error.getvalue())

    def test_prices_fill_routes_to_price_repository(self):
        with patch("home_energy.cli.DayAheadPriceRepository") as repository, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["fill", "prices", "2026-10-10"]), 0)
        self.assertEqual(repository.return_value.fill.call_args.args, (date(2026, 10, 10), None))

    def test_chart_reads_only_database_and_releases_figure_on_save_error(self):
        error = io.StringIO()
        with patch("home_energy.cli.DayAheadPriceRepository") as repository, \
                patch("home_energy.prices.chart.create_price_chart") as create, \
                patch("home_energy.charts.save_png", side_effect=OSError("Cannot write output")), \
                redirect_stderr(error):
            self.assertEqual(main(["chart", "prices", "2026-10-10", "--output", "outputs/prices.png", "--threshold", "250"]), 1)
            create.return_value.clear.assert_called_once()
            repository.return_value.fill.assert_not_called()
            self.assertEqual(create.call_args.kwargs["threshold"], 250)

    def test_missing_prices_fail_without_creating_chart(self):
        with patch("home_energy.cli.DayAheadPriceRepository") as repository, \
                patch("home_energy.prices.chart.create_price_chart") as create, redirect_stderr(io.StringIO()):
            repository.return_value.load_day.return_value = None
            self.assertEqual(main(["chart", "prices", "2026-10-10", "--output", "outputs/prices.png"]), 1)
            create.assert_not_called()

    def test_invalid_threshold_fails_in_argument_parsing_without_traceback_or_database(self):
        for value in ("invalid", "NaN", "Infinity"):
            with patch("home_energy.cli.DayAheadPriceRepository") as repository, redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                main(["chart", "prices", "2026-10-10", "--output", "prices.png", "--threshold", value])
            self.assertEqual(error.exception.code, 2)
            repository.assert_not_called()
