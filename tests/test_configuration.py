"""Check .env precedence and authentication defaults."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from home_energy.usage.constants import PLANT_ID
from home_energy.usage.fusionsolar import FusionSolarClient, load_config
from helpers import URL, config


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
                    with patch.object(requests.Session, "request") as network, \
                            patch("home_energy.usage.fusionsolar.time.time_ns", return_value=1791718559952000000):
                        with FusionSolarClient(loaded) as client:
                            self.assertEqual(client.request_url_for("2026-10-10"), URL)
                            self.assertEqual(client.plant_id, PLANT_ID)
                        network.assert_not_called()
            finally:
                os.chdir(original)


    def test_header_defaults_can_be_overridden_case_insensitively(self):
        settings = config()
        settings["headers"].update({"user-agent": "custom-browser", "x-timezone-offset": "60"})
        with FusionSolarClient(settings) as client:
            self.assertEqual(client.session.headers["User-Agent"], "custom-browser")
            self.assertEqual(client.session.headers["X-Timezone-Offset"], "60")


if __name__ == "__main__":
    unittest.main()
