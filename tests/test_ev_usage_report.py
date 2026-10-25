"""Shareable EV reports: real PNGs, exact blocks, no-charge days and date limits."""

import base64
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from fastapi.testclient import TestClient

from home_energy.api.app import create_app
from home_energy.usage.ingestion import UsageConfigurationError
from home_energy.usage.report import EVUsageReportService
from home_energy.usage.ingestion import UsageIngestionService
from home_energy.usage.fusionsolar import FusionSolarError
from service_helpers import MemoryRepository, RecordingProvider, assert_png
from usage_helpers import DAY, NOW, ZONE, complete_energy_payload, complete_source_days as days


PATH = "/api/v1/reports/ev/daily"


class EVUsageReportTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository(days(loads=[("2026-09-02 23:30", "2026-09-03 01:30", 2.4)]))
        self.provider = RecordingProvider(complete_energy_payload)
        ingestion = UsageIngestionService(self.repository, client_factory=lambda: self.provider, now=lambda: NOW)
        self.report_service = EVUsageReportService(ingestion_service=ingestion)
        self.service = Mock(wraps=self.report_service)
        self.client = TestClient(create_app(ev_report_service=self.service, schema_initializer=Mock()))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_completed_report_returns_valid_png_and_clipped_ev_energy(self):
        response = self.client.post(PATH, json={"date": str(DAY)})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["image_filename"], f"ev-usage-{DAY}.png")
        self.assertEqual(body["image_mime_type"], "image/png")
        self.assertEqual(body["total_kwh"], "42.750")
        self.assertEqual(body["total_kwh_source"], "fusionsolar.totalUsePower")
        self.assertEqual(body["ev_total_kwh"], "1.0795")
        self.assertEqual(body["ev_blocks"][0]["end_at"], "2026-09-03T00:00:00+02:00")
        self.assertEqual(body["ev_model"]["minimum_minutes"], 120)
        self.assertEqual(body["ev_model"]["power_kw"], "2.159")
        self.assertEqual(body["ev_model"]["method"], "mean_clean_stop_drop")
        self.assertTrue(body["ev_model"]["version"])
        self.assertEqual(len(body["consumption"]), 288)
        self.assertEqual(body["consumption"][0], {
            "start_at": "2026-09-02T00:00:00+02:00", "end_at": "2026-09-02T00:05:00+02:00",
            "power_kw": "0.2", "ev_power_kw": "0",
        })
        self.assertEqual(body["consumption"][-1]["power_kw"], "2.4")
        self.assertEqual(body["consumption"][-1]["ev_power_kw"], "2.159")
        assert_png(self, base64.b64decode(body["image"], validate=True))
        self.assertEqual(self.provider.requests, [])

    def test_no_ev_charging_still_produces_an_image(self):
        self.repository.days = {day.day: day for day in days()}
        response = self.client.post(PATH, json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["date"], str(DAY))
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["ev_total_kwh"], "0")
        self.assertEqual(body["ev_blocks"], [])
        assert_png(self, base64.b64decode(body["image"], validate=True))

    def test_recent_dates_are_rejected_before_service(self):
        today = datetime.now(ZONE).date()
        for offset in (-1, 0, 1):
            with self.subTest(offset=offset):
                response = self.client.post(PATH, json={"date": str(today + timedelta(days=offset))})
                self.assertEqual(response.status_code, 422)
        self.service.assert_not_called()

    def test_incomplete_data_returns_an_error_and_succeeds_after_provider_correction(self):
        del self.repository.days[DAY]
        payload = complete_energy_payload(DAY)
        del payload["data"]["totalUsePower"]
        self.provider.fetch = lambda day: payload
        with self.assertLogs("uvicorn.error", level="ERROR"):
            response = self.client.post(PATH, json={"date": str(DAY)})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "invalid_provider_data")
        self.assertNotIn("image", response.json())
        self.assertFalse(self.repository.has_day(DAY))
        self.provider.fetch = complete_energy_payload
        corrected = self.client.post(PATH, json={"date": str(DAY)})
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(corrected.json()["total_kwh"], "42.750")
        assert_png(self, base64.b64decode(corrected.json()["image"], validate=True))
        self.assertEqual(self.provider.requests, [DAY, DAY])

    def test_bad_input_and_provider_errors_keep_existing_safe_envelope(self):
        for body in ({"date": "9999-01-01"}, {"date": "2026-09-02T00:00:00Z"},
                     {"date": "2026-02-30"}, {"date": str(DAY), "threshold": 1.5}):
            self.assertEqual(self.client.post(PATH, json=body).status_code, 422)
        self.service.assert_not_called()
        for error, status, code in ((FusionSolarError("private-secret"), 502, "provider_unavailable"),
                                    (UsageConfigurationError("private-secret"), 500, "configuration_error")):
            with self.subTest(code=code), self.assertLogs("uvicorn.error", level="ERROR") as logs:
                self.service.side_effect = error
                response = self.client.post(PATH, json={"date": str(DAY)})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn("private-secret", response.text + " ".join(logs.output))

    def test_openapi_has_one_complete_ev_report_contract(self):
        document = self.client.get("/openapi.json").json()
        self.assertNotIn("/api/v1/usage/daily", document["paths"])
        endpoint = document["paths"][PATH]["post"]
        self.assertEqual(endpoint["operationId"], "prepare_daily_ev_report")
        self.assertEqual(endpoint["responses"]["502"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/ErrorResponse"})
        schemas = document["components"]["schemas"]
        self.assertNotIn("DailyUsageResponse", schemas)
        request = schemas["DailyUsageRequest"]
        self.assertNotIn("required", request)
        self.assertFalse(request["additionalProperties"])
        response = schemas["EVUsageReportResponse"]
        self.assertEqual(response["properties"]["status"]["const"], "complete")
        self.assertEqual(response["properties"]["total_kwh"]["type"], "string")
        self.assertIn("consumption", response["required"])
        for name in ("power_kw", "ev_power_kw"):
            self.assertEqual(schemas["ConsumptionIntervalResponse"]["properties"][name]["type"], "string")

    def test_removed_data_only_endpoint_returns_404_without_service_calls(self):
        response = self.client.post("/api/v1/usage/daily", json={"date": str(DAY)})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.service.assert_not_called()

