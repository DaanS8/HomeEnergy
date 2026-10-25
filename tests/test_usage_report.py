"""Completed EV reports use authoritative snapshots and survive failed retries."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from home_energy.filling import FillError
from home_energy.usage.daily import DailyUsageRequestError
from home_energy.usage.energy_balance import EnergyBalanceValidationError
from home_energy.usage.ingestion import UsageConfigurationError, UsageIngestionService
from home_energy.usage.models import DailyMetric
from home_energy.usage.report import EVUsageReportService
from service_helpers import MemoryRepository, RecordingProvider, assert_png
from usage_helpers import DAY, NOW, complete_energy_payload, complete_source_days


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.provider = RecordingProvider(complete_energy_payload)
        self.ingestion = UsageIngestionService(
            self.repository, client_factory=lambda: self.provider, now=lambda: NOW,
        )
        self.service = EVUsageReportService(ingestion_service=self.ingestion)

    def test_fresh_and_cached_reports_preserve_midnight_energy_and_skip_provider_on_replay(self):
        loads = [("2026-09-02 23:30", "2026-09-03 01:30", 2.4)]
        self.provider.fetch = lambda day: complete_energy_payload(day, loads=loads)
        first = self.service(DAY)
        second = self.service(DAY)
        self.assertEqual(first.usage, second.usage)
        self.assertEqual(first.usage.total_kwh, Decimal("42.750"))
        self.assertEqual(first.usage.ev_total_kwh, Decimal("1.0795"))
        self.assertEqual(len(first.usage.ev_blocks), 1)
        self.assertEqual(self.provider.requests, [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)])
        self.assertEqual(sorted(self.repository.days), self.provider.requests)
        self.assertEqual(self.provider.opened, self.provider.closed)
        assert_png(self, first.png)
        assert_png(self, second.png)

    def test_default_is_two_days_ago_using_brussels_date_without_provider_credentials(self):
        self.repository.days = {day.day: day for day in complete_source_days()}
        service = EVUsageReportService(
            ingestion_service=self.ingestion,
            now=lambda: datetime(2026, 9, 3, 22, 30, tzinfo=timezone.utc),
        )
        report = service()
        self.assertEqual(report.usage.day, DAY)
        self.assertEqual(report.usage.ev_blocks, ())
        assert_png(self, report.png)
        self.assertEqual(self.provider.opened, 0)

    def test_invalid_dates_and_naive_clock_fail_without_provider_or_storage_changes(self):
        for offset in (-1, 0, 1):
            with self.subTest(offset=offset), self.assertRaises(DailyUsageRequestError):
                self.service(NOW.date() + timedelta(days=offset))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            EVUsageReportService(self.repository, now=lambda: datetime(2026, 9, 4))(DAY)
        self.assertEqual(self.repository.days, {})
        self.assertEqual(self.provider.opened, 0)

    def test_configuration_errors_are_typed_and_redacted(self):
        def bad_configuration():
            raise ValueError("private-secret")

        self.ingestion.client_factory = bad_configuration
        with self.assertRaises(FillError) as error:
            self.service(DAY)
        self.assertIsInstance(error.exception.cause, UsageConfigurationError)
        self.assertNotIn("private-secret", str(error.exception.cause))
        self.assertEqual(self.repository.days, {})

    def test_incomplete_requested_or_neighbor_day_can_be_corrected_and_retried(self):
        for missing in (DAY, DAY - timedelta(days=1), DAY + timedelta(days=1)):
            with self.subTest(missing=missing):
                self.provider.requests.clear()
                self.repository.days = {day.day: day for day in complete_source_days() if day.day != missing}

                def incomplete(day):
                    payload = complete_energy_payload(day)
                    del payload["data"]["totalUsePower"]
                    return payload

                self.provider.fetch = incomplete
                with self.assertRaises(FillError) as error:
                    self.service(DAY)
                self.assertEqual(error.exception.day, missing)
                self.assertIsInstance(error.exception.cause, EnergyBalanceValidationError)
                self.assertIsNone(self.repository.load_day(missing))
                self.provider.fetch = complete_energy_payload
                report = self.service(DAY)
                self.assertEqual(report.usage.total_kwh, Decimal("42.750"))
                self.assertEqual(self.provider.requests, [missing, missing])
                self.assertEqual(len(self.repository.days), 3)
                assert_png(self, report.png)

    def test_incomplete_stored_context_is_rejected_without_overwriting_or_refetching(self):
        self.repository.days = {day.day: day for day in complete_source_days()}
        context = self.repository.days[DAY + timedelta(days=1)]
        context.samples = [s for s in context.samples if s.sample_index != 12]
        before = self.repository.load_range(DAY - timedelta(days=1), DAY + timedelta(days=1))
        with self.assertRaises(EnergyBalanceValidationError):
            self.service(DAY)
        self.assertEqual(self.repository.load_range(DAY - timedelta(days=1), DAY + timedelta(days=1)), before)
        self.assertEqual(self.provider.opened, 0)

    def test_concurrent_insert_reports_committed_total_and_ev_blocks(self):
        committed = complete_source_days(loads=[("2026-09-02 14:00", "2026-09-02 16:00", 2.4)])[1]
        committed.metrics[0] = DailyMetric("totalUsePower", Decimal("99"), "99", "kWh")
        self.repository.days = {day.day: day for day in complete_source_days() if day.day != DAY}

        def competing_insert(day):
            self.repository.days[day.day] = committed
            return False

        with patch.object(self.repository, "save_day", side_effect=competing_insert):
            report = self.service(DAY)
        self.assertEqual(report.usage.total_kwh, Decimal("99"))
        self.assertEqual(report.usage.ev_total_kwh, Decimal("4.318"))
        self.assertEqual(len(report.usage.ev_blocks), 1)

    def test_missing_committed_day_cannot_produce_a_completed_report(self):
        self.repository.days = {day.day: day for day in complete_source_days()}
        with patch.object(self.repository, "load_range", return_value=[]):
            with self.assertRaises(EnergyBalanceValidationError):
                self.service(DAY)

    def test_render_failure_retry_reuses_all_committed_days(self):
        with patch("home_energy.usage.report.render_png", side_effect=OSError("render failed")):
            with self.assertRaises(OSError):
                self.service(DAY)
        before = self.repository.load_range(DAY - timedelta(days=1), DAY + timedelta(days=1))
        report = self.service(DAY)
        self.assertEqual(self.repository.load_range(DAY - timedelta(days=1), DAY + timedelta(days=1)), before)
        self.assertEqual(self.provider.requests, [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)])
        self.assertEqual(report.usage.total_kwh, Decimal("42.750"))
        assert_png(self, report.png)
