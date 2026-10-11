"""Offline checks for request fidelity, API failures, and lossless responses."""

import os
import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from home_energy.usage.fusionsolar import FusionSolarClient, FusionSolarError, SessionExpiredError, load_config
from helpers import URL, config, response


class ClientTests(unittest.TestCase):
    def setUp(self):
        dotenv = patch("home_energy.usage.fusionsolar.dotenv_values", return_value={})
        dotenv.start()
        self.addCleanup(dotenv.stop)

    def test_exact_replay_and_raw_cookie_preservation(self):
        payload = {"success": True, "failCode": 0, "data": {"productPower": ["--", "0.000"]}}
        with FusionSolarClient(config()) as client:
            prepared = client.session.prepare_request(requests.Request("GET", client.request_url_for(date(2026, 10, 10))))
            self.assertEqual(prepared.url, URL)
            self.assertEqual(prepared.headers["Cookie"], "sid=first; sid=second")
            self.assertEqual(prepared.headers["Roarand"], "test-only")
            with patch.object(client.session, "get", return_value=response(payload)) as get:
                self.assertEqual(client.get_energy_balance("2026-10-10"), payload)
                get.assert_called_once_with(URL, timeout=30, allow_redirects=False)

    def test_fixed_recipe_reproduces_second_capture(self):
        with FusionSolarClient(config()) as client, patch("home_energy.usage.fusionsolar.time.time_ns", return_value=1791719451904000000):
            params = parse_qs(urlsplit(client.request_url_for("2026-10-09")).query)
            self.assertEqual(params["queryTime"], ["1791496800000"])
            self.assertEqual(params["dateStr"], ["2026-10-09 00:00:00"])
            self.assertEqual(params["timeZone"], ["2.0"])
            self.assertEqual(params["_"], ["1791719451904"])

    def test_wrong_recipe_rejected(self):
        with patch("home_energy.usage.fusionsolar.LOCAL_TIME", "01:00:00"), \
                FusionSolarClient(config()) as client, self.assertRaisesRegex(ValueError, "do not reproduce"):
            client.get_energy_balance("2026-10-09")

    def test_dst_timestamp_conversion_preserves_uninterpreted_timezone_parameter(self):
        with FusionSolarClient(config()) as client:
            before = parse_qs(urlsplit(client.request_url_for("2026-10-25")).query)
            after = parse_qs(urlsplit(client.request_url_for("2026-10-26")).query)
            self.assertEqual(int(after["queryTime"][0]) - int(before["queryTime"][0]), 25 * 3600000)
            self.assertEqual(after["timeZone"], ["2.0"])

    def test_http_application_and_session_errors(self):
        cases = [
            (response(status=401), SessionExpiredError),
            (response(status=403), SessionExpiredError),
            (response(status=302), SessionExpiredError),
            (response(status=500), FusionSolarError),
            (response(body="<html>login</html>", content_type="text/html"), SessionExpiredError),
            (response(body="invalid"), FusionSolarError),
            (response({"success": False, "failCode": 9, "message": "Session expired"}), SessionExpiredError),
            (response({"success": False, "failReason": "No permission"}), FusionSolarError),
            (response({"success": True, "failCode": 42}), FusionSolarError),
        ]
        with FusionSolarClient(config()) as client:
            for result, expected in cases:
                with self.subTest(status=result.status_code, body=result.text), patch.object(client.session, "get", return_value=result):
                    with self.assertRaises(expected):
                        client.get_energy_balance("2026-10-10")
            with patch.object(client.session, "get", side_effect=requests.Timeout("secret URL")):
                with self.assertRaises(FusionSolarError) as error:
                    client.get_energy_balance("2026-10-10")
                self.assertNotIn("secret", str(error.exception))

    def test_environment_only_authentication(self):
        environment = {"FUSIONSOLAR_COOKIES": "sid=env", "FUSIONSOLAR_ROARAND": "env-token"}
        with patch.dict(os.environ, environment, clear=True):
            with FusionSolarClient(load_config()) as client:
                self.assertEqual(client.session.headers["Cookie"], "sid=env")
                self.assertEqual(client.session.headers["Roarand"], "env-token")

