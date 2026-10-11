"""FusionSolar web login and local session persistence (not the Northbound API).

Protocol references: Huawei's /unisso/react/loginAPI.js and
https://github.com/jgriss/FusionSolarPy/blob/main/src/fusion_solar_py/client.py
"""

import base64
import json
import os
import secrets
import tempfile
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .constants import LOGIN_HOST


class FusionSolarError(RuntimeError):
    """An HTTP, transport, response-schema, or application-level failure."""


class SessionExpiredError(FusionSolarError):
    """The session was rejected or appears to have expired."""


class AuthenticationError(FusionSolarError):
    """Login failed or requires interactive verification."""


def encrypt_password(password, key_data):
    """Match encodeURIComponent, RSA-OAEP/SHA-384, chunking and version suffix."""
    try:
        if key_data.get("enableEncrypt") is not True:
            raise ValueError("Password encryption is not enabled.")
        key = serialization.load_pem_public_key(key_data["pubKey"].encode("ascii"))
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("Expected an RSA public key.")
        encoded = quote(password, safe="~!*'()-._").encode("ascii")
        chunks = [base64.b64encode(key.encrypt(encoded[i:i + 270], padding.OAEP(
            mgf=padding.MGF1(hashes.SHA384()), algorithm=hashes.SHA384(), label=None,
        ))).decode("ascii") for i in range(0, len(encoded), 270)]
        return "00000001".join(chunks) + key_data["version"]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AuthenticationError("Cannot encrypt the password using Huawei's public-key response.") from None


def validate_auth_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise AuthenticationError("Huawei login returned an invalid redirect destination.") from None
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".fusionsolar.huawei.com")
            or parsed.username or parsed.password or port not in (None, 443)):
        raise AuthenticationError("Huawei login returned an unexpected redirect destination.")
    return url


