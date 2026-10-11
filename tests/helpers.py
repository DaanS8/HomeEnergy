"""Small synthetic API fixtures shared by protocol and collection tests."""

import json
from datetime import date

import requests

from home_energy.usage.constants import PLANT_ID, REQUEST_URL


URL = REQUEST_URL


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
