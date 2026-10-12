"""Database connection configuration and credential handling."""

import os
import unittest
from unittest.mock import patch
from home_energy.database import connect
from home_energy.database.connection import DatabaseConfigurationError
from helpers import isolated_configuration


SETTINGS = {
    "PGHOST": "localhost", "PGPORT": "55432", "PGDATABASE": "home_energy_dev",
    "PGUSER": "home_energy_dev", "PGPASSWORD": "test-only",
}


class ConfigurationTests(unittest.TestCase):
    def test_process_overrides_file_without_mutating_environment_or_password(self):
        settings = {**SETTINGS, "PGPASSWORD": "literal ${SECRET} # / +"}
        contents = "\n".join(f"{key}='{value}'" for key, value in settings.items())
        environment = {"PGHOST": "db", "PGPORT": "5432", "SECRET": "must-not-be-expanded"}
        with isolated_configuration(contents, environment):
            with patch("home_energy.database.connection.psycopg.connect") as open_connection:
                connect()
                open_connection.assert_called_once_with(
                    host="db", port=5432, dbname="home_energy_dev",
                    user="home_energy_dev", password=settings["PGPASSWORD"], connect_timeout=5,
                )
            self.assertEqual(dict(os.environ), environment)

    def test_missing_and_invalid_settings_fail_before_connecting_without_values(self):
        for key in SETTINGS:
            settings = {**SETTINGS, key: ""}
            with self.subTest(key=key), patch.dict(os.environ, {}, clear=True):
                with patch("home_energy.database.connection.dotenv_values", return_value=settings):
                    with patch("home_energy.database.connection.psycopg.connect") as open_connection:
                        with self.assertRaises(DatabaseConfigurationError) as error:
                            connect()
                        self.assertIn(key, str(error.exception))
                        self.assertNotIn(SETTINGS["PGPASSWORD"], str(error.exception))
                        open_connection.assert_not_called()
        for invalid in ("secret-port", "0", "65536"):
            with self.subTest(port=invalid), patch.dict(os.environ, {}, clear=True):
                with patch("home_energy.database.connection.dotenv_values", return_value={**SETTINGS, "PGPORT": invalid}):
                    with self.assertRaises(DatabaseConfigurationError) as error:
                        connect()
                    self.assertNotIn(invalid, str(error.exception))

    def test_blank_process_value_does_not_fall_back_to_file(self):
        with patch("home_energy.database.connection.dotenv_values", return_value=SETTINGS.copy()):
            with patch.dict(os.environ, {"PGPASSWORD": ""}, clear=True):
                with self.assertRaisesRegex(DatabaseConfigurationError, "PGPASSWORD"):
                    connect()

