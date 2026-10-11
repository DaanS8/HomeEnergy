"""Known-power endings, transition contamination and uncertainty safeguards."""

from datetime import timedelta
import unittest

import numpy as np
import pandas as pd

from home_energy.investigations.ev.analysis import EVDetectionConfig, consumption_frame, detect_regions
from home_energy.investigations.ev.calibration import ending_table, estimate_loads
from usage_helpers import DAY, source_days


class EndingEstimationTests(unittest.TestCase):
    def data(self):
        frame = consumption_frame(source_days(loads=[("2026-09-02 10:00", "2026-09-02 12:00", 2.4)]))
        regions = detect_regions(frame, EVDetectionConfig(), 0.2)
        return frame, regions

    def table(self, frame, regions):
        return ending_table(frame, regions, cadence_seconds=300, window_minutes=10,
                            guard_samples=2, timezone="Europe/Brussels", maximum_spread_kw=0.2)

    def test_two_transition_readings_are_excluded_and_exact_windows_are_exported(self):
        frame, regions = self.data()
        stop = pd.Timestamp(regions[0].end)
        frame.loc[stop - pd.Timedelta(minutes=5), "total_kw"] = 2.14
        frame.loc[stop, "total_kw"] = 0.9
        row = self.table(frame, regions).iloc[0]
        self.assertTrue(row.accepted)
        self.assertAlmostEqual(row.ev_kw, 2.2)
        self.assertEqual(row.before_first_utc, stop - pd.Timedelta(minutes=15))
        self.assertEqual(row.before_last_utc, stop - pd.Timedelta(minutes=10))
        self.assertEqual(row.after_first_utc, stop + pd.Timedelta(minutes=5))
        self.assertEqual(row.after_last_utc, stop + pd.Timedelta(minutes=10))

    def test_local_background_cancels_without_subtracting_global_baseload(self):
        frame, regions = self.data()
        region = regions[0]
        frame.loc[(frame.index >= region.start) & (frame.index < region.end), "total_kw"] = 2.9
        frame.loc[(frame.index >= region.end) & (frame.index < pd.Timestamp(region.end) + pd.Timedelta(minutes=30)), "total_kw"] = 0.7
        row = self.table(frame, regions).iloc[0]
        self.assertAlmostEqual(row.ev_kw, 2.2)
        self.assertAlmostEqual(row.after_background_kw, 0.7)

    def test_noisy_start_does_not_exclude_an_otherwise_clean_ending(self):
        frame, _ = self.data()
        local = frame.index.tz_convert("Europe/Brussels")
        frame.loc[(local.hour == 10) & (local.minute < 30), "total_kw"] = 6
        regions = detect_regions(frame, EVDetectionConfig(), 0.2)
        self.assertFalse(regions[0].consistent)
        row = self.table(frame, regions).iloc[0]
        self.assertTrue(row.accepted)
        self.assertAlmostEqual(row.ev_kw, 2.2)

    def test_negative_drop_missing_reading_and_unstable_background_are_rejected(self):
        for cause in ("nonpositive_drop", "missing_window_samples", "unstable_after"):
            frame, regions = self.data()
            stop = pd.Timestamp(regions[0].end)
            if cause == "nonpositive_drop":
                frame.loc[[stop + pd.Timedelta(minutes=5), stop + pd.Timedelta(minutes=10)], "total_kw"] = 3
            elif cause == "missing_window_samples":
                frame.loc[stop - pd.Timedelta(minutes=15), "total_kw"] = np.nan
            else:
                frame.loc[stop + pd.Timedelta(minutes=5), "total_kw"] = 1
            row = self.table(frame, regions).iloc[0]
            with self.subTest(cause=cause):
                self.assertFalse(row.accepted)
                self.assertIn(cause, row.rejection_reason)

    def test_night_baseload_gives_equal_weight_to_nights_and_keeps_uncertainty_reproducible(self):
        frame = consumption_frame(source_days(loads=[("2026-09-02 10:00", "2026-09-02 12:00", 2.4)], count=5))
        local = frame.index.tz_convert("Europe/Brussels")
        frame.loc[(local.date == DAY - timedelta(days=1)) & (local.hour < 6), "total_kw"] = 0.8
        regions = detect_regions(frame, EVDetectionConfig(), 0.2)
        kwargs = dict(start=DAY - timedelta(days=1), end=DAY + timedelta(days=3),
                      timezone="Europe/Brussels", seed_threshold_kw=2.13, cadence_seconds=300)
        first = estimate_loads(frame, regions, **kwargs)
        second = estimate_loads(frame, regions, **kwargs)
        self.assertAlmostEqual(first.baseline_kw, 0.2)
        self.assertAlmostEqual(first.ev_kw, 2.2)
        self.assertEqual(first.summary, second.summary)
        self.assertIsNone(first.summary["bootstrap_95pct_low_kw"])  # one ending is too few
        self.assertEqual(len(first.sensitivity), 6)

    def test_mean_gives_each_clean_stop_equal_weight_and_bootstraps_the_same_estimator(self):
        frame = consumption_frame(source_days(loads=[
            ("2026-09-02 08:00", "2026-09-02 10:00", 2.2),
            ("2026-09-02 11:00", "2026-09-02 14:00", 2.3),
            ("2026-09-02 16:00", "2026-09-02 20:00", 2.7),
            ("2026-09-03 02:00", "2026-09-03 04:00", 3.1),
        ]))
        regions = detect_regions(frame, EVDetectionConfig(), 0.2)
        result = estimate_loads(frame, regions, start=DAY, end=DAY + timedelta(days=1),
                                timezone="Europe/Brussels", seed_threshold_kw=2.13, cadence_seconds=300)
        # Drops 2.0, 2.1, 2.5, 2.9 have unequal session lengths. Their mean is
        # 2.375, their median 2.3; neither duration nor reading count weights stops.
        self.assertEqual(result.ev_kw, 2.375)
        self.assertAlmostEqual(result.summary["mean_ev_kw"], 2.375)
        self.assertEqual(result.summary["method"], "mean_clean_stop_drop")
        self.assertAlmostEqual(result.summary["earlier_fit_ev_kw"], 2.2)
        self.assertAlmostEqual(result.summary["nighttime_ev_kw"], 2.9)
        self.assertIn("mean_ev_kw", result.sensitivity)
        self.assertNotIn("median_ev_kw", result.sensitivity)
        self.assertTrue(np.allclose(result.sensitivity["mean_ev_kw"], 2.375))
        for variant in result.summary["quality_sensitivity"]:
            self.assertAlmostEqual(variant["mean_ev_kw"], 2.375)
        drops = result.endings.loc[result.endings["accepted"], "ev_kw"].to_numpy()
        draws = np.random.default_rng(result.config.random_seed).choice(
            drops, size=(result.config.bootstrap_repetitions, len(drops)), replace=True)
        expected = np.quantile(draws.mean(axis=1), [0.025, 0.975])
        np.testing.assert_allclose([result.summary["bootstrap_95pct_low_kw"],
                                    result.summary["bootstrap_95pct_high_kw"]], expected)
        self.assertFalse(np.allclose(expected, np.quantile(np.median(draws, axis=1), [0.025, 0.975])))

    def test_adopted_power_is_rounded_but_the_calibration_mean_is_preserved(self):
        frame = consumption_frame(source_days(loads=[
            ("2026-09-02 08:00", "2026-09-02 10:00", 2.2),
            ("2026-09-02 11:00", "2026-09-02 13:00", 2.3),
            ("2026-09-02 16:00", "2026-09-02 18:00", 2.6),
        ]))
        regions = detect_regions(frame, EVDetectionConfig(), 0.2)
        result = estimate_loads(frame, regions, start=DAY, end=DAY,
                                timezone="Europe/Brussels", seed_threshold_kw=2.13, cadence_seconds=300)
        self.assertEqual(result.ev_kw, 2.167)
        self.assertAlmostEqual(result.summary["mean_ev_kw"], 6.5 / 3)


if __name__ == "__main__":
    unittest.main()
