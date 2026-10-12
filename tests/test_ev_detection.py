"""Boundary attribution, interruptions and missing-data safety for EV hypotheses."""

from datetime import date, datetime, timedelta
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from home_energy.investigations.ev.analysis import EVDetectionConfig, consumption_frame, investigate
from home_energy.investigations.ev.report import (
    load_investigation_days, normalized_region_parts, region_profile, write_index, sensitivity_table,
)

from home_energy.usage.ingestion import UsageIngestionService
from service_helpers import MemoryRepository, RecordingProvider
from usage_helpers import DAY, NOW, ZONE, complete_energy_payload, source_days


class DetectionTests(unittest.TestCase):
    def test_cross_midnight_region_is_counted_once_and_split_between_days(self):
        days = source_days(count=4, loads=[("2026-09-02 23:30", "2026-09-03 01:30", 2.4)])
        result = investigate(days, DAY, DAY + timedelta(days=1))
        self.assertEqual(len(result.regions), 1)
        self.assertAlmostEqual(result.regions[0].duration_hours, 2)
        self.assertEqual(result.daily["region_count"].tolist(), [1, 1])
        np.testing.assert_allclose(result.daily["charging_hours"], [0.5, 1.5])
        np.testing.assert_allclose(result.daily["ev_kwh"], [1.1, 3.3])
        self.assertAlmostEqual(result.summary["estimated_ev_kwh"], 4.4)

    def test_previous_day_counts_toward_minimum_duration_but_energy_is_clipped(self):
        result = investigate(source_days(loads=[("2026-09-01 22:30", "2026-09-02 00:30", 2.4)]), DAY, DAY)
        self.assertEqual(len(result.regions), 1)
        self.assertAlmostEqual(result.daily["charging_hours"].iloc[0], 0.5)
        self.assertAlmostEqual(result.daily["ev_kwh"].iloc[0], 1.1)

    def test_short_interruptions_are_not_bridged(self):
        loads = [("2026-09-02 10:00", "2026-09-02 10:35", 2.4),
                 ("2026-09-02 10:40", "2026-09-02 11:15", 2.4)]
        result = investigate(source_days(loads=loads), DAY, DAY)
        self.assertEqual(result.regions, [])

    def test_exact_120_minutes_qualifies_but_90_and_115_minutes_do_not(self):
        loads = [("2026-09-02 10:00", "2026-09-02 11:30", 2.4),
                 ("2026-09-02 12:00", "2026-09-02 13:55", 2.4),
                 ("2026-09-02 14:00", "2026-09-02 15:10", 2.4),
                 ("2026-09-02 16:00", "2026-09-02 18:00", 2.4),
                 ("2026-09-02 19:00", "2026-09-02 21:00", 2.13)]
        result = investigate(source_days(loads=loads), DAY, DAY)
        self.assertEqual(len(result.regions), 1)
        self.assertEqual(result.regions[0].sample_count, 24)

    def test_null_and_missing_timestamp_break_regions_and_remain_unknown(self):
        gap = pd.Timestamp("2026-09-02 10:35", tz=ZONE).tz_convert("UTC")
        loads = [("2026-09-02 10:00", "2026-09-02 12:00", 2.4)]
        days = source_days(loads=loads, missing=[gap])
        result = investigate(days, DAY, DAY)
        self.assertEqual(result.regions, [])
        self.assertTrue(np.isnan(result.samples.loc[gap, "ev_kw"]))
        # Also represent the same outage as an absent timestamp, not a null.
        middle = days[1]
        middle.samples = [s for s in middle.samples if s.observed_at != gap]
        result = investigate(days, DAY, DAY)
        self.assertEqual(result.regions, [])
        self.assertFalse(result.samples.loc[gap, "observed"])

    def test_q10_block_subtracts_baseline_and_ignores_other_series(self):
        days = source_days(loads=[("2026-09-02 12:00", "2026-09-02 14:00", 2.4)])
        result = investigate(days, DAY, DAY)
        region = result.regions[0]
        self.assertAlmostEqual(region.total_q10_kw, 2.4)
        self.assertAlmostEqual(result.baseline_kw, 0.2)
        self.assertAlmostEqual(region.ev_kw, 2.2)
        self.assertAlmostEqual(region.ev_above_local_background_kw, 2.2)
        self.assertAlmostEqual(result.shared_ev_kw, 2.2)

    def test_variable_regions_are_retained_for_review(self):
        loads = [("2026-09-02 12:00", "2026-09-02 12:30", 2.3),
                 ("2026-09-02 12:30", "2026-09-02 14:00", 5.0)]
        result = investigate(source_days(loads=loads), DAY, DAY)
        self.assertEqual(len(result.regions), 1)
        self.assertFalse(result.regions[0].consistent)
        self.assertIsNone(result.shared_ev_kw)

    def test_missing_outer_context_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "boundary"):
            investigate(source_days(first=DAY, count=1), DAY, DAY)

    def test_one_hour_boundary_context_is_insufficient_on_both_sides(self):
        for side in ("previous", "following"):
            days = source_days()
            if side == "previous":
                days[0].samples = [s for s in days[0].samples if s.observed_at.astimezone(ZONE).hour >= 23]
            else:
                days[-1].samples = [s for s in days[-1].samples if s.observed_at.astimezone(ZONE).hour < 1]
            with self.subTest(side=side), self.assertRaisesRegex(ValueError, "120 minutes"):
                investigate(days, DAY, DAY)

    def test_exact_120_minute_boundary_context_supports_midnight_edges(self):
        days = source_days(loads=[("2026-09-01 22:00", "2026-09-02 00:30", 2.4),
                                 ("2026-09-02 23:30", "2026-09-03 02:00", 2.4)])
        days[0].samples = [s for s in days[0].samples if s.observed_at.astimezone(ZONE).strftime("%H:%M") >= "22:00"]
        days[-1].samples = [s for s in days[-1].samples if s.observed_at.astimezone(ZONE).strftime("%H:%M") < "02:00"]
        result = investigate(days, DAY, DAY)
        self.assertEqual(len(result.regions), 2)
        self.assertAlmostEqual(result.daily["charging_hours"].iloc[0], 1)
        self.assertAlmostEqual(result.daily["ev_kwh"].iloc[0], 2.2)

    def test_units_and_unresolved_timestamps_are_not_silently_guessed(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            consumption_frame(source_days(unit="W"), unit="kW")
        days = source_days()
        days[1].samples[0].observed_at = None
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            consumption_frame(days)

    def test_dst_duration_uses_elapsed_time_not_wall_clock(self):
        spring = date(2026, 3, 29)
        days = source_days(first=spring - timedelta(days=1), loads=[("2026-03-29 01:00", "2026-03-29 04:00", 2.4)])
        result = investigate(days, spring, spring)
        self.assertAlmostEqual(result.daily["charging_hours"].iloc[0], 2)
        self.assertEqual(len(result.samples.loc[result.samples["source_date"] == spring]), 276)

    def test_invalid_parameters_fail_before_analysis(self):
        for kwargs in ({"threshold_kw": float("nan")}, {"block_quantile": 1.1}, {"minimum_minutes": 0},
                       {"stability_absolute_kw": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                EVDetectionConfig(**kwargs)

    def test_no_detections_still_produce_readable_index_with_boundary_explanation(self):
        result = investigate(source_days(), DAY, DAY)
        with tempfile.TemporaryDirectory() as output:
            write_index(result, Path(output), sensitivity_table(result))
            text = (Path(output) / "index.html").read_text(encoding="utf-8")
            self.assertIn("0 candidate regions", text)
            self.assertIn("22:00–00:00", text)
            self.assertIn("00:00–02:00", text)

    def test_ev_level_trigger_excludes_lower_tail_and_splits_interruptions(self):
        days = source_days(loads=[("2026-09-02 12:00", "2026-09-02 14:00", 2.4),
                                 ("2026-09-02 14:00", "2026-09-02 14:30", 1.8),
                                 ("2026-09-02 15:00", "2026-09-02 15:45", 2.4),
                                 ("2026-09-02 15:45", "2026-09-02 15:50", 2.12),
                                 ("2026-09-02 15:50", "2026-09-02 16:35", 2.4)])
        result = investigate(days, DAY, DAY)
        self.assertEqual(result.config.threshold_kw, 2.13)
        self.assertEqual(len(result.regions), 1)
        self.assertEqual(result.regions[0].end.astimezone(ZONE).strftime("%H:%M"), "14:00")
        self.assertAlmostEqual(result.daily["charging_hours"].iloc[0], 2)
        self.assertAlmostEqual(result.shared_threshold_kw, result.shared_ev_kw)

    def test_quantile_blocks_are_bounded_and_shared_model_uses_its_own_trigger(self):
        # One low interval lies below q10-minus-baseline while still above the
        # main trigger. It must not be hidden under either displayed EV block.
        days = source_days(loads=[("2026-09-02 12:00", "2026-09-02 14:00", 3.0),
                                 ("2026-09-02 12:30", "2026-09-02 12:35", 2.2)])
        result = investigate(days, DAY, DAY)
        region = result.regions[0]
        self.assertAlmostEqual(region.quantile_ev_kw, 2.8)
        self.assertAlmostEqual(region.ev_kw, 2.2)
        self.assertTrue(region.block_limited)
        self.assertTrue((result.samples.ev_kw <= result.samples.total_kw).all())
        self.assertTrue((result.samples.shared_ev_kw <= result.samples.total_kw).all())
        # Neither half of the shared 2.8 kW run reaches 120 minutes.
        self.assertEqual(result.shared_regions, [])


class RegionProfileTests(unittest.TestCase):
    def result(self):
        return investigate(source_days(loads=[("2026-09-02 10:00", "2026-09-02 12:00", 2.4),
                                             ("2026-09-02 15:00", "2026-09-02 18:00", 2.4)]), DAY, DAY)

    def test_full_profiles_have_exact_context_and_preserve_different_durations(self):
        result = self.result()
        np.testing.assert_allclose([r.duration_hours for r in result.regions], [2, 3])
        for region in result.regions:
            profile = region_profile(result, region)
            self.assertEqual(profile.index[0], pd.Timestamp(region.start) - pd.Timedelta(hours=1))
            self.assertEqual(profile.index[-1] + pd.Timedelta(minutes=5), pd.Timestamp(region.end) + pd.Timedelta(hours=1))
            self.assertEqual((profile.section == "before").sum(), 12)
            self.assertEqual((profile.section == "after").sum(), 12)
            self.assertEqual((profile.section == "charging").sum(), region.sample_count)
            np.testing.assert_allclose(profile.hours_from_start - profile.hours_from_end, region.duration_hours)
            np.testing.assert_allclose(np.diff(profile.hours_from_start), 5 / 60)
            self.assertTrue(profile.loc[profile.section != "charging", "normalized_percent"].isna().all())

    def test_normalized_edges_do_not_borrow_readings_from_adjacent_sections(self):
        result = self.result()
        for region in result.regions:
            before, charging, after = normalized_region_parts(result, region)
            np.testing.assert_allclose(before[0][[0, -1]], [-60, 0])
            np.testing.assert_allclose(charging[0][[0, -1]], [0, 100])
            np.testing.assert_allclose(after[0][[0, -1]], [0, 60])
            np.testing.assert_allclose(before[1][:, 0], 0)
            np.testing.assert_allclose(charging[1][:, 0], 2.2)
            np.testing.assert_allclose(after[1][:, 0], 0)

    def test_missing_context_is_not_interpolated_or_extrapolated(self):
        result = self.result()
        region = result.regions[0]
        before_gap = pd.Timestamp(region.start) - pd.Timedelta(minutes=30)
        after_gap = pd.Timestamp(region.end) + pd.Timedelta(minutes=30)
        result.samples.loc[[before_gap, after_gap], "total_kw"] = np.nan
        before, _, after = normalized_region_parts(result, region)
        self.assertTrue(np.isnan(before[1][before[0] == -30]).all())
        self.assertTrue(np.isnan(after[1][after[0] == 30]).all())
        # Unavailable source timestamps must remain missing, including edges.
        result.samples = result.samples.loc[result.samples.index >= pd.Timestamp(region.start)]
        before, _, _ = normalized_region_parts(result, region)
        self.assertTrue(np.isnan(before[1]).all())


class ContextLoadingTests(unittest.TestCase):
    def test_unfinished_context_is_rejected_before_io(self):
        today = datetime.now(ZONE).date()
        repository = Mock()
        with patch("home_energy.investigations.ev.report.UsageIngestionService") as ingestion:
            for selected in (today - timedelta(days=1), today):
                with self.subTest(selected=selected), self.assertRaises(ValueError):
                    load_investigation_days(repository, selected, selected, fetch=True)
            ingestion.assert_not_called()
        repository.load_range.assert_not_called()

    def test_both_context_days_are_filled_and_loaded_from_storage(self):
        repository = MemoryRepository()
        provider = RecordingProvider(complete_energy_payload)
        ingestion = UsageIngestionService(repository, client_factory=lambda: provider, now=lambda: NOW)
        with patch("home_energy.investigations.ev.report.UsageIngestionService", return_value=ingestion), \
                redirect_stdout(io.StringIO()):
            loaded = load_investigation_days(repository, DAY, DAY, fetch=True)
            replay = load_investigation_days(repository, DAY, DAY, fetch=True)
        required = [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)]
        self.assertEqual([day.day for day in loaded], required)
        self.assertEqual(loaded, replay)
        self.assertEqual(provider.requests, required)
        self.assertEqual(investigate(loaded, DAY, DAY).summary["api_total_kwh"], 42.75)

    def test_missing_next_day_context_requires_fill(self):
        repository = MemoryRepository(source_days(count=2))
        with self.assertRaises(ValueError) as error:
            load_investigation_days(repository, DAY, DAY)
        self.assertIn(str(DAY + timedelta(days=1)), str(error.exception))
        self.assertIn("--fetch", str(error.exception))


if __name__ == "__main__":
    unittest.main()
