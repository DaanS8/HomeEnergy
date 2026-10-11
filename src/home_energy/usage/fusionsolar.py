"""Replay FusionSolar's authenticated, browser-observed energy-balance request."""

import json
import os
import time
from datetime import date as Date, datetime, time as Time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import dotenv_values

from .auth import AuthenticationError, FusionSolarError, SessionExpiredError, WebAuthentication
from .constants import (
    CAPTURED_DATE, DATE_STR_FORMAT, LOCAL_TIME, PLANT_ID, PLANT_TIMEZONE,
    REQUEST_HEADERS, REQUEST_URL,
)


FIXED_CONFIG_KEYS = (
    "request_url", "captured_date", "date_parameters", "plant_id",
    "plant_timezone", "time_dim", "login_host",
)


def _validate_config(config):
    if any(key in config for key in FIXED_CONFIG_KEYS):
        raise ValueError("Plant and request settings are fixed in usage/constants.py.")
    headers = requests.structures.CaseInsensitiveDict(config.get("headers", {}))
    if any(name in headers and headers[name] != value for name, value in REQUEST_HEADERS.items()):
        raise ValueError("Request headers are fixed in usage/constants.py.")


def load_config(path=None):
    """Load credentials, session settings and verified units; request values are fixed."""
    # No interpolation: passwords containing ${...} must remain literal.
    environment = {key: value for key, value in dotenv_values(
        Path(".env"), interpolate=False, encoding="utf-8-sig").items() if value is not None}
    environment.update(os.environ)
    if any("FUSIONSOLAR_" + key.upper() in environment for key in FIXED_CONFIG_KEYS):
        raise ValueError("Plant and request settings are fixed in usage/constants.py.")
    config = {}
    override_path = path or environment.get("FUSIONSOLAR_CONFIG")
    if override_path:
        config_path = Path(override_path)
        if not config_path.exists():
            raise ValueError(f"Configuration file does not exist: {config_path}")
        overrides = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            raise ValueError("Configuration must be a JSON object.")
        config.update(overrides)
    for key in ("username", "password", "session_file"):
        value = environment.get("FUSIONSOLAR_" + key.upper())
        if value is not None:
            config[key] = value
    headers = requests.structures.CaseInsensitiveDict(config.get("headers", {}))
    if "FUSIONSOLAR_HEADERS" in environment:
        headers.update(json.loads(environment["FUSIONSOLAR_HEADERS"]))
    if "FUSIONSOLAR_ROARAND" in environment:
        headers["Roarand"] = environment["FUSIONSOLAR_ROARAND"]
    if "FUSIONSOLAR_COOKIES" in environment:
        # A raw Cookie header preserves duplicate names found in browser captures.
        config["cookies"] = environment["FUSIONSOLAR_COOKIES"]
        headers.pop("Cookie", None)
    config["headers"] = dict(headers)
    _validate_config(config)
    return config