class WebAuthentication:
    def __init__(self, session, config, portal_host, timeout):
        self.session = session
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        if not isinstance(self.username, str) or not isinstance(self.password, str):
            raise ValueError("username and password must be strings.")
        if bool(self.username) != bool(self.password):
            raise ValueError("Supply both username and password in local config or FUSIONSOLAR_USERNAME/FUSIONSOLAR_PASSWORD.")
        self.enabled = bool(self.username and self.password)
        self.portal_host = portal_host
        self.login_base = validate_auth_url("https://" + LOGIN_HOST)
        cache = config.get("session_file", ".cache/fusionsolar-session.json")
        self.cache_path = Path(cache) if cache else None
        self.timeout = timeout

    def load(self):
        """Malformed, expired, or other-account caches are disposable."""
        if not self.enabled or not self.cache_path:
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if (data["version"] != 1 or data["username"] != self.username
                    or data["portal_host"] != self.portal_host or data["login_base"] != self.login_base):
                return
            cookies = requests.cookies.RequestsCookieJar()
            for attributes in data["cookies"]:
                cookie = Cookie(**attributes)
                if not cookie.is_expired():
                    cookies.set_cookie(cookie)
            token = data["roarand"]
            raw = data.get("raw_cookie")
            if not isinstance(token, str) or not token or (raw is not None and not isinstance(raw, str)):
                return
            if not raw and not cookies:
                return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return
        self.session.cookies = cookies
        self.session.headers.pop("Cookie", None)
        if raw:
            self.session.headers["Cookie"] = raw
        self.session.headers["Roarand"] = token

    def save(self):
        if not self.enabled or not self.cache_path:
            return
        names = ("version", "name", "value", "port", "port_specified", "domain",
                 "domain_specified", "domain_initial_dot", "path", "path_specified",
                 "secure", "expires", "discard", "comment", "comment_url", "rfc2109")
        cookies = []
        for cookie in self.session.cookies:
            attributes = {name: getattr(cookie, name) for name in names}
            attributes["rest"] = dict(cookie._rest)
            cookies.append(attributes)
        data = {"version": 1, "username": self.username, "portal_host": self.portal_host,
                "login_base": self.login_base, "roarand": self.session.headers.get("Roarand"),
                "raw_cookie": self.session.headers.get("Cookie"), "cookies": cookies}
        temporary = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replacement avoids leaving a partial cache if the process stops.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_path.parent,
                                             prefix=".fusionsolar-session-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream)
                stream.write("\n")
            os.chmod(temporary, 0o600)
            temporary.replace(self.cache_path)
        except OSError:
            raise FusionSolarError("Could not save the local FusionSolar session cache.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _request(self, method, url, **kwargs):
        # Captured data-request headers must not override login cookies or prevent renewal.
        headers = {name: None for name in self.session.headers if name.lower() != "user-agent"}
        headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        for _ in range(10):
            validate_auth_url(url)
            try:
                response = self.session.request(method, url, headers=headers, timeout=self.timeout,
                                                allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise AuthenticationError("Network request failed during FusionSolar authentication.") from None
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                response.close()
                if method != "GET" or not location:
                    raise AuthenticationError("Unexpected redirect during FusionSolar login.")
                url = urljoin(url, location)
                continue
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise AuthenticationError(f"HTTP {status} during FusionSolar authentication.")
            return response
        raise AuthenticationError("Too many redirects during FusionSolar login.")

    def _json(self, method, url, **kwargs):
        with self._request(method, url, **kwargs) as response:
            try:
                data = response.json()
            except ValueError:
                raise AuthenticationError("Expected JSON during FusionSolar authentication.") from None
        if not isinstance(data, dict):
            raise AuthenticationError("Unexpected FusionSolar authentication response.")
        return data

    def login(self):
        """Start a fresh session; no credential, ciphertext or token is logged."""
        self.session.headers.pop("Cookie", None)
        self.session.headers.pop("Roarand", None)
        self.session.cookies.clear()
        with self._request("GET", self.login_base + "/unisso/login.action"):
            pass
        key = self._json("GET", self.login_base + "/unisso/pubkey")
        password = encrypt_password(self.password, key)
        if "timeStamp" not in key:
            raise AuthenticationError("Huawei's public-key response has no timestamp.")
        data = self._json("POST", self.login_base + "/unisso/v3/validateUser.action",
                          params={"timeStamp": key["timeStamp"], "nonce": secrets.token_hex(16)},
                          json={"username": self.username, "password": password,
                                "organizationName": "", "multiRegionName": "", "verifycode": ""})
        # Do not echo server messages: they can contain credential material.
        message = str(data.get("errorMsg", "")).lower()
        if data.get("verifyCodeCreate") or any(word in message for word in ("captcha", "verification code")):
            raise AuthenticationError("Huawei requires CAPTCHA verification. Log in through the browser and supply fresh cookies/Roarand.")
        if data.get("twoFactorStatus") or data.get("status") == "needTwoFactor":
            raise AuthenticationError("Huawei requires two-factor verification. Log in through the browser and supply fresh cookies/Roarand.")
        if data.get("errorMsg"):
            raise AuthenticationError("FusionSolar login was rejected. Check your username and password.")
        destination = data.get("redirectURL")
        if str(data.get("errorCode")) == "470":
            regions = data.get("respMultiRegionName")
            if not destination and isinstance(regions, list) and len(regions) > 1:
                destination = regions[1]
        if not isinstance(destination, str) or not destination:
            raise AuthenticationError("FusionSolar login did not return a supported session redirect.")
        with self._request("GET", urljoin(self.login_base, destination)):
            pass
        alive = self._json("GET", "https://" + self.portal_host + "/rest/dpcloud/auth/v1/keep-alive")
        token = alive.get("payload")
        if alive.get("code") != 0 or not isinstance(token, str) or not token or not self.session.cookies:
            raise AuthenticationError("FusionSolar login did not establish a session. Check login_host and the captured portal hostname.")
        self.session.headers["Roarand"] = token
