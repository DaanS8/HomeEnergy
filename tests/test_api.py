"""Offline HTTP contracts, startup, failure redaction, and generated documentation."""

import base64
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import psycopg
from fastapi.testclient import TestClient

from home_energy.api.app import create_app
from home_energy.database.connection import DatabaseConfigurationError
from home_energy.filling import FillError
from home_energy.prices.entsoe import (InvalidPriceDataError, PriceAPIError, PriceConfigurationError,
                                      PricesUnavailableError)
from home_energy.prices.report import DayAheadReportService, ReportRequestError
from price_helpers import price_day
from service_helpers import MemoryRepository, assert_png


REPORT_PATH = "/api/v1/reports/day-ahead"
DAY = date(2000, 1, 1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository([price_day(DAY, "-20")])
        self.service = Mock(wraps=DayAheadReportService(self.repository))
        self.probe = Mock()
        self.initialize = Mock()
        self.app = create_app(report_service=self.service, readiness_probe=self.probe,
                              schema_initializer=self.initialize)
        self.initialize.assert_not_called()
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_success_is_a_completed_report_with_exact_threshold_and_plain_base64(self):
        for threshold, expected in ((None, "300"), ("300.000", "300.000")):
            with self.subTest(threshold=threshold):
                request = {"date": str(DAY)}
                if threshold is not None:
                    request["threshold_eur_per_mwh"] = threshold
                response = self.client.post(REPORT_PATH, json=request)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["status"], "success")
                self.assertEqual(body["threshold_eur_per_mwh"], expected)
                assert_png(self, base64.b64decode(body["image"], validate=True))
                self.assertEqual(body["image_mime_type"], "image/png")
                self.assertEqual(body["image_filename"], "day-ahead-2000-01-01.png")
                self.assertIsNone(body["warning"])
                self.assertEqual(body["high_price_ranges"], [])

    def test_warning_is_http_success_and_preserves_brussels_offsets(self):
        day = self.repository.load_day(DAY)
        for index in (17, 18):
            day.intervals[index] = replace(day.intervals[index], price_eur_per_mwh=Decimal("-10.249"))
        self.repository.days[DAY] = day
        response = self.client.post(REPORT_PATH, json={"date": str(DAY), "threshold_eur_per_mwh": "-10.25"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "warning")
        self.assertEqual(response.json()["high_price_ranges"][0]["start_at"], "2000-01-01T17:00:00+01:00")
        self.assertEqual(response.json()["high_price_ranges"][0]["end_at"], "2000-01-01T19:00:00+01:00")
        self.assertEqual(response.json()["threshold_eur_per_mwh"], "-10.25")
        self.assertEqual(response.json()["warning"], "van 17:00 tot 19:00")

    def test_bad_input_never_invokes_the_service_and_has_the_documented_envelope(self):
        bodies = [{}, {"date": "tomorrow"}, {"date": "2000-02-30"}, {"date": "9999-01-01"},
                  {"date": "2000-01-01T00:00:00Z"}, {"date": 946684800},
                  {"date": str(DAY), "typo_threshold": "300"},
                  *[{"date": str(DAY), "threshold_eur_per_mwh": v}
                    for v in ("NaN", "Infinity", "abc", None, True)]]
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.post(REPORT_PATH, json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
                self.assertFalse(response.json()["retryable"])
                self.assertEqual(set(response.json()), {"status", "error", "code", "retryable"})
        self.assertEqual(self.client.post(REPORT_PATH, content="{", headers={"Content-Type": "application/json"}).status_code, 422)
        self.service.assert_not_called()

    def test_typed_failures_and_wrapped_causes_are_mapped_without_leaking_details(self):
        cases = [(PricesUnavailableError("securityToken=private-secret"), 503, "prices_unavailable", True),
                 (PriceAPIError("private-secret"), 502, "provider_unavailable", True),
                 (InvalidPriceDataError("private-secret"), 502, "invalid_provider_data", False),
                 (PriceConfigurationError("private-secret"), 500, "configuration_error", False),
                 (DatabaseConfigurationError("private-secret"), 500, "configuration_error", False),
                 (psycopg.OperationalError("password=private-secret"), 503, "database_unavailable", True),
                 (ReportRequestError("private-secret"), 422, "invalid_request", False),
                 (ValueError("private-secret"), 500, "internal_error", False)]
        for exception, status, code, retryable in cases:
            for wrapped in (False, True):
                with self.subTest(code=code, wrapped=wrapped):
                    self.service.side_effect = FillError(DAY, exception) if wrapped else exception
                    with self.assertLogs("uvicorn.error", level="INFO") as logs:
                        response = self.client.post(REPORT_PATH, json={"date": str(DAY)})
                    self.assertEqual(response.status_code, status)
                    body = response.json()
                    self.assertEqual((body["status"], body["code"], body["retryable"]), ("error", code, retryable))
                    self.assertNotIn("private-secret", response.text + " ".join(logs.output))
                    self.assertNotIn("Traceback", " ".join(logs.output))

    def test_live_and_ready_are_distinct_and_startup_initializes_once(self):
        self.initialize.assert_called_once_with()
        self.assertEqual(self.client.get("/health/live").json(), {"status": "ok"})
        self.probe.assert_not_called()
        self.probe.side_effect = psycopg.OperationalError("private-secret")
        with self.assertLogs("uvicorn.error", level="ERROR"):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "database_unavailable")
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.probe.side_effect = None
        self.assertEqual(self.client.get("/health/ready").status_code, 200)
        self.service.assert_not_called()
        self.initialize.assert_called_once_with()

    def test_documentation_describes_actual_validation_and_error_schemas(self):
        document = self.client.get("/openapi.json").json()
        operation = document["paths"][REPORT_PATH]["post"]
        self.assertEqual(operation["operationId"], "prepare_day_ahead_report")
        for status in (422, 502, 503, 500):
            schema = operation["responses"][str(status)]["content"]["application/json"]["schema"]
            self.assertEqual(schema, {"$ref": "#/components/schemas/ErrorResponse"})
        request_schema = document["components"]["schemas"]["DayAheadReportRequest"]
        self.assertEqual(request_schema["required"], ["date"])
        self.assertFalse(request_schema["additionalProperties"])
        response_schema = document["components"]["schemas"]["DayAheadReportResponse"]
        self.assertEqual(response_schema["properties"]["threshold_eur_per_mwh"]["type"], "string")
        self.assertEqual(response_schema["properties"]["image"]["contentEncoding"], "base64")
        self.assertTrue(response_schema["examples"])
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/redoc").status_code, 200)

    def test_unknown_routes_and_wrong_methods_use_safe_http_errors(self):
        self.assertEqual(self.client.get("/missing").json()["code"], "not_found")
        response = self.client.get(REPORT_PATH)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "method_not_allowed")
        self.assertIn("POST", response.headers["allow"])


class StartupTests(unittest.TestCase):
    def test_startup_fails_safely_and_does_not_contact_providers(self):
        with patch("home_energy.api.app.initialize_schema", side_effect=psycopg.OperationalError("private-secret")), \
                patch("home_energy.prices.ingestion.DayAheadPriceClient") as provider:
            app = create_app()
            provider.from_config.assert_not_called()
            with self.assertLogs("uvicorn.error", level="ERROR") as logs, self.assertRaises(RuntimeError) as error:
                with TestClient(app):
                    self.fail("Startup should fail")
            self.assertNotIn("private-secret", str(error.exception) + " ".join(logs.output))
            provider.from_config.assert_not_called()
