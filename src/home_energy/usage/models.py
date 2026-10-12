"""Database-independent objects for daily energy-balance analysis."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class UsageInterval:
    start_at: datetime
    end_at: datetime
    power_kw: Decimal
    ev_power_kw: Decimal


@dataclass(frozen=True)
class EVBlock:
    start_at: datetime
    end_at: datetime
    power_kw: Decimal
    energy_kwh: Decimal


@dataclass(frozen=True)
class DailyUsage:
    day: date
    fetched_at: datetime
    total_kwh: Decimal
    ev_total_kwh: Decimal
    consumption: tuple[UsageInterval, ...]
    ev_blocks: tuple[EVBlock, ...]


@dataclass
class MeasurementSample:
    sample_index: int
    axis_label: str
    observed_at: datetime | None
    measurement: str
    value: Decimal | None
    original_value: str | None
    unit: str | None = None


@dataclass
class DailyMetric:
    name: str
    value: Decimal | None
    original_value: str | None
    unit: str | None = None


@dataclass
class EnergyBalanceDay:
    plant_id: str
    day: date
    timezone: str
    fetched_at: datetime
    sample_count: int
    available_series: list[str]
    empty_series: list[str]
    samples: list[MeasurementSample]
    metrics: list[DailyMetric] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def consumed_energy(self) -> Decimal | None:
        """API daily consumption total (totalUsePower), in its stored unit."""
        return next((metric.value for metric in self.metrics if metric.name == "totalUsePower"), None)

    def validate(self) -> None:
        """Prevent a partial or inconsistent object from becoming a stored day."""
        if not self.plant_id or not isinstance(self.day, date) or isinstance(self.day, datetime):
            raise ValueError("A day requires a plant identifier and datetime.date.")
        ZoneInfo(self.timezone)
        if self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware.")
        if self.sample_count < 1 or not self.available_series:
            raise ValueError("A day requires labels and at least one populated series.")
        names = self.available_series + self.empty_series
        if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
            raise ValueError("Series names must be nonempty and unique.")
        expected = self.sample_count * len(self.available_series)
        if len(self.samples) != expected:
            raise ValueError("Every populated series must contain every sample index.")
        keys, labels = set(), {}
        for sample in self.samples:
            key = (sample.sample_index, sample.measurement)
            if (sample.measurement not in self.available_series or
                    not 0 <= sample.sample_index < self.sample_count or key in keys):
                raise ValueError("Sample indices and measurement names must form a complete unique grid.")
            if not isinstance(sample.axis_label, str) or not sample.axis_label.strip():
                raise ValueError("Axis labels must be nonempty strings.")
            label = (sample.axis_label, sample.observed_at)
            if labels.setdefault(sample.sample_index, label) != label:
                raise ValueError("Series must share the same labels and timestamps at each index.")
            if sample.observed_at is not None and sample.observed_at.utcoffset() is None:
                raise ValueError("Resolved sample timestamps must be timezone-aware.")
            keys.add(key)
        metric_names = [metric.name for metric in self.metrics]
        if any(not name for name in metric_names) or len(metric_names) != len(set(metric_names)):
            raise ValueError("Daily metric names must be nonempty and unique.")
        for reading in [*self.samples, *self.metrics]:
            if reading.value is not None and (not isinstance(reading.value, Decimal) or not reading.value.is_finite()):
                raise ValueError("Numeric readings must be finite Decimals.")
            if reading.original_value is not None and not isinstance(reading.original_value, str):
                raise ValueError("Original readings must be text or null.")
            missing = reading.original_value in (None, "", "--")
            if missing != (reading.value is None):
                raise ValueError("Missing readings must remain distinct from numeric readings.")
            if not missing:
                try:
                    matches = Decimal(reading.original_value) == reading.value
                except ArithmeticError:
                    matches = False
                if not matches:
                    raise ValueError("Numeric and original readings must agree.")
            if reading.unit is not None and (not isinstance(reading.unit, str) or not reading.unit.strip()):
                raise ValueError("Verified units must be nonempty text or null.")
