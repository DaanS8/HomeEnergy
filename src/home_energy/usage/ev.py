"""Fixed EV calibration and pure uninterrupted-region detection."""

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .models import EVBlock


# Equal-weight mean of accepted clean-stop drops, rounded to 0.001 kW.
# Frozen calibration; see docs/ev-estimation.md.
EV_POWER_KW = Decimal("2.169")
MINIMUM_EV_DURATION = timedelta(hours=2)
SAMPLE_INTERVAL = timedelta(minutes=5)
MODEL_VERSION = "clean-stops-2026-10-10-mean-v2"


def find_regions(readings: Iterable[tuple[datetime, Decimal | float | None]], *,
                 threshold_kw: Decimal | float = EV_POWER_KW,
                 sample_interval: timedelta = SAMPLE_INTERVAL,
                 minimum_duration: timedelta = MINIMUM_EV_DURATION) -> list[tuple[datetime, datetime]]:
    """Find above-threshold runs in ordered readings; nulls and gaps break runs.

    Each reading covers the following sampling interval. Returned ends are
    exclusive UTC instants.
    This same rule serves production ingestion and the investigation.
    """
    regions = []
    run_start = previous = None

    def finish(end):
        if run_start is not None and end - run_start >= minimum_duration:
            regions.append((run_start, end))

    for stamp, power in readings:
        if stamp.utcoffset() is None:
            raise ValueError("Region timestamps must be timezone-aware.")
        stamp = stamp.astimezone(timezone.utc)
        if previous is not None and stamp <= previous:
            raise ValueError("Region readings must be ordered and unique.")
        if previous is not None and stamp != previous + sample_interval:
            finish(previous + sample_interval)
            run_start = None
        if power is not None and power > threshold_kw:
            if run_start is None:
                run_start = stamp
        else:
            finish(stamp)
            run_start = None
        previous = stamp
    if previous is not None:
        finish(previous + sample_interval)
    return regions


def detect_ev_blocks(readings: Sequence[tuple[datetime, Decimal]], start: datetime, end: datetime) -> tuple[EVBlock, ...]:
    """Qualify regions and clip their energy to the requested local day."""
    runs = find_regions(readings)
    blocks = []
    for begin, finish in runs:
        if begin >= end or finish <= start:
            continue
        begin, finish = max(begin, start), min(finish, end)
        energy = EV_POWER_KW * Decimal(int((finish - begin).total_seconds())) / Decimal(3600)
        blocks.append(EVBlock(begin, finish, EV_POWER_KW, energy))
    return tuple(blocks)
