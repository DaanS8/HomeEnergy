"""Past-day validation, immutable skips, failure reporting, and range resume."""

import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from home_energy.usage.fusionsolar import FusionSolarError
from home_energy.usage.constants import PLANT_ID, PLANT_TIMEZONE
from home_energy.usage.filling import DataFetcher
from home_energy.filling import FillError
from helpers import energy_payload


class MemoryRepository:
    def __init__(self):
        self.days = {}

    def has_day(self, day):
        return (PLANT_ID, day) in self.days

    def save_day(self, day):
        if self.has_day(day.day):
            return False
        self.days[(day.plant_id, day.day)] = day
        return True


class DataFetcherTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.plant_id = PLANT_ID
        self.client.timezone = PLANT_TIMEZONE
        self.client.config = {}
        self.client.get_energy_balance.side_effect = lambda day: energy_payload(day)
        self.repository = MemoryRepository()
        self.fetcher = DataFetcher(self.client, self.repository,
                                  now=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))

    def test_inclusive_range_and_skipped_days(self):
        reports = []
        results = self.fetcher.fill(date(2026, 10, 8), date(2026, 10, 10), reports.append)
        self.assertEqual(len(results), 3)
        self.assertEqual(results, reports)
        self.assertTrue(all(r.inserted for r in results))
        self.client.get_energy_balance.assert_any_call(date(2026, 10, 10))
        self.client.get_energy_balance.reset_mock()
        self.assertTrue(all(not r.inserted for r in self.fetcher.fill(date(2026, 10, 8), date(2026, 10, 10))))
        self.client.get_energy_balance.assert_not_called()

    def test_failure_commits_prior_days_and_resume_skips_them(self):
        def get(day):
            if day == date(2026, 10, 9):
                raise FusionSolarError("failed")
            return energy_payload(day)
        self.client.get_energy_balance.side_effect = get
        with self.assertRaises(FillError) as error:
            self.fetcher.fill(date(2026, 10, 8), date(2026, 10, 10))
        self.assertEqual(error.exception.day, date(2026, 10, 9))
        self.assertEqual(list(self.repository.days), [(PLANT_ID, date(2026, 10, 8))])
        self.client.get_energy_balance.reset_mock()
        self.client.get_energy_balance.side_effect = lambda day: energy_payload(day)
        results = self.fetcher.fill(date(2026, 10, 8), date(2026, 10, 10))
        self.assertEqual([r.inserted for r in results], [False, True, True])
        self.assertEqual([c.args[0] for c in self.client.get_energy_balance.call_args_list],
                         [date(2026, 10, 9), date(2026, 10, 10)])

    def test_invalid_response_leaves_no_completed_day(self):
        self.client.get_energy_balance.side_effect = None
        self.client.get_energy_balance.return_value = {"data": {}}
        with self.assertRaises(FillError):
            self.fetcher.fill(date(2026, 10, 10))
        self.assertFalse(self.repository.days)

    def test_current_future_reversed_and_non_date_ranges_fail_before_network(self):
        for start, end in [(date(2026, 10, 11), None), (date(2026, 10, 10), date(2026, 10, 12)),
                           (date(2026, 10, 10), date(2026, 10, 9)), ("2026-10-10", None)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.fetcher.fill(start, end)
        self.client.get_energy_balance.assert_not_called()
        self.assertFalse(self.repository.days)

    def test_today_uses_plant_timezone_at_midnight(self):
        self.fetcher.now = lambda: datetime(2026, 10, 10, 22, 30, tzinfo=timezone.utc)
        self.assertTrue(self.fetcher.fill(date(2026, 10, 10))[0].inserted)
        with self.assertRaises(ValueError):
            self.fetcher.fill(date(2026, 10, 11))
