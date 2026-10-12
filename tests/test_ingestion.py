"""Shared ingestion promises, exercised through both real domain services."""

import unittest
from datetime import date, datetime, timedelta, timezone

from home_energy.filling import FillError
from home_energy.prices.entsoe import PricesUnavailableError
from home_energy.prices.ingestion import DayAheadPriceIngestionService
from home_energy.usage.energy_balance import EnergyBalanceValidationError
from home_energy.usage.fusionsolar import FusionSolarError
from home_energy.usage.ingestion import UsageIngestionService
from price_helpers import price_day
from service_helpers import MemoryRepository, RecordingProvider
from usage_helpers import complete_energy_payload


START, FAILED, END = date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
DOMAINS = (
    (UsageIngestionService, complete_energy_payload, FusionSolarError),
    (DayAheadPriceIngestionService, price_day, PricesUnavailableError),
)


def ingestion(service_type, fetch, *, repository=None, now=NOW):
    repository = repository if repository is not None else MemoryRepository()
    provider = RecordingProvider(fetch)
    service = service_type(repository, client_factory=lambda: provider, now=lambda: now)
    return service, repository, provider


class IngestionTests(unittest.TestCase):
    def test_inclusive_range_reports_progress_and_never_refetches_stored_days(self):
        for service_type, fetch, _ in DOMAINS:
            with self.subTest(domain=service_type.__name__):
                service, repository, provider = ingestion(service_type, fetch)
                service.fill(START)
                original = repository.load_day(START)
                provider.requests.clear()
                progress = []
                results = service.fill(START, END, progress.append)
                self.assertEqual([(r.day, r.inserted) for r in results],
                                 [(START, False), (FAILED, True), (END, True)])
                self.assertEqual(progress, results)
                self.assertEqual(provider.requests, [FAILED, END])
                self.assertEqual([d.day for d in repository.load_range(START, END)], [START, FAILED, END])
                self.assertEqual(repository.load_day(START), original)
                self.assertEqual(provider.opened, provider.closed)
                sessions = provider.opened
                provider.requests.clear()
                self.assertEqual([r.inserted for r in service.fill(START, END)], [False] * 3)
                self.assertEqual(provider.requests, [])
                self.assertEqual(provider.opened, sessions, "Stored data must work without provider login")

    def test_failure_preserves_progress_and_retry_fetches_only_uncommitted_days(self):
        for service_type, fetch, error_type in DOMAINS:
            with self.subTest(domain=service_type.__name__):
                def failing_fetch(day):
                    if day == FAILED:
                        raise error_type("Provider unavailable")
                    return fetch(day)

                service, repository, provider = ingestion(service_type, failing_fetch)
                progress = []
                with self.assertRaises(FillError) as error:
                    service.fill(START, END, progress.append)
                self.assertEqual(error.exception.day, FAILED)
                self.assertIsInstance(error.exception.cause, error_type)
                self.assertEqual([r.day for r in progress], [START])
                self.assertEqual(list(repository.days), [START])
                first = repository.load_day(START)
                self.assertEqual(provider.requests, [START, FAILED])
                self.assertEqual(provider.opened, provider.closed)
                provider.fetch = fetch
                provider.requests.clear()
                self.assertEqual([r.inserted for r in service.fill(START, END)], [False, True, True])
                self.assertEqual(provider.requests, [FAILED, END])
                self.assertEqual(repository.load_day(START), first)
                self.assertEqual(len(repository.days), 3)

    def test_storage_failure_stops_the_range_and_can_be_resumed(self):
        for service_type, fetch, _ in DOMAINS:
            with self.subTest(domain=service_type.__name__):
                class FailingRepository(MemoryRepository):
                    fail = True

                    def save_day(self, day):
                        if self.fail and day.day == FAILED:
                            raise OSError("Storage unavailable")
                        return super().save_day(day)

                repository = FailingRepository()
                service, _, provider = ingestion(service_type, fetch, repository=repository)
                with self.assertRaises(FillError) as error:
                    service.fill(START, END)
                self.assertEqual(error.exception.day, FAILED)
                self.assertEqual(list(repository.days), [START])
                self.assertEqual(provider.requests, [START, FAILED])
                self.assertEqual(provider.opened, provider.closed)
                repository.fail = False
                self.assertEqual([r.inserted for r in service.fill(START, END)], [False, True, True])
                self.assertEqual(len(repository.days), 3)

    def test_invalid_ranges_leave_storage_and_provider_untouched(self):
        for service_type, fetch, _ in DOMAINS:
            for start, end in ((date(2026, 10, 13), None), (END, START),
                               ("2026-10-10", None), (datetime(2026, 10, 10), None)):
                with self.subTest(domain=service_type.__name__, start=start, end=end):
                    service, repository, provider = ingestion(service_type, fetch)
                    with self.assertRaises(ValueError):
                        service.fill(start, end)
                    self.assertEqual(repository.days, {})
                    self.assertEqual(provider.opened, 0)

    def test_incomplete_provider_days_remain_retryable_in_both_domains(self):
        for service_type, fetch, _ in DOMAINS:
            with self.subTest(domain=service_type.__name__):
                incomplete = fetch(END)
                if service_type is UsageIngestionService:
                    del incomplete["data"]["totalUsePower"]
                else:
                    incomplete.intervals.pop()
                service, repository, provider = ingestion(service_type, lambda day: incomplete)
                with self.assertRaises(FillError):
                    service.fill(END)
                self.assertIsNone(repository.load_day(END))
                self.assertEqual(provider.opened, provider.closed)
                provider.fetch = fetch
                self.assertTrue(service.fill(END)[0].inserted)
                self.assertIsNotNone(repository.load_day(END))

    def test_usage_rejects_missing_null_and_malformed_readings_before_storage(self):
        for mode in ("missing", "null", "null_total", "malformed"):
            with self.subTest(mode=mode):
                payload = complete_energy_payload(END)
                if mode == "missing":
                    payload["data"]["xAxis"].pop()
                    payload["data"]["usePower"].pop()
                elif mode == "null":
                    payload["data"]["usePower"][10] = None
                elif mode == "null_total":
                    payload["data"]["totalUsePower"] = "--"
                else:
                    payload = {"data": {}}
                service, repository, provider = ingestion(UsageIngestionService, lambda day: payload)
                with self.assertRaises(FillError) as error:
                    service.fill(END)
                self.assertIsInstance(error.exception.cause, EnergyBalanceValidationError)
                self.assertEqual(repository.days, {})
                self.assertEqual(provider.opened, provider.closed)

    def test_each_domain_uses_brussels_date_at_utc_midnight_boundary(self):
        # It is already October 11 locally. Usage permits the 10th; prices the 12th.
        clock = datetime(2026, 10, 10, 22, 30, tzinfo=timezone.utc)
        for service_type, fetch, last in ((UsageIngestionService, complete_energy_payload, END),
                                          (DayAheadPriceIngestionService, price_day, date(2026, 10, 12))):
            with self.subTest(domain=service_type.__name__):
                service, repository, provider = ingestion(service_type, fetch, now=clock)
                self.assertTrue(service.fill(last)[0].inserted)
                with self.assertRaises(ValueError):
                    service.fill(last + timedelta(days=1))
                self.assertEqual(list(repository.days), [last])
                self.assertEqual(provider.requests, [last])

    def test_usage_stores_zero_consumption_on_complete_short_and_long_dst_days(self):
        for selected, count in ((END, 288), (date(2026, 3, 29), 276), (date(2026, 10, 25), 300)):
            with self.subTest(day=selected):
                payload = complete_energy_payload(selected)
                payload["data"]["usePower"] = ["0"] * count
                payload["data"]["totalUsePower"] = "0"
                service, repository, _ = ingestion(
                    UsageIngestionService, lambda day: payload,
                    now=datetime(2026, 10, 27, tzinfo=timezone.utc),
                )
                service.fill(selected)
                stored = repository.load_day(selected)
                self.assertEqual(stored.consumed_energy, 0)
                self.assertEqual(stored.sample_count, count)
