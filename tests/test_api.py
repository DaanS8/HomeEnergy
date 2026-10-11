"""Offline HTTP contracts, startup, failure redaction, and generated documentation."""

import base64
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import psycopg
from fastapi.testclient import TestClient

from home_energy.api.app import check_database, create_app
from home_energy.database.connection import DatabaseConfigurationError
from home_energy.filling import FillError
from home_energy.prices.analysis import PriceRange
from home_energy.prices.entsoe import (InvalidPriceDataError, PriceAPIError, PriceConfigurationError,
                                      PricesUnavailableError)
from home_energy.prices.report import PriceReport, ReportRequestError


REPORT_PATH = "/api/v1/reports/day-ahead"
DAY = date(2000, 1, 1)
PNG = b"\x89PNG\r\n\x1a\nexample"


def report():
    return PriceReport(DAY, "BE", "Europe/Brussels", Decimal("300.000"),
                       datetime(2000, 1, 1, tzinfo=timezone.utc), (), None, PNG)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock(return_value=report())
        self.probe = Mock()
        self.initialize = Mock()
        self.app = create_app(report_service=self.service, readiness_probe=self.probe,
                              schema_initializer=self.initialize)
        self.initialize.assert_not_called()
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_success_is_a_completed_report_with_exact_threshold_and_plain_base64(self):
        response = self.client.post(REPORT_PATH, json={"date": str(DAY)})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["threshold_eur_per_mwh"], "300.000")
        self.assertEqual(base64.b64decode(body["image"], validate=True), PNG)
        self.assertEqual(body["image_mime_type"], "image/png")
        self.assertEqual(body["image_filename"], "day-ahead-2000-01-01.png")
        self.assertIsNone(body["warning"])
        self.assertEqual(body["high_price_ranges"], [])
        self.service.assert_called_once_with(DAY, Decimal("300"))

    def test_warning_is_http_success_and_preserves_brussels_offsets(self):
        ranges = (PriceRange(datetime(2000, 1, 1, 16, tzinfo=timezone.utc),
                             datetime(2000, 1, 1, 18, tzinfo=timezone.utc)),)
        self.service.return_value = replace(report(), high_price_ranges=ranges,
                                            warning="van 17:00 tot 19:00")
        response = self.client.post(REPORT_PATH, json={"date": str(DAY), "threshold_eur_per_mwh": "-10.25"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "warning")
        self.assertEqual(response.json()["high_price_ranges"][0]["start_at"], "2000-01-01T17:00:00+01:00")
        self.service.assert_called_once_with(DAY, Decimal("-10.25"))

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
                patch("home_energy.prices.repository.DayAheadPriceClient") as provider:
            app = create_app()
            provider.from_config.assert_not_called()
            with self.assertLogs("uvicorn.error", level="ERROR") as logs, self.assertRaises(RuntimeError) as error:
                with TestClient(app):
                    self.fail("Startup should fail")
            self.assertNotIn("private-secret", str(error.exception) + " ".join(logs.output))
            self.assertTrue(error.exception.__suppress_context__)
            provider.from_config.assert_not_called()

    def test_readiness_checks_application_tables_with_a_bounded_statement(self):
        with patch("home_energy.api.app.connect") as connect:
            check_database()
            query = connect.return_value.__enter__.return_value.execute.call_args_list
            self.assertIn("statement_timeout", query[0].args[0])
            for table in ("energy_balance_days", "measurement_samples", "daily_metrics", "day_ahead_price_days", "price_intervals"):
                self.assertIn("home_energy." + table, query[1].args[0])
