"""Native cadence, DST, API redaction, configuration, and date policies."""

import os
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import requests
from entsoe.exceptions import NoMatchingDataError

from home_energy.prices.entsoe import (DayAheadPriceClient, InvalidPriceDataError, PriceAPIError,
                                      PriceConfigurationError, PricesUnavailableError, normalize_prices)
from price_helpers import price_day, price_series
from helpers import isolated_configuration


class PriceNormalizationTests(unittest.TestCase):
    def test_native_cadence_and_both_dst_days(self):
        for day, count, minutes in [(date(2025, 9, 30), 24, 60), (date(2025, 3, 30), 23, 60),
                                   (date(2026, 10, 10), 96, 15), (date(2026, 3, 29), 92, 15),
                                   (date(2026, 10, 25), 100, 15), (date(2024, 10, 27), 25, 60)]:
            with self.subTest(day=day):
                loaded = price_day(day)
                self.assertEqual(len(loaded.intervals), count)
                self.assertEqual(loaded.intervals[0].end_at - loaded.intervals[0].start_at,
                                 timedelta(minutes=minutes))
                self.assertTrue(all(i.start_at.utcoffset() == timedelta(0) for i in loaded.intervals))

    def test_zero_negative_decimal_sorting_dataframe_and_end_exclusion(self):
        series = price_series(value="-12.3400")
        series.iloc[0] = "0"
        series.loc[series.index[-1] + timedelta(minutes=15)] = "999"
        day = normalize_prices(series.iloc[::-1].to_frame(), date(2026, 10, 10))
        self.assertEqual(len(day.intervals), 96)
        self.assertEqual(day.intervals[0].price_eur_per_mwh, Decimal(0))
        self.assertEqual(day.intervals[1].price_eur_per_mwh, Decimal("-12.3400"))
        self.assertEqual(day.intervals[1].price_eur_per_mwh.as_tuple().exponent, -4)

    def test_incomplete_conflicting_naive_and_bad_data_are_rejected(self):
        series = price_series()
        bad_price = series.copy()
        for value in (None, float("nan"), "Infinity", "not-a-number", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bad_price.iloc[10] = value
                normalize_prices(bad_price, date(2026, 10, 10))
        invalid = [series.drop(series.index[10]), series.iloc[1:], series.iloc[:-1],
                   pd.concat([series, series.iloc[:1]]), series.set_axis(series.index.tz_localize(None)),
                   pd.concat([series, series], axis=1), series.set_axis(series.index + timedelta(minutes=1))]
        for response in invalid:
            with self.subTest(shape=response.shape), self.assertRaises(ValueError):
                normalize_prices(response, date(2026, 10, 10))

    def test_unpublished_day_and_wrong_day_are_retryable(self):
        for response in (None, pd.Series(dtype=float), price_series(date(2026, 10, 9))):
            with self.assertRaises(PricesUnavailableError):
                normalize_prices(response, date(2026, 10, 10))

    def test_manually_changed_models_are_validated(self):
        for changes in ({"price_eur_per_mwh": Decimal("NaN")},
                        {"start_at": datetime(2026, 10, 10)},
                        {"end_at": datetime(2026, 10, 11, tzinfo=timezone.utc)}):
            day = price_day()
            day.intervals[0] = replace(day.intervals[0], **changes)
            with self.assertRaises(ValueError):
                day.validate()


class PriceClientTests(unittest.TestCase):
    def test_bad_provider_data_and_configuration_have_distinct_error_types(self):
        with patch("home_energy.prices.entsoe.EntsoePandasClient") as upstream:
            upstream.return_value.query_day_ahead_prices.return_value = price_series().iloc[1:]
            with DayAheadPriceClient("test-key") as client, self.assertRaises(InvalidPriceDataError):
                client.get_day(date(2026, 10, 10))
        with self.assertRaises(PriceConfigurationError):
            DayAheadPriceClient("")
        with patch("home_energy.prices.entsoe.dotenv_values", side_effect=UnicodeError("secret")), \
                self.assertRaises(PriceConfigurationError) as error:
            DayAheadPriceClient.from_config()
        self.assertNotIn("secret", str(error.exception))

    def test_configuration_precedence_no_interpolation_or_environment_mutation(self):
        file_key = "literal ${SECRET} # / +"
        for process_key in (None, "process-key"):
            environment = {"SECRET": "must-not-be-expanded"}
            if process_key is not None:
                environment["ENTSOE_API_KEY"] = process_key
            with self.subTest(process_key=process_key), \
                    isolated_configuration(f"ENTSOE_API_KEY='{file_key}'", environment), \
                    patch("home_energy.prices.entsoe.EntsoePandasClient") as upstream:
                with DayAheadPriceClient.from_config():
                    self.assertEqual(upstream.call_args.kwargs["api_key"], process_key or file_key)
                self.assertEqual(dict(os.environ), environment)
                upstream.return_value.session.close.assert_called_once()
        with isolated_configuration("ENTSOE_API_KEY=file-key", {"ENTSOE_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "ENTSOE_API_KEY"):
                DayAheadPriceClient.from_config()

    def test_query_bounds_are_local_midnights_and_no_resolution_override(self):
        with patch("home_energy.prices.entsoe.EntsoePandasClient") as upstream:
            upstream.return_value.query_day_ahead_prices.return_value = price_series(date(2026, 3, 29))
            with DayAheadPriceClient("test-key") as client:
                day = client.get_day(date(2026, 3, 29))
            query = upstream.return_value.query_day_ahead_prices.call_args.kwargs
            self.assertEqual(query["country_code"], "BE")
            self.assertNotIn("resolution", query)
            self.assertEqual(query["start"].hour, 0)
            self.assertEqual(query["end"].hour, 0)
            self.assertEqual(query["end"].tz_convert("UTC") - query["start"].tz_convert("UTC"), timedelta(hours=23))
            self.assertEqual(len(day.intervals), 92)

    def test_upstream_request_errors_do_not_expose_tokens(self):
        with patch("home_energy.prices.entsoe.EntsoePandasClient") as upstream:
            upstream.return_value.query_day_ahead_prices.side_effect = requests.HTTPError("securityToken=private-key")
            with DayAheadPriceClient("test-key") as client, self.assertRaises(PriceAPIError) as error:
                client.get_day(date(2026, 10, 10))
            self.assertNotIn("private-key", str(error.exception))
            upstream.return_value.query_day_ahead_prices.side_effect = NoMatchingDataError()
            with DayAheadPriceClient("test-key") as client, self.assertRaises(PricesUnavailableError):
                client.get_day(date(2026, 10, 10))
