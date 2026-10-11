"""Report composition, stored-day retries, PNG lifecycle, and warning labels."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Event, Lock
from unittest.mock import Mock, patch

from home_energy.filling import FillError
from home_energy.prices.analysis import PriceRange
from home_energy.prices.entsoe import (DayAheadPriceClient, InvalidPriceDataError,
                                      PriceConfigurationError)
from home_energy.prices.report import DayAheadReportService, ReportRequestError, warning_summary
from home_energy.prices.repository import DayAheadPriceRepository
from price_helpers import price_day, price_series


DAY = date(2000, 1, 1)


class PriceReportTests(unittest.TestCase):
    def test_report_returns_a_real_png_and_actual_price_warning(self):
        day = price_day(DAY, "100")
        day.intervals[4] = replace(day.intervals[4], price_eur_per_mwh=Decimal("400"))
        repository = Mock()
        repository.load_day.return_value = day
        result = DayAheadReportService(repository)(DAY)
        repository.fill.assert_called_once_with(DAY)
        repository.load_day.assert_called_once_with(DAY)
        self.assertTrue(result.png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(len(result.high_price_ranges), 1)
        self.assertEqual(result.high_price_ranges[0].start_at, day.intervals[4].start_at)
        self.assertIn("04:00", result.warning)

    def test_a_retry_after_rendering_failure_uses_persisted_data_without_refetching(self):
        repository = DayAheadPriceRepository()
        stored = {}
        def save(day):
            stored[day.day] = day
            return True
        with patch.object(repository, "has_day", side_effect=lambda day: day in stored), \
                patch.object(repository, "save_day", side_effect=save), \
                patch.object(repository, "load_day", side_effect=stored.get), \
                patch("home_energy.prices.repository.DayAheadPriceClient") as provider, \
                patch("home_energy.prices.report._render_png", side_effect=[OSError("render failed"), b"png"]):
            client = provider.from_config.return_value.__enter__.return_value
            client.get_day.return_value = price_day(DAY)
            service = DayAheadReportService(repository)
            with self.assertRaises(OSError):
                service(DAY)
            result = service(DAY)
            self.assertEqual(result.png, b"png")
            self.assertIsNone(result.warning)
            provider.from_config.assert_called_once()
            client.get_day.assert_called_once_with(DAY)

    def test_invalid_input_is_distinct_from_invalid_provider_or_render_data(self):
        repository = Mock()
        service = DayAheadReportService(repository)
        for day, threshold in [(date(9999, 1, 1), 300), (DAY, "NaN"), (DAY, True)]:
            with self.subTest(day=day, threshold=threshold), self.assertRaises(ReportRequestError):
                service(day, threshold)
        repository.fill.assert_not_called()
        repository.fill.side_effect = FillError(DAY, InvalidPriceDataError("invalid upstream"))
        with self.assertRaises(FillError):
            service(DAY)
        repository.fill.side_effect = None
        repository.load_day.return_value = None
        with self.assertRaises(RuntimeError):
            service(DAY)

    def test_render_releases_the_figure_on_success_and_save_failure(self):
        repository = Mock()
        repository.load_day.return_value = price_day(DAY)
        with patch("home_energy.prices.report.create_price_chart") as create:
            create.return_value.savefig.side_effect = OSError("write failed")
            with self.assertRaises(OSError):
                DayAheadReportService(repository)(DAY)
            create.return_value.clear.assert_called_once_with()

    def test_concurrent_report_requests_do_not_overlap_chart_creation(self):
        repository = Mock()
        repository.load_day.return_value = price_day(DAY)
        both_loaded = Event()
        counter_lock = Lock()
        calls = 0
        def load(day):
            nonlocal calls
            with counter_lock:
                calls += 1
                if calls == 2:
                    both_loaded.set()
            return price_day(DAY)
        repository.load_day.side_effect = load
        acquired = Lock()
        def create(day, threshold):
            self.assertTrue(acquired.acquire(blocking=False), "Rendering overlapped")
            figure = Mock()
            def save(*args, **kwargs):
                self.assertTrue(both_loaded.wait(5), "Second request did not reach rendering")
            figure.savefig.side_effect = save
            figure.clear.side_effect = acquired.release
            return figure
        with patch("home_energy.prices.report.create_price_chart", side_effect=create), \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(DayAheadReportService(repository), DAY) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)

    def test_dst_warning_labels_distinguish_repeated_hours_and_next_day_midnight(self):
        day = date(2026, 10, 25)
        ranges = (PriceRange(datetime(2026, 10, 25, 0, tzinfo=timezone.utc),
                             datetime(2026, 10, 25, 1, tzinfo=timezone.utc)),
                  PriceRange(datetime(2026, 10, 25, 22, tzinfo=timezone.utc),
                             datetime(2026, 10, 25, 23, tzinfo=timezone.utc)))
        text = warning_summary(ranges, day, "Europe/Brussels")
        self.assertIn("02:00 (+0200) tot 02:00 (+0100)", text)
        self.assertIn("2026-10-26 00:00", text)
        self.assertNotIn("**", text)


class ProviderFailureTests(unittest.TestCase):
    def test_bad_provider_data_and_configuration_have_distinct_error_types(self):
        with patch("home_energy.prices.entsoe.EntsoePandasClient") as upstream:
            upstream.return_value.query_day_ahead_prices.return_value = price_series(DAY).iloc[1:]
            with DayAheadPriceClient("test-key") as client, self.assertRaises(InvalidPriceDataError):
                client.get_day(DAY)
        with self.assertRaises(PriceConfigurationError):
            DayAheadPriceClient("")
        with patch("home_energy.prices.entsoe.dotenv_values", side_effect=UnicodeError("secret")), \
                self.assertRaises(PriceConfigurationError) as error:
            DayAheadPriceClient.from_config()
        self.assertNotIn("secret", str(error.exception))
