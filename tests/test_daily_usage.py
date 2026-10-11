"""Production daily ingestion: boundaries, exact energy, snapshots and HTTP contract."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from home_energy.api.app import create_app
from home_energy.usage.daily import (DailyUsageService, DailyUsageRequestError, UsageConfigurationError,
                                     analyse_daily_usage)
from home_energy.usage.ev import EV_POWER_KW, detect_ev_blocks
from home_energy.usage.energy_balance import EnergyBalanceValidationError
from home_energy.usage.fusionsolar import FusionSolarError
from home_energy.usage.models import DailyMetric
from usage_helpers import DAY, ZONE, source_days


PATH = "/api/v1/usage/daily"
NOW = datetime(2026, 9, 4, 12, tzinfo=ZONE)


def days(**kwargs):
    result = source_days(**kwargs)
    for day in result:
        day.metrics.append(DailyMetric("totalUsePower", Decimal("42.750"), "42.750", "kWh"))
    return result


class DailyDetectionTests(unittest.TestCase):
    def test_two_hours_and_strict_power_threshold_with_no_baseload_subtraction(self):
        data = days(loads=[("2026-09-02 08:00", "2026-09-02 09:55", 2.4),
                           ("2026-09-02 10:00", "2026-09-02 12:00", 2.169),
                           ("2026-09-02 14:00", "2026-09-02 16:00", 2.4)])
        report = analyse_daily_usage(data, DAY, now=NOW)
        self.assertTrue(report.complete)
        self.assertEqual(len(report.consumption), 288)
        self.assertEqual(len(report.ev_blocks), 1)
        self.assertEqual(report.ev_blocks[0].power_kw, Decimal("2.169"))
        self.assertEqual(report.ev_total_kwh, Decimal("4.338"))
        self.assertEqual(report.total_kwh, Decimal("42.750"))
        self.assertTrue(all(s.ev_power_kw <= s.power_kw for s in report.consumption))

    def test_midnight_context_qualifies_short_daily_fragments_and_preserves_energy(self):
        data = days(loads=[("2026-09-01 22:30", "2026-09-02 00:30", 2.4),
                           ("2026-09-02 23:30", "2026-09-03 01:30", 2.4)])
        report = analyse_daily_usage(data, DAY, now=NOW)
        self.assertEqual([b.energy_kwh for b in report.ev_blocks], [Decimal("1.0845")] * 2)
        self.assertEqual(report.ev_total_kwh, EV_POWER_KW)
        following = analyse_daily_usage(data[1:], DAY + timedelta(days=1), now=NOW)
        self.assertEqual(following.ev_total_kwh + report.ev_blocks[1].energy_kwh, Decimal("4.338"))

    def test_interruptions_nulls_and_absent_readings_break_regions(self):
        for mode in ("dip", "null", "absent"):
            with self.subTest(mode=mode):
                data = days(loads=[("2026-09-02 10:00", "2026-09-02 13:00", 2.4)])
                target = data[1]
                sample = next(s for s in target.samples if s.measurement == "usePower" and s.sample_index == 138)
                if mode == "absent":
                    target.samples.remove(sample)
                else:
                    sample.value = Decimal("0.2") if mode == "dip" else None
                    sample.original_value = "0.2" if mode == "dip" else None
                report = analyse_daily_usage(data, DAY, now=NOW)
                self.assertEqual(report.ev_blocks, ())
                self.assertEqual(report.complete, mode == "dip")
                if mode != "dip":
                    self.assertIsNone(report.consumption[138].power_kw)
                    self.assertIsNone(report.consumption[138].ev_power_kw)

    def test_today_ignores_future_readings_and_marks_short_active_tail_pending(self):
        data = days(loads=[("2026-09-02 10:00", "2026-09-02 13:00", 2.4)])
        now = datetime(2026, 9, 2, 11, 2, tzinfo=ZONE)
        report = analyse_daily_usage(data[:2], DAY, now=now)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.consumption), 132)
        self.assertEqual(report.ev_blocks, ())
        self.assertIsNone(report.consumption[-1].ev_power_kw)
        self.assertTrue(all(s.end_at <= now for s in report.consumption))
        later = analyse_daily_usage(data[:2], DAY, now=now + timedelta(hours=1))
        self.assertEqual(later.ev_total_kwh, Decimal("4.338"))
        self.assertFalse(later.complete)

    def test_yesterday_is_provisional_until_next_day_two_hour_context_exists(self):
        data = days(loads=[("2026-09-02 23:30", "2026-09-03 01:30", 2.4)])
        early = analyse_daily_usage(data, DAY, now=datetime(2026, 9, 3, 1, tzinfo=ZONE))
        self.assertFalse(early.complete)
        self.assertIsNone(early.consumption[-1].ev_power_kw)
        final = analyse_daily_usage(data, DAY, now=datetime(2026, 9, 3, 2, tzinfo=ZONE))
        self.assertTrue(final.complete)
        self.assertEqual(final.ev_total_kwh, Decimal("1.0845"))

    def test_missing_total_is_null_and_never_replaced_by_power_integral(self):
        data = days()
        data[1].metrics = []
        report = analyse_daily_usage(data, DAY, now=NOW)
        self.assertIsNone(report.total_kwh)
        self.assertFalse(report.complete)

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
        self.assertEqual(report.ev_total_kwh, Decimal("4.338"))
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
        self.assertTrue(report.complete)
        self.assertEqual(report.ev_total_kwh, Decimal("4.338"))

    def test_unresolved_required_boundary_timestamp_still_fails(self):
        for offset, index in ((0, 276), (2, 12)):
            with self.subTest(offset=offset):
                data = days()
                sample = next(s for s in data[offset].samples if s.measurement == "usePower" and s.sample_index == index)
                sample.observed_at = None
                with self.assertRaises(EnergyBalanceValidationError):
                    analyse_daily_usage(data, DAY, now=NOW)

    def test_ambiguous_unused_dst_hour_does_not_reject_the_following_day(self):
        selected = DAY.replace(month=10, day=26)
        data = days(first=selected - timedelta(days=1))
        sample = next(s for s in data[0].samples if s.measurement == "usePower" and s.sample_index == 30)
        sample.observed_at = None
        sample.axis_label = "2026-10-25 02:30"
        report = analyse_daily_usage(data, selected, now=datetime(2026, 10, 28, tzinfo=ZONE))
        self.assertTrue(report.complete)
        self.assertEqual(len(report.consumption), 288)

    def test_live_report_ignores_future_unresolved_labels(self):
        data = days()
        sample = next(s for s in data[1].samples if s.measurement == "usePower" and s.sample_index == 192)
        sample.observed_at = None
        report = analyse_daily_usage(data[:2], DAY, now=datetime(2026, 9, 2, 12, tzinfo=ZONE))
        self.assertFalse(report.complete)
        self.assertEqual(len(report.consumption), 144)


class DailyServiceTests(unittest.TestCase):
    def test_historical_replay_only_reads_existing_repository(self):
        data = days()
        repository = Mock()
        repository.load_day.side_effect = {d.day: d for d in data}.get
        factory = Mock()
        report = DailyUsageService(repository, client_factory=factory, now=lambda: NOW)(DAY)
        self.assertTrue(report.complete)
        self.assertEqual(repository.load_day.call_count, 3)
        factory.assert_not_called()
        repository.save_day.assert_not_called()

    def test_missing_past_day_is_saved_but_today_is_fetched_fresh_and_not_saved(self):
        data = days()
        today = DAY + timedelta(days=1)
        now = datetime.combine(today, datetime.min.time(), ZONE) + timedelta(hours=3)
        repository = Mock()
        repository.load_day.side_effect = lambda d: data[0] if d == data[0].day else None
        client = Mock(config={})
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock()
        factory = Mock(return_value=client)
        with patch("home_energy.usage.daily.parse_energy_balance", side_effect=[data[1], data[2]]) as parse, \
                patch("home_energy.usage.daily.detect_ev_blocks", wraps=detect_ev_blocks) as detect:
            service = DailyUsageService(repository, client_factory=factory, now=lambda: now)
            report = service(DAY)
        detect.assert_called_once()
        self.assertTrue(report.complete)
        repository.save_day.assert_called_once_with(data[1])
        self.assertEqual([c.args[0] for c in client.get_energy_balance.call_args_list], [DAY, today])
        self.assertEqual(parse.call_count, 2)
        repository.load_day.reset_mock()
        repository.load_day.side_effect = lambda d: data[1] if d == DAY else None
        with patch("home_energy.usage.daily.parse_energy_balance", return_value=data[2]):
            live = service()
        self.assertEqual(live.day, today)
        self.assertFalse(live.complete)
        repository.load_day.assert_called_once_with(DAY)
        self.assertEqual(repository.save_day.call_count, 1)

    def test_future_input_fails_before_io_and_configuration_errors_are_typed(self):
        repository, factory = Mock(), Mock()
        service = DailyUsageService(repository, client_factory=factory, now=lambda: NOW)
        with self.assertRaises(DailyUsageRequestError):
            service(NOW.date() + timedelta(days=1))
        repository.load_day.assert_not_called()
        factory.assert_not_called()
        repository.load_day.return_value = None
        factory.side_effect = ValueError("private-secret")
        with self.assertRaises(UsageConfigurationError):
            service(DAY)
        repository.save_day.assert_not_called()

    def test_incomplete_new_past_snapshot_is_not_frozen_and_is_retried(self):
        data = days()
        target = data[1]
        target.metrics = []
        repository = Mock()
        repository.load_day.side_effect = lambda d: None if d == DAY else next(x for x in data if x.day == d)
        client = Mock(config={})
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock()
        service = DailyUsageService(repository, client_factory=lambda: client, now=lambda: NOW)
        with patch("home_energy.usage.daily.parse_energy_balance", return_value=target):
            first = service(DAY)
            self.assertFalse(first.complete)
            repository.save_day.assert_not_called()
            target.metrics.append(DailyMetric("totalUsePower", Decimal("42.750"), "42.750", "kWh"))
            second = service(DAY)
            self.assertTrue(second.complete)
            repository.save_day.assert_called_once_with(target)
        self.assertEqual(client.get_energy_balance.call_count, 2)

    def test_concurrent_insert_uses_committed_snapshot(self):
        data = days()
        committed = data[1]
        fresh = days()[1]
        fresh.metrics[0] = DailyMetric("totalUsePower", Decimal("99"), "99", "kWh")
        repository = Mock()
        repository.load_day.side_effect = [data[0], None, committed, data[2]]
        repository.save_day.return_value = False
        client = Mock(config={})
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock()
        service = DailyUsageService(repository, client_factory=lambda: client, now=lambda: NOW)
        with patch("home_energy.usage.daily.parse_energy_balance", return_value=fresh):
            report = service(DAY)
        self.assertEqual(report.total_kwh, committed.consumed_energy)
        self.assertTrue(report.complete)

    def test_fetched_neighbor_with_invalid_unused_data_does_not_block_ingestion(self):
        data = days()
        sample = next(s for s in data[0].samples if s.measurement == "usePower" and s.sample_index == 144)
        sample.observed_at = None
        repository = Mock()
        repository.load_day.side_effect = lambda d: None if d == data[0].day else next(x for x in data if x.day == d)
        client = Mock(config={})
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock()
        service = DailyUsageService(repository, client_factory=lambda: client, now=lambda: NOW)
        with patch("home_energy.usage.daily.parse_energy_balance", return_value=data[0]):
            report = service(DAY)
        self.assertTrue(report.complete)
        repository.save_day.assert_not_called()


class DailyApiTests(unittest.TestCase):
    def setUp(self):
        report = analyse_daily_usage(days(loads=[("2026-09-02 14:00", "2026-09-02 16:00", 2.4)]), DAY, now=NOW)
        self.service = Mock(return_value=report)
        app = create_app(usage_service=self.service, schema_initializer=Mock(), readiness_probe=Mock())
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_contract_exact_units_times_model_and_daily_total(self):
        response = self.client.post(PATH, json={"date": str(DAY)})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["total_kwh"], "42.750")
        self.assertEqual(body["ev_total_kwh"], "4.338")
        self.assertEqual(body["ev_model"]["minimum_minutes"], 120)
        self.assertEqual(body["ev_model"]["power_kw"], "2.169")
        self.assertEqual(body["ev_model"]["method"], "mean_clean_stop_drop")
        self.assertEqual(body["ev_model"]["version"], "clean-stops-2026-10-10-mean-v2")
        self.assertEqual(body["ev_blocks"][0]["start_at"], "2026-09-02T14:00:00+02:00")
        self.assertEqual(body["consumption"][168]["ev_power_kw"], "2.169")
        self.assertNotIn("baseline", response.text)
        self.service.assert_called_once_with(DAY)
        self.assertEqual(self.client.post(PATH, json={}).status_code, 200)
        self.service.assert_called_with(None)

    def test_bad_input_and_provider_errors_keep_existing_safe_envelope(self):
        for body in ({"date": "9999-01-01"}, {"date": "2026-09-02T00:00:00Z"},
                     {"date": "2026-02-30"}, {"date": str(DAY), "threshold": 1.5}):
            self.assertEqual(self.client.post(PATH, json=body).status_code, 422)
        self.service.assert_not_called()
        for error, status, code in ((FusionSolarError("private-secret"), 502, "provider_unavailable"),
                                    (EnergyBalanceValidationError("private-secret"), 502, "invalid_provider_data"),
                                    (UsageConfigurationError("private-secret"), 500, "configuration_error")):
            with self.subTest(code=code), self.assertLogs("uvicorn.error", level="ERROR") as logs:
                self.service.side_effect = error
                response = self.client.post(PATH, json={"date": str(DAY)})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn("private-secret", response.text + " ".join(logs.output))

    def test_openapi_documents_ingestion_and_nullable_measurements(self):
        document = self.client.get("/openapi.json").json()
        endpoint = document["paths"][PATH]["post"]
        self.assertEqual(endpoint["operationId"], "ingest_daily_usage")
        self.assertEqual(endpoint["responses"]["502"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/ErrorResponse"})
        request = document["components"]["schemas"]["DailyUsageRequest"]
        self.assertNotIn("required", request)
        self.assertFalse(request["additionalProperties"])
