"""External-boundary fakes for exercising real ingestion and report services."""

from copy import deepcopy
from io import BytesIO

from PIL import Image

from home_energy.usage.constants import PLANT_ID, PLANT_TIMEZONE


class MemoryRepository:
    """Immutable snapshots; reads return copies, as a SQL round trip would."""

    def __init__(self, days=()):
        self.days = {day.day: deepcopy(day) for day in days}

    def has_day(self, day):
        return day in self.days

    def save_day(self, day):
        if self.has_day(day.day):
            return False
        self.days[day.day] = deepcopy(day)
        return True

    def load_day(self, day):
        return deepcopy(self.days.get(day))

    def load_range(self, start, end):
        return [self.load_day(day) for day in sorted(self.days) if start <= day <= end]


class RecordingProvider:
    """Record paid/network requests and session lifetime, with configurable data."""

    plant_id = PLANT_ID
    timezone = PLANT_TIMEZONE

    def __init__(self, fetch):
        self.fetch = fetch
        self.config = {}
        self.requests = []
        self.opened = 0
        self.closed = 0

    def __enter__(self):
        self.opened += 1
        return self

    def __exit__(self, *args):
        self.closed += 1

    def _fetch(self, day):
        self.requests.append(day)
        return self.fetch(day)

    get_day = _fetch
    get_energy_balance = _fetch


def assert_png(test, data):
    """Decode the whole export and reject truncated or blank images."""
    with Image.open(BytesIO(data)) as image:
        test.assertEqual(image.format, "PNG")
        image.load()
        test.assertGreater(image.width, 0)
        test.assertGreater(image.height, 0)
        test.assertTrue(any(low != high for low, high in image.convert("RGB").getextrema()),
                        "The report image is blank")
