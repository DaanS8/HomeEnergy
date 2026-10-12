"""Shared plant settings and FusionSolar request defaults."""

PLANT_ID = "NE=147174083"
PLANT_TIMEZONE = "Europe/Brussels"
LOGIN_HOST = "eu5.fusionsolar.huawei.com"
PORTAL_HOST = "uni004eu5.fusionsolar.huawei.com"
REQUEST_URL = f"https://{PORTAL_HOST}/rest/pvms/web/station/v3/overview/energy-balance"
TIME_DIM = 2
DATE_STR_FORMAT = "%Y-%m-%d %H:%M:%S"

# Browser-observed provider fields; their winter interpretation is unverified.
TIME_ZONE = "2.0"

REQUEST_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "X-Non-Renewal-Session": "true",
    "X-Timezone-Offset": "120",
}
