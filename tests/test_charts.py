"""Analysis correctness and shared chart rendering without pyplot state changes."""

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import matplotlib
from matplotlib.colors import to_rgba
import numpy as np

from home_energy.charts import COLORS, LineSeries, build_line_chart, chart_style, save_png
from home_energy.prices.analysis import analyse_prices, smooth_series
from home_energy.prices.chart import create_price_chart
from price_helpers import price_day
from test_energy_balance import parsed


class PriceAnalysisTests(unittest.TestCase):
    def test_narrow_actual_spike_remains_high_even_when_smoothed_below_threshold(self):
        day = price_day(value="0")
        day.intervals[40] = replace(day.intervals[40], price_eur_per_mwh=Decimal("500"))
        day.intervals[41] = replace(day.intervals[41], price_eur_per_mwh=Decimal("300"))
        analysis = analyse_prices(day)
        self.assertLess(analysis.smoothed_prices[40], 300)
        self.assertTrue(analysis.high_mask[40])
        self.assertFalse(analysis.high_mask[41])
        self.assertEqual(analysis.high_price_ranges[0].start_at, day.intervals[40].start_at)
        self.assertEqual(analysis.high_price_ranges[0].end_at, day.intervals[40].end_at)
        self.assertEqual(day.intervals[40].price_eur_per_mwh, Decimal("500"))

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

    def test_short_series_and_invalid_analysis_options(self):
        for values in ([], [1], [1, 2]):
            self.assertEqual(smooth_series(values), tuple(values))
        for threshold in ("invalid", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                analyse_prices(price_day(), threshold=threshold)
        for profile in ([1] * 95, [float("nan")] * 96):
            with self.assertRaises(ValueError):
                analyse_prices(price_day(), reference_profile=profile)


class ChartTests(unittest.TestCase):
    def test_scoped_style_and_builder_restore_global_settings(self):
        original = matplotlib.rcParams.copy()
        with chart_style():
            self.assertEqual(matplotlib.rcParams["figure.facecolor"], COLORS["background"])
            self.assertEqual(matplotlib.rcParams["axes.grid.axis"], "y")
        figure = build_line_chart([LineSeries([0, 1], [1, 2])], title="Usage")
        self.addCleanup(figure.clear)
        self.assertEqual(figure.get_facecolor(), to_rgba(COLORS["background"]))
        ax = figure.axes[0]
        self.assertTrue(all(not spine.get_visible() for spine in ax.spines.values()))
        self.assertFalse(any(line.get_visible() for line in ax.get_xgridlines()))
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
            self.assertEqual(output.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertGreater(output.stat().st_size, 1000)

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
        self.assertEqual(ax.lines[-1].get_xdata()[-1], day.intervals[-1].end_at)
        self.assertEqual(ax.lines[-1].get_color(), COLORS["error"])
        self.assertEqual(len(ax.get_legend_handles_labels()[1]), 3)
        self.assertAlmostEqual(ax.get_xlim()[1] - ax.get_xlim()[0], 25 / 24)

    def test_invalid_generic_chart_inputs(self):
        for lines in ([], [LineSeries([0], [0, 1])], [LineSeries([0], [0], role="rainbow")]):
            with self.assertRaises(ValueError):
                build_line_chart(lines, title="Invalid")
