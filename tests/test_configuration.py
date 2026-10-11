"""Check .env precedence and authentication defaults."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from home_energy.usage.constants import CAPTURED_DATE, PLANT_ID, REQUEST_URL
from home_energy.usage.fusionsolar import FusionSolarClient, load_config
from helpers import config


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_credentials_are_literal_and_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "FUSIONSOLAR_USERNAME=file-user\nFUSIONSOLAR_PASSWORD='literal ${PASSWORD} # + / Ã©'\n",
                encoding="utf-8-sig")
            settings = config()
            settings.update(username="json-user", password="json-password")
            (root / "fusionsolar.local.json").write_text(json.dumps(settings))
            original = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {"FUSIONSOLAR_USERNAME": "environment-user"}, clear=True):
                    loaded = load_config(root / "fusionsolar.local.json")
                    self.assertEqual(loaded["username"], "environment-user")
                    self.assertEqual(loaded["password"], "literal ${PASSWORD} # + / Ã©")
                    self.assertNotIn("FUSIONSOLAR_PASSWORD", os.environ)
                    with FusionSolarClient(loaded) as client:
                        self.assertEqual(client.auth.cache_path, Path(".cache/fusionsolar-session.json"))
                        self.assertTrue(client.session.headers["User-Agent"].startswith("Mozilla/5.0"))
            finally:
                os.chdir(original)

    def test_default_startup_needs_only_dotenv_and_does_not_read_legacy_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("FUSIONSOLAR_USERNAME=test-user\nFUSIONSOLAR_PASSWORD=test-password\n")
            # Leftover root JSON must not silently supply old credentials/settings.
            (root / "fusionsolar.local.json").write_text("invalid legacy config")
            original = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {}, clear=True):
                    loaded = load_config()
                    self.assertNotIn("request_url", loaded)
                    self.assertEqual(loaded["username"], "test-user")
                    with patch.object(requests.Session, "request") as network:
                        with FusionSolarClient(loaded) as client:
                            self.assertEqual(client.request_url_for(CAPTURED_DATE), REQUEST_URL)
                            self.assertEqual(client.plant_id, PLANT_ID)
                        network.assert_not_called()
            finally:
                os.chdir(original)


    def test_fixed_values_cannot_be_overridden_by_environment_json_or_constructor(self):
        for key, value in [("request_url", "private-url"), ("captured_date", "2000-01-01"),
                           ("date_parameters", {"verified": False}), ("plant_id", "OTHER"),
                           ("login_host", "another.fusionsolar.huawei.com")]:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "fixed in usage/constants.py") as error:
                    FusionSolarClient({**config(), key: value})
                self.assertNotIn(str(value), str(error.exception))
                with patch.dict(os.environ, {"FUSIONSOLAR_" + key.upper(): str(value)}, clear=True), \
                        patch("home_energy.usage.fusionsolar.dotenv_values", return_value={}):
                    with self.assertRaisesRegex(ValueError, "fixed in usage/constants.py"):
                        load_config()
                with tempfile.TemporaryDirectory() as directory:
                    filename = Path(directory) / "config.json"
                    filename.write_text(json.dumps({key: value}))
                    with patch.dict(os.environ, {}, clear=True), \
                            patch("home_energy.usage.fusionsolar.dotenv_values", return_value={}):
                        with self.assertRaisesRegex(ValueError, "fixed in usage/constants.py"):
                            load_config(filename)

    def test_fixed_headers_cannot_be_overridden_case_insensitively(self):
        with self.assertRaisesRegex(ValueError, "Request headers are fixed"):
            FusionSolarClient({"headers": {"x-timezone-offset": "0"}})


if __name__ == "__main__":
    unittest.main()
