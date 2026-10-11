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
    exclusive UTC instants. A zero minimum includes short runs for live status.
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


def detect_ev_blocks(readings: Sequence[tuple[datetime, Decimal | None]], start: datetime, end: datetime,
                     *, open_end: bool) -> tuple[tuple[EVBlock, ...], datetime | None]:
    """Qualify regions, clip to the local day, and identify a short live tail."""
    runs = find_regions(readings, minimum_duration=timedelta(0))
    blocks = []
    for begin, finish in runs:
        if finish - begin < MINIMUM_EV_DURATION or begin >= end or finish <= start:
            continue
        begin, finish = max(begin, start), min(finish, end)
        energy = EV_POWER_KW * Decimal(int((finish - begin).total_seconds())) / Decimal(3600)
        blocks.append(EVBlock(begin, finish, EV_POWER_KW, energy))
    pending = None
    if open_end and runs and runs[-1][1] == readings[-1][0] + SAMPLE_INTERVAL:
        begin, finish = runs[-1]
        if finish - begin < MINIMUM_EV_DURATION:
            pending = begin
    return tuple(blocks), pending
