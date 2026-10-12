"""Small synthetic API fixtures shared by protocol and collection tests."""

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from home_energy.usage.constants import PLANT_ID
from home_energy.usage.energy_balance import parse_energy_balance


# Captured protocol fixture, independent of runtime request construction.
URL = (
    "https://uni004eu5.fusionsolar.huawei.com/rest/pvms/web/station/v3/overview/energy-balance?"
    "stationDn=NE%3D147174083&timeDim=2&timeZone=2.0&timeZoneStr=Europe%2FBrussels"
    "&queryTime=1791583200000&dateStr=2026-10-10%2000%3A00%3A00&_=1791718559952"
)


def config():
    return {"headers": {"roarand": "test-only"}, "cookies": "sid=first; sid=second"}


def response(payload=None, status=200, content_type="application/json", body=None):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = content_type
    result._content = (body if body is not None else json.dumps(payload)).encode()
    result._content_consumed = True
    return result


def energy_payload(day=date(2026, 10, 10)):
    return {"success": True, "failCode": 0, "message": "envelope omitted from storage",
            "data": {"stationDn": PLANT_ID, "stationTimezone": None,
                     "existInverter": True, "sceneType": 1,
                     "xAxis": [f"{day} 00:00 DST", f"{day} 00:17 DST", f"{day} 00:19 DST"],
                     "productPower": ["0.000", "--", None],
                     "usePower": ["1.12345678901234567890123456789", "2", ""],
                     "onGridPower": [], "totalProductPower": "12.650",
                     "selfUsePowerRatioByProduct": "33.333", "totalBusinessChargePower": None}}


def parsed_energy_day(payload=None, day=date(2026, 10, 10), **kwargs):
    return parse_energy_balance(energy_payload(day) if payload is None else payload,
                                plant_id=PLANT_ID, day=day, timezone_name="Europe/Brussels",
                                fetched_at=datetime(2026, 10, 11, tzinfo=timezone.utc), **kwargs)


@contextmanager
def isolated_configuration(contents, environment):
    """Read an actual .env without inheriting workspace secrets or settings."""
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment, clear=True):
        root = Path(directory)
        (root / ".env").write_text(contents, encoding="utf-8-sig")
        try:
            os.chdir(root)
            yield
        finally:
            os.chdir(previous)
