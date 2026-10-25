"""Daily consumption and EV attribution, including midnight and DST boundaries."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from home_energy.usage.daily import DailyUsageRequestError, analyse_daily_usage
from home_energy.usage.ev import EV_POWER_KW
from home_energy.usage.energy_balance import EnergyBalanceValidationError
from usage_helpers import DAY, NOW, ZONE, complete_source_days as days


class DailyDetectionTests(unittest.TestCase):
    def test_two_hours_and_strict_power_threshold_with_no_baseload_subtraction(self):
        data = days(loads=[("2026-09-02 08:00", "2026-09-02 09:55", 2.4),
                           ("2026-09-02 10:00", "2026-09-02 12:00", 2.159),
                           ("2026-09-02 14:00", "2026-09-02 16:00", 2.4)])
        report = analyse_daily_usage(data, DAY, now=NOW)
        self.assertEqual(len(report.consumption), 288)
        self.assertEqual(len(report.ev_blocks), 1)
        self.assertEqual(report.ev_blocks[0].power_kw, Decimal("2.159"))
        self.assertEqual(report.ev_total_kwh, Decimal("4.318"))
        self.assertEqual(report.total_kwh, Decimal("42.750"))
        self.assertTrue(all(s.ev_power_kw <= s.power_kw for s in report.consumption))

    def test_midnight_context_qualifies_short_daily_fragments_and_preserves_energy(self):
        data = days(count=4, loads=[("2026-09-01 22:30", "2026-09-02 00:30", 2.4),
                                    ("2026-09-02 23:30", "2026-09-03 01:30", 2.4)])
        report = analyse_daily_usage(data, DAY, now=NOW)
        self.assertEqual([b.energy_kwh for b in report.ev_blocks], [Decimal("1.0795")] * 2)
        self.assertEqual(report.ev_total_kwh, EV_POWER_KW)
        following = analyse_daily_usage(data[1:], DAY + timedelta(days=1), now=NOW + timedelta(days=1))
        self.assertEqual(following.ev_total_kwh + report.ev_blocks[1].energy_kwh, Decimal("4.318"))

    def test_interruptions_split_regions_but_missing_readings_fail(self):
        for mode in ("dip", "null", "absent"):
            with self.subTest(mode=mode):
                data = days(loads=[("2026-09-02 10:00", "2026-09-02 13:00", 2.4)])
                sample = next(s for s in data[1].samples if s.measurement == "usePower" and s.sample_index == 138)
                if mode == "absent":
                    data[1].samples.remove(sample)
                else:
                    sample.value = Decimal("0.2") if mode == "dip" else None
                    sample.original_value = "0.2" if mode == "dip" else None
                if mode == "dip":
                    self.assertEqual(analyse_daily_usage(data, DAY, now=NOW).ev_blocks, ())
                else:
                    with self.assertRaises(EnergyBalanceValidationError):
                        analyse_daily_usage(data, DAY, now=NOW)

    def test_today_and_yesterday_are_rejected_even_after_two_hours(self):
        for now in (datetime(2026, 9, 2, 12, tzinfo=ZONE), datetime(2026, 9, 3, 12, tzinfo=ZONE)):
            with self.subTest(now=now), self.assertRaises(DailyUsageRequestError):
                analyse_daily_usage(days(), DAY, now=now)

    def test_missing_total_is_rejected_and_never_replaced_by_power_integral(self):
        data = days()
        data[1].metrics = []
        with self.assertRaises(EnergyBalanceValidationError):
            analyse_daily_usage(data, DAY, now=NOW)

    def test_invalid_required_boundary_readings_are_rejected(self):
        for offset, index in ((0, 276), (2, 12)):
            for mode in ("null", "absent", "unresolved"):
                with self.subTest(offset=offset, mode=mode):
                    data = days()
                    sample = next(s for s in data[offset].samples
                                  if s.measurement == "usePower" and s.sample_index == index)
                    if mode == "null":
                        sample.value = sample.original_value = None
                    elif mode == "absent":
                        data[offset].samples.remove(sample)
                    else:
                        sample.observed_at = None
                    with self.assertRaises(EnergyBalanceValidationError):
                        analyse_daily_usage(data, DAY, now=NOW)

    def test_dst_day_uses_elapsed_time_and_both_brussels_offsets(self):
        selected = DAY.replace(month=10, day=25)
        data = days(first=selected - timedelta(days=1))
        start = datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)
        finish = start + timedelta(hours=2)
        for sample in data[1].samples:
            if sample.measurement == "usePower" and start <= sample.observed_at < finish:
                sample.value, sample.original_value = Decimal("2.4"), "2.4"
        report = analyse_daily_usage(data, selected, now=datetime(2026, 10, 27, tzinfo=ZONE))
        self.assertEqual(len(report.consumption), 300)
        self.assertEqual(report.ev_total_kwh, Decimal("4.318"))
        self.assertEqual(report.ev_blocks[0].start_at.astimezone(ZONE).hour, 1)
        self.assertEqual(report.ev_blocks[0].end_at.astimezone(ZONE).hour, 2)

    def test_unresolved_duplicate_and_wrong_unit_data_are_rejected(self):
        for mode in ("unresolved", "duplicate", "wrong_unit"):
            with self.subTest(mode=mode):
                data = days()
                sample = next(s for s in data[1].samples if s.measurement == "usePower")
                if mode == "unresolved":
                    sample.observed_at = None
                elif mode == "duplicate":
                    data[1].samples.append(sample)
                else:
                    sample.unit = "kWh"
                with self.assertRaises(EnergyBalanceValidationError):
                    analyse_daily_usage(data, DAY, now=NOW)

    def test_validation_ignores_unused_adjacent_day_data(self):
        data = days(loads=[("2026-09-02 14:00", "2026-09-02 16:00", 2.4)])
        noon = next(s for s in data[0].samples if s.measurement == "usePower" and s.sample_index == 144)
        noon.observed_at = None
        tomorrow_noon = next(s for s in data[2].samples if s.measurement == "usePower" and s.sample_index == 144)
        tomorrow_noon.unit = "kWh"
        report = analyse_daily_usage(data, DAY, now=NOW)
        self.assertEqual(report.ev_total_kwh, Decimal("4.318"))

    def test_ambiguous_unused_dst_hour_does_not_reject_the_following_day(self):
        selected = DAY.replace(month=10, day=26)
        data = days(first=selected - timedelta(days=1))
        sample = next(s for s in data[0].samples if s.measurement == "usePower" and s.sample_index == 30)
        sample.observed_at = None
        sample.axis_label = "2026-10-25 02:30"
        report = analyse_daily_usage(data, selected, now=datetime(2026, 10, 28, tzinfo=ZONE))
        self.assertEqual(len(report.consumption), 288)
