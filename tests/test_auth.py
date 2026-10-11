"""Offline protocol, credential redaction and session lifecycle checks."""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from home_energy.usage.auth import AuthenticationError, encrypt_password
from home_energy.usage.fusionsolar import FusionSolarClient, FusionSolarError, SessionExpiredError, load_config
from helpers import config, response


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        dotenv = patch("home_energy.usage.fusionsolar.dotenv_values", return_value={})
        dotenv.start()
        self.addCleanup(dotenv.stop)

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.key_data = {"enableEncrypt": True, "version": "00002201", "timeStamp": 123,
                        "pubKey": cls.key.public_key().public_bytes(
                            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()}

    def settings(self, directory):
        settings = config()
        settings.update({"username": "test-user", "password": "secret-test-password",
                         "session_file": str(Path(directory) / "session.local.json")})
        settings.pop("cookies")
        settings["headers"].pop("roarand")
        return settings

    def login_responses(self, client):
        def request(method, url, **kwargs):
            if url.endswith("/unisso/login.action"):
                return response(body="login", content_type="text/html")
            if url.endswith("/unisso/pubkey"):
                return response(self.key_data)
            if url.endswith("/unisso/v3/validateUser.action"):
                self.assertEqual(method, "POST")
                self.assertEqual(kwargs["params"]["timeStamp"], 123)
                self.assertRegex(kwargs["params"]["nonce"], r"^[0-9a-f]{32}$")
                self.assertNotEqual(kwargs["json"]["password"], "secret-test-password")
                self.assertIsNone(kwargs["headers"].get("X-Non-Renewal-Session"))
                return response({"errorCode": "470", "errorMsg": None,
                                 "respMultiRegionName": ["region", "/session/start"]})
            if url.endswith("/session/start"):
                redirect = response(status=302)
                redirect.headers["Location"] = "https://uni004eu5.fusionsolar.huawei.com/session/final"
                return redirect
            if url.endswith("/session/final"):
                client.session.cookies.set("dp-session", "new-session", domain="uni004eu5.fusionsolar.huawei.com", path="/", secure=True)
                return response(body="home", content_type="text/html")
            if url.endswith("/rest/dpcloud/auth/v1/keep-alive"):
                return response({"code": 0, "payload": "new-roarand"})
            self.fail("Unexpected authentication endpoint")
        return request

    def test_encryption_roundtrip_special_characters_and_exact_chunk_multiple(self):
        for password in ("a/b +%?Ã©'!()*", "x" * 270, "x" * 271):
            with self.subTest(length=len(password)):
                encrypted = encrypt_password(password, self.key_data)
                self.assertTrue(encrypted.endswith("00002201"))
                # 3072-bit RSA ciphertext occupies 512 Base64 characters per chunk.
                data = encrypted[:-8]
                chunks = [data[i:i + 512] for i in range(0, len(data), 520)]
                decoded = b"".join(self.key.decrypt(base64.b64decode(chunk), padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA384()), algorithm=hashes.SHA384(), label=None)) for chunk in chunks)
                self.assertEqual(decoded.decode(), quote(password, safe="~!*'()-._"))
        self.assertNotEqual(encrypt_password("password", self.key_data), encrypt_password("password", self.key_data))
        with self.assertRaises(AuthenticationError):
            encrypt_password("password", {"enableEncrypt": False})

    def test_fresh_login_cache_reuse_and_no_password_in_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            with FusionSolarClient(settings) as client:
                client.session.headers["X-Non-Renewal-Session"] = "true"
                with patch.object(client.session, "request", side_effect=self.login_responses(client)), patch.object(client.session, "get", return_value=response({"data": {}})):
                    self.assertEqual(client.get_energy_balance("2026-10-10"), {"data": {}})
            cache = Path(settings["session_file"]).read_text()
            self.assertNotIn(settings["password"], cache)
            self.assertNotIn('"password"', cache)
            with FusionSolarClient(settings) as client, patch.object(client.auth, "login") as login:
                self.assertEqual(client.session.cookies.get("dp-session"), "new-session")
                self.assertEqual(client.session.headers["Roarand"], "new-roarand")
                self.assertNotIn("Cookie", client.session.headers)
                with patch.object(client.session, "get", return_value=response({"data": "cached"})):
                    self.assertEqual(client.get_energy_balance("2026-10-10"), {"data": "cached"})
                login.assert_not_called()

    def test_expired_capture_reauthenticates_once_and_removes_raw_header(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            settings.update({"cookies": "sid=old", "headers": {"Roarand": "old"}})
            with FusionSolarClient(settings) as client:
                with patch.object(client.session, "request", side_effect=self.login_responses(client)) as auth_requests, patch.object(client.session, "get", side_effect=[response(status=401), response({"data": "new"})]) as get:
                    self.assertEqual(client.get_energy_balance("2026-10-10"), {"data": "new"})
                    self.assertEqual(get.call_count, 2)
                    self.assertEqual(sum(c.args[0] == "POST" for c in auth_requests.call_args_list), 1)
                self.assertNotIn("Cookie", client.session.headers)

    def test_retry_is_bounded_and_generic_errors_do_not_trigger_login(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            settings.update({"cookies": "sid=old", "headers": {"Roarand": "old"}})
            with FusionSolarClient(settings) as client:
                with patch.object(client.auth, "login") as login, patch.object(client.session, "get", return_value=response(status=401)) as get:
                    with self.assertRaises(SessionExpiredError):
                        client.get_energy_balance("2026-10-10")
                    self.assertEqual(get.call_count, 2)
                    login.assert_called_once()
                with patch.object(client.auth, "login") as login, patch.object(client.session, "get", return_value=response(status=500)):
                    with self.assertRaises(FusionSolarError):
                        client.get_energy_balance("2026-10-10")
                    login.assert_not_called()

    def test_failed_first_request_after_fresh_login_does_not_login_again(self):
        with tempfile.TemporaryDirectory() as directory, FusionSolarClient(self.settings(directory)) as client:
            with patch.object(client.session, "request", side_effect=self.login_responses(client)) as auth_requests, patch.object(client.session, "get", return_value=response(status=403)) as get:
                with self.assertRaises(SessionExpiredError):
                    client.get_energy_balance("2026-10-10")
                get.assert_called_once()
                self.assertEqual(sum(c.args[0] == "POST" for c in auth_requests.call_args_list), 1)
            self.assertFalse(Path(client.auth.cache_path).exists())

    def test_corrupt_or_wrong_account_cache_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            cache = Path(settings["session_file"])
            cache.write_text("invalid")
            with FusionSolarClient(settings) as client:
                self.assertFalse(client.session.cookies)
            settings.update({"cookies": "sid=old", "headers": {"Roarand": "old"}})
            with FusionSolarClient(settings) as client:
                client.auth.save()
            settings["username"] = "different-user"
            settings.pop("cookies")
            settings["headers"] = {}
            with FusionSolarClient(settings) as client:
                self.assertNotIn("Roarand", client.session.headers)
                self.assertFalse(client.session.cookies)

    def test_cookie_domain_path_and_expiry_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            with FusionSolarClient(settings) as client:
                client.session.headers["Roarand"] = "token"
                client.session.cookies.set("sid", "root", domain=".fusionsolar.huawei.com", path="/", secure=True)
                client.session.cookies.set("sid", "nested", domain="uni004eu5.fusionsolar.huawei.com", path="/rest", secure=True, expires=4102444800)
                client.session.cookies.set("expired", "discard", domain="uni004eu5.fusionsolar.huawei.com", expires=1)
                client.auth.save()
            with FusionSolarClient(settings) as client:
                values = {(c.name, c.domain, c.path, c.expires) for c in client.session.cookies}
                self.assertEqual(values, {("sid", ".fusionsolar.huawei.com", "/", None),
                                          ("sid", "uni004eu5.fusionsolar.huawei.com", "/rest", 4102444800)})

    def test_captcha_mfa_and_login_errors_redact_server_message(self):
        failures = [({"verifyCodeCreate": True}, "CAPTCHA"),
                    ({"twoFactorStatus": 1}, "two-factor"),
                    ({"errorMsg": "secret-test-password and ciphertext"}, "rejected")]
        with tempfile.TemporaryDirectory() as directory:
            for failure, expected in failures:
                with self.subTest(expected=expected), FusionSolarClient(self.settings(directory)) as client:
                    with patch.object(client.session, "request", side_effect=[response(), response(self.key_data), response(failure)]):
                        with self.assertRaisesRegex(AuthenticationError, expected) as error:
                            client.get_energy_balance("2026-10-10")
                        self.assertNotIn("secret-test-password", str(error.exception))

    def test_external_redirect_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory, FusionSolarClient(self.settings(directory)) as client:
            redirect = response(status=302)
            redirect.headers["Location"] = "https://example.com/"
            with patch.object(client.session, "request", return_value=redirect) as request:
                with self.assertRaisesRegex(AuthenticationError, "unexpected redirect"):
                    client.auth.login()
                request.assert_called_once()

    def test_login_network_error_does_not_echo_credentials_or_save_cache(self):
        with tempfile.TemporaryDirectory() as directory, FusionSolarClient(self.settings(directory)) as client:
            with patch.object(client.session, "request", side_effect=requests.Timeout("secret-test-password")):
                with self.assertRaises(AuthenticationError) as error:
                    client.get_energy_balance("2026-10-10")
                self.assertNotIn("secret-test-password", str(error.exception))
            self.assertFalse(client.auth.cache_path.exists())

    def test_environment_credentials_and_request_preparation_have_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = config()
            settings.pop("cookies")
            settings["headers"] = {}
            filename = Path(directory) / "config.local.json"
            filename.write_text(json.dumps(settings))
            environment = {"FUSIONSOLAR_USERNAME": "test-user", "FUSIONSOLAR_PASSWORD": "secret-test-password",
                           "FUSIONSOLAR_SESSION_FILE": str(Path(directory) / "session.local.json")}
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(load_config(filename)["username"], "test-user")
                with patch.object(requests.Session, "request") as network:
                    with FusionSolarClient.from_config(filename) as client:
                        url = client.request_url_for("2026-10-10")
                    network.assert_not_called()
                    self.assertNotIn("secret-test-password", url)


if __name__ == "__main__":
    unittest.main()
