"""Shared region rules without a provider, database, dataframe or renderer."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from home_energy.usage.ev import find_regions


START = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


class RegionTests(unittest.TestCase):
    def test_exact_duration_and_strict_threshold(self):
        for count, power, expected in ((24, Decimal("2.4"), [(START, START + timedelta(hours=2))]),
                                       (23, Decimal("2.4"), []), (24, Decimal("2.169"), [])):
            with self.subTest(count=count, power=power):
                self.assertEqual(find_regions([(START + n * STEP, power) for n in range(count)]), expected)

    def test_both_an_absent_timestamp_and_a_null_break_the_run(self):
        for missing in (None, "absent"):
            with self.subTest(missing=missing):
                readings = [(START + n * STEP, Decimal("2.4")) for n in range(36)]
                if missing == "absent":
                    del readings[18]
                else:
                    readings[18] = (readings[18][0], None)
                self.assertEqual(find_regions(readings), [])

    def test_investigation_can_use_its_reference_threshold_and_cadence(self):
        readings = [(START + timedelta(minutes=10 * n), 2.15) for n in range(12)]
        self.assertEqual(find_regions(readings, threshold_kw=2.13, sample_interval=timedelta(minutes=10)),
                         [(START, START + timedelta(hours=2))])
        self.assertEqual(find_regions([]), [])

    def test_elapsed_duration_survives_clock_change(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Europe/Brussels")
        start = datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)
        readings = [((start + n * STEP).astimezone(zone), Decimal("2.4")) for n in range(24)]
        self.assertEqual(find_regions(readings), [(start, start + timedelta(hours=2))])