class FusionSolarClient:
    """Authenticated daily requests for our fixed plant."""

    def __init__(self, config, timeout=30):
        _validate_config(config)
        self.config = config
        self.request_url = REQUEST_URL
        self.url = urlsplit(self.request_url)
        if (self.url.scheme != "https" or not self.url.hostname
                or not self.url.hostname.endswith(".fusionsolar.huawei.com")
                or self.url.path != "/rest/pvms/web/station/v3/overview/energy-balance"
                or self.url.username or self.url.password or self.url.fragment):
            raise ValueError("Set request_url to the captured HTTPS FusionSolar energy-balance URL.")
        self.query = parse_qsl(self.url.query, keep_blank_values=True)
        self.params = dict(self.query)
        required = ("stationDn", "timeDim", "timeZone", "timeZoneStr", "queryTime", "dateStr")
        if any(not self.params.get(key) for key in required):
            raise ValueError("The captured URL is missing required query parameters.")
        if len(self.query) != len(self.params):
            raise ValueError("Duplicate query parameters are unsupported.")
        self.captured_date = Date.fromisoformat(CAPTURED_DATE)
        if timeout <= 0:
            raise ValueError("timeout must be positive.")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers.update(config.get("headers", {}))
        cookies = config.get("cookies")
        if isinstance(cookies, str):
            self.session.headers["Cookie"] = cookies
        elif isinstance(cookies, dict):
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain=self.url.hostname, path="/", secure=True)
        elif cookies is not None:
            raise ValueError("cookies must be a raw Cookie string or a JSON object.")
        self.auth = WebAuthentication(self.session, config, self.url.hostname, self.timeout)
        self.auth.load()
        if not self.auth.enabled and not self.session.headers.get("Roarand"):
            raise ValueError("Supply the browser's Roarand header in config or FUSIONSOLAR_ROARAND.")
        if not self.auth.enabled and not self.session.headers.get("Cookie") and not self.session.cookies:
            raise ValueError("Supply browser cookies in config or FUSIONSOLAR_COOKIES.")

    @classmethod
    def from_config(cls, path=None, **kwargs):
        return cls(load_config(path), **kwargs)

    @property
    def plant_id(self):
        return PLANT_ID

    @property
    def timezone(self):
        return PLANT_TIMEZONE

    def _date_params(self, day):
        instant = datetime.combine(day, Time.fromisoformat(LOCAL_TIME), ZoneInfo(PLANT_TIMEZONE))
        return {
            "queryTime": str(int(instant.timestamp() * 1000)),
            "dateStr": instant.strftime(DATE_STR_FORMAT),
        }

    def request_url_for(self, date):
        """Return the URL used by get_energy_balance, for comparison with the UI."""
        day = Date.fromisoformat(date) if isinstance(date, str) else date
        if not isinstance(day, Date) or isinstance(day, datetime):
            raise ValueError("date must be a datetime.date or YYYY-MM-DD string.")
        changes = {}
        if day != self.captured_date:
            # The recipe must also reproduce the original date before using it.
            baseline = self._date_params(self.captured_date)
            if any(self.params[key] != value for key, value in baseline.items()):
                raise ValueError("Date constants do not reproduce the captured queryTime/dateStr.")
            changes.update(self._date_params(day))
            if "_" in self.params:
                changes["_"] = str(time.time_ns() // 1_000_000)
        if not changes:
            return self.request_url
        query = urlencode([(key, changes.get(key, value)) for key, value in self.query])
        return urlunsplit(self.url._replace(query=query))

    def get_energy_balance(self, date):
        """Fetch one browser-observed date selection and return its untouched JSON object."""
        url = self.request_url_for(date)
        logged_in = False
        if self.auth.enabled and (not self.session.headers.get("Roarand") or
                                 not (self.session.headers.get("Cookie") or self.session.cookies)):
            self.auth.login()
            logged_in = True
        try:
            payload = self._get_energy_balance(url)
        except SessionExpiredError:
            if not self.auth.enabled or logged_in:
                raise
            self.auth.login()
            payload = self._get_energy_balance(url)
        self.auth.save()
        return payload

    def _get_energy_balance(self, url):
        try:
            response = self.session.get(url, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            # requests exceptions can contain the station URL; do not echo it.
            raise FusionSolarError(f"Request failed ({type(exc).__name__}). Check network access and request headers.") from None
        with response:
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise SessionExpiredError(f"HTTP {response.status_code}: session rejected or redirected. Refresh browser cookies and Roarand.")
            try:
                response.raise_for_status()
            except requests.HTTPError:
                raise FusionSolarError(f"HTTP {response.status_code} from the energy-balance endpoint.") from None
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise SessionExpiredError("Received HTML instead of JSON, possibly a login page. Refresh browser cookies and Roarand.")
            try:
                payload = response.json()
            except ValueError:
                raise FusionSolarError("The endpoint returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise FusionSolarError("Expected a JSON object from energy-balance.")
        failed = payload.get("success") in (False, "false", "False")
        fail_code = payload.get("failCode")
        if failed or fail_code not in (None, 0, "0") or payload.get("failReason"):
            reason = payload.get("failReason") or payload.get("message") or "Unspecified API error"
            error_type = FusionSolarError
            if any(word in str(reason).lower() for word in ("login", "log in", "session", "unauthorized", "not authenticated")):
                error_type = SessionExpiredError
            raise error_type(f"API failure (failCode={fail_code}): {str(reason)[:300]}. Refresh browser authentication if the session expired.")
        return payload

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

