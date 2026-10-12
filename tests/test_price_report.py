"""Price reports protect exact warnings, committed data, and retry outcomes."""

import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from home_energy.filling import FillError
from home_energy.prices.analysis import PriceRange
from home_energy.prices.entsoe import InvalidPriceDataError
from home_energy.prices.ingestion import DayAheadPriceIngestionService
from home_energy.prices.report import DayAheadReportService, ReportRequestError, warning_summary
from price_helpers import price_day
from service_helpers import MemoryRepository, RecordingProvider, assert_png


DAY = date(2026, 10, 10)
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


class PriceReportTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.provider = RecordingProvider(price_day)
        ingestion = DayAheadPriceIngestionService(
            self.repository, client_factory=lambda: self.provider, now=lambda: NOW,
        )
        self.service = DayAheadReportService(ingestion_service=ingestion)

    def test_actual_spike_produces_warning_and_cached_replay_needs_no_provider(self):
        day = price_day(DAY, "100")
        day.intervals[16] = replace(day.intervals[16], price_eur_per_mwh=Decimal("400"))
        self.provider.fetch = lambda selected: day
        first, second = self.service(DAY), self.service(DAY)
        self.assertEqual(first.high_price_ranges, second.high_price_ranges)
        self.assertEqual(first.warning, second.warning)
        self.assertEqual(first.high_price_ranges,
                         (PriceRange(day.intervals[16].start_at, day.intervals[16].end_at),))
        self.assertIn("04:00", first.warning)
        self.assertIn("04:15", first.warning)
        self.assertEqual(self.provider.requests, [DAY])
        self.assertEqual(self.repository.load_day(DAY), day)
        assert_png(self, first.png)
        assert_png(self, second.png)

    def test_negative_decimal_threshold_is_strict_and_unrounded(self):
        self.repository.save_day(price_day(DAY, "-10.2500"))
        equal = self.service(DAY, Decimal("-10.2500"))
        self.assertEqual(equal.high_price_ranges, ())
        self.assertIsNone(equal.warning)
        lower = self.service(DAY, Decimal("-10.2501"))
        self.assertEqual(lower.threshold, Decimal("-10.2501"))
        self.assertEqual(len(lower.high_price_ranges), 1)
        self.assertEqual(lower.high_price_ranges[0].start_at, self.repository.load_day(DAY).intervals[0].start_at)
        self.assertEqual(lower.high_price_ranges[0].end_at, self.repository.load_day(DAY).intervals[-1].end_at)
        self.assertEqual(self.provider.opened, 0)

    def test_render_failure_retry_uses_persisted_data_without_refetching(self):
        with patch("home_energy.prices.report.render_png", side_effect=OSError("render failed")):
            with self.assertRaises(OSError):
                self.service(DAY)
        stored = self.repository.load_day(DAY)
        result = self.service(DAY)
        self.assertEqual(self.repository.load_day(DAY), stored)
        self.assertEqual(self.provider.requests, [DAY])
        self.assertIsNone(result.warning)
        assert_png(self, result.png)

    def test_invalid_request_and_provider_data_have_distinct_errors(self):
        for day, threshold in ((date(9999, 1, 1), 300), (DAY, "NaN"), (DAY, True)):
            with self.subTest(day=day, threshold=threshold), self.assertRaises(ReportRequestError):
                self.service(day, threshold)
        self.assertEqual(self.provider.opened, 0)
        self.assertEqual(self.repository.days, {})

        def invalid_data(day):
            raise InvalidPriceDataError("invalid upstream")

        self.provider.fetch = invalid_data
        with self.assertRaises(FillError) as error:
            self.service(DAY)
        self.assertIsInstance(error.exception.cause, InvalidPriceDataError)
        self.assertIsNone(self.repository.load_day(DAY))

    def test_missing_committed_day_cannot_produce_a_report(self):
        self.repository.save_day(price_day(DAY))
        with patch.object(self.repository, "load_day", return_value=None):
            with self.assertRaises(RuntimeError):
                self.service(DAY)

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

    def test_concurrent_insert_analyses_committed_prices_instead_of_fetched_prices(self):
        committed, fresh = price_day(DAY, "100"), price_day(DAY, "400")
        self.provider.fetch = lambda day: fresh

        def competing_insert(day):
            self.repository.days[day.day] = committed
            return False

        with patch.object(self.repository, "save_day", side_effect=competing_insert):
            result = self.service(DAY)
        self.assertEqual(result.high_price_ranges, ())
        self.assertIsNone(result.warning)
        self.assertEqual(result.fetched_at, committed.fetched_at)
