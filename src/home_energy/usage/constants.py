"""Fixed values for our FusionSolar plant and verified daily request."""

from types import MappingProxyType
from urllib.parse import quote, urlencode

PLANT_ID = "NE=147174083"
PLANT_TIMEZONE = "Europe/Brussels"
LOGIN_HOST = "eu5.fusionsolar.huawei.com"
TIME_DIM = 2
CAPTURED_DATE = "2026-10-10"
LOCAL_TIME = "00:00:00"
DATE_STR_FORMAT = "%Y-%m-%d %H:%M:%S"

# Preserve the captured parameter order and offset values.
REQUEST_URL = (
    "https://uni004eu5.fusionsolar.huawei.com/rest/pvms/web/station/v3/overview/energy-balance?"
    + urlencode({
        "stationDn": PLANT_ID,
        "timeDim": TIME_DIM,
        "timeZone": "2.0",
        "timeZoneStr": PLANT_TIMEZONE,
        "queryTime": 1791583200000,
        "dateStr": f"{CAPTURED_DATE} {LOCAL_TIME}",
        "_": 1791718559952,
    }, quote_via=quote)
)

REQUEST_HEADERS = MappingProxyType({
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "X-Non-Renewal-Session": "true",
    "X-Timezone-Offset": "120",
})
