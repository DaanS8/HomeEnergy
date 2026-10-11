"""Response integrity, original values, numeric precision and DST resolution."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from home_energy.usage.energy_balance import EnergyBalanceValidationError, parse_energy_balance
from home_energy.usage.constants import PLANT_ID
from helpers import energy_payload


def parsed(payload=None, day=date(2026, 10, 10), **kwargs):
    return parse_energy_balance(energy_payload(day) if payload is None else payload,
                                plant_id=PLANT_ID, day=day, timezone_name="Europe/Brussels",
                                fetched_at=datetime(2026, 10, 11, tzinfo=timezone.utc), **kwargs)


class ParserTests(unittest.TestCase):
    def test_consumed_energy_preserves_zero_and_missing_totals(self):
        payload = energy_payload()
        self.assertIsNone(parsed(payload).consumed_energy)
        for raw, expected in [("14.060", Decimal("14.060")), ("0", Decimal("0")), ("--", None)]:
            payload["data"]["totalUsePower"] = raw
            self.assertEqual(parsed(payload).consumed_energy, expected)

    def test_values_precision_metadata_metrics_and_empty_series(self):
        day = parsed()
        self.assertEqual(day.sample_count, 3)
        self.assertEqual(day.available_series, ["productPower", "usePower"])
        self.assertEqual(day.empty_series, ["onGridPower"])
        self.assertEqual([s.original_value for s in day.samples],
                         ["0.000", "--", None, "1.12345678901234567890123456789", "2", ""])
        self.assertEqual([s.value for s in day.samples[:3]], [Decimal("0.000"), None, None])
        self.assertEqual(day.samples[3].value, Decimal("1.12345678901234567890123456789"))
        self.assertTrue(all(s.unit is None for s in day.samples))
        self.assertEqual(day.metadata, {"stationTimezone": None, "existInverter": True, "sceneType": 1})
        self.assertEqual({m.name: m.value for m in day.metrics}, {
            "totalProductPower": Decimal("12.650"), "selfUsePowerRatioByProduct": Decimal("33.333"),
            "totalBusinessChargePower": None})
        self.assertNotIn("message", day.metadata)
        self.assertEqual(day.samples[0].observed_at, datetime(2026, 10, 9, 22, tzinfo=timezone.utc))

    def test_units_are_only_assigned_by_explicit_mapping(self):
        day = parsed(units={"productPower": "verified-unit", "totalProductPower": "verified-daily-unit"})
        self.assertEqual(day.samples[0].unit, "verified-unit")
        self.assertIsNone(day.samples[3].unit)
        self.assertEqual(day.metrics[0].unit, "verified-daily-unit")

    def test_numeric_json_values_and_new_measurement_arrays(self):
        payload = energy_payload()
        payload["data"]["newMeasurement"] = [1, 1.5, "0"]
        payload["data"]["newFlag"] = False
        day = parsed(payload)
        self.assertEqual([s.value for s in day.samples[-3:]], [Decimal(1), Decimal("1.5"), Decimal(0)])
        self.assertFalse(day.metadata["newFlag"])

    def test_invalid_shapes_and_identity(self):
        for field, value in [("stationDn", "wrong"), ("xAxis", []), ("xAxis", [""]),
                             ("xAxis", [None]), ("productPower", ["1"]),
                             ("productPower", None), ("productPower", "1"),
                             ("totalProductPower", ["1", "2", "3"])]:
            with self.subTest(field=field, value=value):
                payload = energy_payload()
                payload["data"][field] = value
                with self.assertRaises(EnergyBalanceValidationError):
                    parsed(payload)
        for payload in ({}, {"data": None}, {"data": []}, []):
            with self.subTest(payload=payload), self.assertRaises(EnergyBalanceValidationError):
                parsed(payload)

    def test_empty_measurements_and_unsuccessful_response(self):
        payload = energy_payload()
        payload["data"]["productPower"] = []
        payload["data"]["usePower"] = []
        with self.assertRaises(EnergyBalanceValidationError):
            parsed(payload)
        for error in ({"success": False}, {"failCode": 9}, {"failReason": "failure"}):
            payload = energy_payload()
            payload.update(error)
            with self.subTest(error=error), self.assertRaises(EnergyBalanceValidationError):
                parsed(payload)

    def test_invalid_readings_are_rejected(self):
        for value in (True, {}, [], "invalid", "NaN", "Infinity", float("inf")):
            for field in ("productPower", "totalProductPower"):
                payload = energy_payload()
                if field == "productPower":
                    payload["data"][field][0] = value
                else:
                    payload["data"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(EnergyBalanceValidationError):
                    parsed(payload)

    def test_dst_ambiguous_nonexistent_and_unknown_labels_remain_unresolved(self):
        cases = [(date(2026, 10, 25), "2026-10-25 02:30", None),
                 (date(2026, 10, 25), "2026-10-25 02:30 DST", datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)),
                 (date(2026, 3, 29), "2026-03-29 02:30", None),
                 (date(2026, 1, 1), "2026-01-01 12:00", datetime(2026, 1, 1, 11, tzinfo=timezone.utc)),
                 (date(2026, 1, 1), "2026-01-01 12:00 DST", None),
                 (date(2026, 10, 10), "2026-10-10", None),
                 (date(2026, 10, 10), "2026-10-10 02:30+02:00", datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc)),
                 (date(2026, 10, 10), "unrecognized source label", None)]
        for day, label, expected in cases:
            payload = {"data": {"stationDn": PLANT_ID, "xAxis": [label, label], "usePower": ["1", "2"]}}
            with self.subTest(label=label):
                result = parsed(payload, day)
                self.assertEqual(result.samples[0].observed_at, expected)
                self.assertEqual([s.sample_index for s in result.samples], [0, 1])
                self.assertEqual(result.samples[0].axis_label, label)

    def test_wrong_label_day_is_rejected(self):
        payload = energy_payload()
        payload["data"]["xAxis"][0] = "2026-10-09 00:00 DST"
        with self.assertRaises(EnergyBalanceValidationError):
            parsed(payload)

    def test_model_validation_protects_manually_modified_days(self):
        day = parsed()
        day.samples.pop()
        with self.assertRaises(ValueError):
            day.validate()
        day = parsed()
        day.samples[0].value = Decimal(999)
        with self.assertRaises(ValueError):
            day.validate()
