"""Analysis correctness and shared chart rendering without pyplot state changes."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import matplotlib
from matplotlib.dates import date2num
import numpy as np

from home_energy.charts import COLORS, LineSeries, build_line_chart, chart_style, render_png, save_png
from home_energy.prices.analysis import PriceRange, analyse_prices
from home_energy.prices.chart import create_price_chart
from home_energy.usage.chart import create_ev_usage_chart
from home_energy.usage.daily import analyse_daily_usage
from price_helpers import price_day
from helpers import parsed_energy_day as parsed
from service_helpers import assert_png
from usage_helpers import DAY, NOW, ZONE, complete_source_days


class PriceAnalysisTests(unittest.TestCase):
    def test_warning_regions_expand_in_both_directions_only_around_triggered_peaks(self):
        cases = (
            ("joined peaks", "300", "25", [275, 280, 305, 290, 301, 276, 275], [(1, 5)]),
            ("no peak", "300", "25", [280, 300, 290], []),
            ("exact lower boundary splits", "300", "25", [276, 301, 275, 301, 276], [(0, 1), (3, 4)]),
            ("zero margin", "300", "0", [300, 301, 300, 301, 299], [(1, 1), (3, 3)]),
            ("custom trigger", "250", "10", [240, 241, 251, 245, 240], [(1, 3)]),
            ("negative prices", "-10.25", "0.5", ["-10.75", "-10.74", "-10.249", "-10.5", "-10.75"], [(1, 3)]),
            ("exact decimals", "300.0001", "0.0001", ["300", "300.0001", "300.0002", "300.0001", "300"], [(1, 3)]),
        )
        for name, threshold, margin, prices, spans in cases:
            with self.subTest(name=name):
                day = price_day(value=str(Decimal(threshold) - Decimal(margin) - 100))
                for index, price in enumerate(prices, start=40):
                    day.intervals[index] = replace(day.intervals[index], price_eur_per_mwh=Decimal(str(price)))
                original = tuple(day.intervals)
                analysis = analyse_prices(day, threshold=threshold, hysteresis=margin)
                expected = {40 + index for first, last in spans for index in range(first, last + 1)}
                self.assertEqual({i for i, high in enumerate(analysis.high_mask) if high}, expected)
                self.assertEqual(analysis.high_price_ranges, tuple(
                    PriceRange(day.intervals[40 + first].start_at, day.intervals[40 + last].end_at)
                    for first, last in spans
                ))
                self.assertEqual(tuple(day.intervals), original)

    def test_regions_clip_at_midnight_and_follow_elapsed_intervals_on_dst_days(self):
        for selected in (date(2025, 9, 30), date(2026, 3, 29), date(2026, 10, 25)):
            with self.subTest(day=selected):
                day = price_day(selected, value="0")
                spans = [(0, 1), (7, 16), (len(day.intervals) - 2, len(day.intervals) - 1)]
                for first, last in spans:
                    for index in range(first, last + 1):
                        price = Decimal("301" if index == first + 1 else "276")
                        day.intervals[index] = replace(day.intervals[index], price_eur_per_mwh=price)
                analysis = analyse_prices(day)
                self.assertEqual(analysis.high_price_ranges, tuple(
                    PriceRange(day.intervals[first].start_at, day.intervals[last].end_at)
                    for first, last in spans
                ))

    def test_adjacent_high_intervals_merge_and_last_interval_ends_at_midnight(self):
        day = price_day(value="0")
        for index in (1, 2, 5, 95):
            day.intervals[index] = replace(day.intervals[index], price_eur_per_mwh=Decimal("301"))
        ranges = analyse_prices(day).high_price_ranges
        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[0].end_at, day.intervals[2].end_at)
        self.assertEqual(ranges[-1].end_at, day.intervals[-1].end_at)

    def test_reference_profile_alignment_hourly_and_dst(self):
        hourly = analyse_prices(price_day(date(2025, 9, 30)), reference_profile=range(96))
        self.assertEqual(hourly.reference_prices[0], 1.5)
        self.assertEqual(hourly.reference_prices[1], 5.5)
        spring = analyse_prices(price_day(date(2026, 3, 29)), reference_profile=range(96))
        self.assertEqual(spring.reference_prices[8], 12)
        autumn = analyse_prices(price_day(date(2026, 10, 25)), reference_profile=range(96))
        self.assertEqual(autumn.reference_prices[8:12], autumn.reference_prices[12:16])
        self.assertIsNone(analyse_prices(price_day(), reference_profile=None).reference_prices)

    def test_invalid_analysis_options(self):
        for threshold in ("invalid", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                analyse_prices(price_day(), threshold=threshold)
        for margin in ("invalid", "NaN", "Infinity", "-0.001", None, True):
            with self.subTest(margin=margin), self.assertRaises(ValueError):
                analyse_prices(price_day(), hysteresis=margin)
        for profile in ([1] * 95, [float("nan")] * 96):
            with self.assertRaises(ValueError):
                analyse_prices(price_day(), reference_profile=profile)


class ChartTests(unittest.TestCase):
    def test_price_steps_preserve_actual_prices_and_red_ranges_without_bridging_gaps(self):
        day = price_day(value="-50")
        for index, price in enumerate((275, 280, 305, 290, 301, 276, 275, 500, 0, 300), start=40):
            day.intervals[index] = replace(day.intervals[index], price_eur_per_mwh=Decimal(price))
        figure = create_price_chart(day)
        self.addCleanup(figure.clear)
        ax = figure.axes[0]
        actual = next(line.get_path().vertices for line in ax.lines if line.get_label() == str(day.day))
        red = next(line for line in ax.lines if line.get_color() == COLORS["error"])
        red_path = red.get_path().vertices
        for index, interval in enumerate(day.intervals):
            for fraction in (0.25, 0.5, 0.75):
                stamp = date2num(interval.start_at + (interval.end_at - interval.start_at) * fraction)
                self.assertEqual(np.interp(stamp, actual[:, 0], actual[:, 1]), float(interval.price_eur_per_mwh))
                value = np.interp(stamp, red_path[:, 0], red_path[:, 1])
                if index in {41, 42, 43, 44, 45, 47}:
                    self.assertEqual(value, float(interval.price_eur_per_mwh))
                else:
                    self.assertTrue(np.isnan(value), index)
        self.assertIn("300", red.get_label())
        self.assertIn("275", red.get_label())
        np.testing.assert_allclose(ax.get_xlim(), date2num([day.intervals[0].start_at, day.intervals[-1].end_at]))

    def test_scoped_style_and_builder_restore_global_settings(self):
        original = matplotlib.rcParams.copy()
        with chart_style():
            build_line_chart([LineSeries([0, 1], [1, 2])], title="Usage").clear()
        figure = build_line_chart([LineSeries([0, 1], [1, 2])], title="Usage")
        self.addCleanup(figure.clear)
        for key in original:
            if key != "backend":  # Accessing backend can lazily resolve Matplotlib's auto sentinel.
                self.assertEqual(matplotlib.rcParams[key], original[key], key)

    def test_generic_usage_chart_keeps_missing_readings_as_gaps_and_saves_png(self):
        usage = parsed()
        samples = [sample for sample in usage.samples if sample.measurement == usage.available_series[0]]
        figure = build_line_chart([LineSeries([s.sample_index for s in samples], [s.value for s in samples],
                                              label="Usage")], title="Energy usage", subtitle="Synthetic usage data")
        self.addCleanup(figure.clear)
        self.assertTrue(np.isnan(figure.axes[0].lines[0].get_ydata()).any())
        with tempfile.TemporaryDirectory() as directory:
            output = save_png(figure, Path(directory) / "nested" / "usage.png")
            assert_png(self, output.read_bytes())

    def test_price_chart_handles_negative_prices_last_highlight_and_dst_ticks(self):
        day = price_day(date(2026, 10, 25), value="-50")
        day.intervals[-1] = replace(day.intervals[-1], price_eur_per_mwh=Decimal("400"))
        figure = create_price_chart(day)
        self.addCleanup(figure.clear)
        ax = figure.axes[0]
        self.assertLess(ax.get_ylim()[0], 0)
        labels = [tick.get_text() for tick in ax.get_xticklabels()]
        self.assertIn("02:00\n+02", labels)
        self.assertIn("02:00\n+01", labels)
        self.assertEqual(labels[-1], "24:00")
        self.assertAlmostEqual(ax.get_xticks()[-1], ax.get_xlim()[1])
        self.assertEqual(ax.lines[-1].get_xdata()[-1], day.intervals[-1].end_at)
        self.assertEqual(ax.lines[-1].get_color(), COLORS["error"])
        self.assertAlmostEqual(ax.get_xlim()[1] - ax.get_xlim()[0], 25 / 24)

    def test_ev_chart_shows_interval_power_without_smoothing_and_labels_dst_hours(self):
        for selected, clock, loads in (
            (DAY, NOW, [("2026-09-02 23:30", "2026-09-03 01:30", 2.4)]),
            (date(2026, 10, 25), datetime(2026, 10, 27, tzinfo=ZONE),
             [("2026-10-25T01:30:00+02:00", "2026-10-25T02:30:00+01:00", 2.4)]),
        ):
            with self.subTest(day=selected):
                report = analyse_daily_usage(complete_source_days(first=selected - timedelta(days=1), loads=loads),
                                             selected, now=clock)
                figure = create_ev_usage_chart(report)
                self.addCleanup(figure.clear)
                lines = {line.get_label(): line.get_path().vertices for line in figure.axes[0].lines}
                for interval in report.consumption:
                    for fraction in (0.25, 0.5, 0.75):
                        stamp = date2num(interval.start_at + (interval.end_at - interval.start_at) * fraction)
                        for label, power in (("Total consumption", interval.power_kw),
                                             ("Estimated EV charging", interval.ev_power_kw)):
                            path = lines[label]
                            self.assertAlmostEqual(np.interp(stamp, path[:, 0], path[:, 1]), float(power))
                np.testing.assert_allclose(figure.axes[0].get_xlim(),
                                           date2num([report.consumption[0].start_at, report.consumption[-1].end_at]))
                self.assertEqual(figure.axes[0].get_xticklabels()[-1].get_text(), "24:00")
                self.assertAlmostEqual(figure.axes[0].get_xticks()[-1], figure.axes[0].get_xlim()[1])
                if selected.month == 10:
                    labels = [tick.get_text() for tick in figure.axes[0].get_xticklabels()]
                    self.assertIn("02:00\n+02", labels)
                    self.assertIn("02:00\n+01", labels)

    def test_render_failure_restores_style_and_next_render_succeeds(self):
        original = matplotlib.rcParams.copy()
        figure = build_line_chart([LineSeries([0, 1], [1, 2])], title="Usage")
        self.addCleanup(figure.clear)
        with patch.object(figure, "savefig", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                render_png(lambda: figure)
        assert_png(self, render_png(lambda: build_line_chart([LineSeries([0, 1], [3, 4])], title="Retry")))
        for key in original:
            if key != "backend":
                self.assertEqual(matplotlib.rcParams[key], original[key], key)

    def test_concurrent_price_and_ev_exports_produce_independent_valid_images(self):
        barrier = Barrier(2)
        usage = analyse_daily_usage(complete_source_days(), DAY, now=NOW)

        def export(create):
            barrier.wait(timeout=5)
            return render_png(create)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(export, create) for create in (
                lambda: create_price_chart(price_day()), lambda: create_ev_usage_chart(usage),
            )]
            images = [future.result(timeout=15) for future in futures]
        for png in images:
            assert_png(self, png)
        self.assertNotEqual(*images)

    def test_invalid_generic_chart_inputs(self):
        for lines in ([], [LineSeries([0], [0, 1])], [LineSeries([0], [0], role="rainbow")]):
            with self.assertRaises(ValueError):
                build_line_chart(lines, title="Invalid")
