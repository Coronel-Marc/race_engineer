"""Immutable derived-state value objects shared by reducers and consumers."""
from dataclasses import dataclass
from typing import Optional

try:
    from .raw_telemetry_reader import DataQuality
    from .raw_telemetry_reader import TelemetrySnapshot
except ImportError:
    from raw_telemetry_reader import DataQuality
    from raw_telemetry_reader import TelemetrySnapshot


@dataclass(frozen=True, slots=True)
class MetricSummary:
    average: Optional[float]
    sample_sum: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]
    count: int


@dataclass(frozen=True, slots=True)
class TyreSummary:
    temperature_c: tuple[MetricSummary, MetricSummary, MetricSummary, MetricSummary]
    pressure_raw: tuple[MetricSummary, MetricSummary, MetricSummary, MetricSummary]
    wear_raw: tuple[MetricSummary, MetricSummary, MetricSummary, MetricSummary]


@dataclass(frozen=True, slots=True)
class DerivedTelemetry:
    timestamp_ns: int
    data_quality: DataQuality
    temperature_rate_c_per_s: tuple[Optional[float], ...]
    pressure_rate_per_s: tuple[Optional[float], ...]
    smoothed_temperature_c: tuple[Optional[float], ...]
    smoothed_pressure_raw: tuple[Optional[float], ...]
    smoothed_wear_raw: tuple[Optional[float], ...]


@dataclass(frozen=True, slots=True)
class ReducerState:
    session_id: Optional[str]
    latest_snapshot: Optional[TelemetrySnapshot]
    derived: Optional[DerivedTelemetry]
    best_lap_time_ms: Optional[int]
    best_sector_times_ms: tuple[Optional[int], ...]
    last_lap_delta_ms: Optional[int]
    tyre_summary: TyreSummary
    data_quality: DataQuality

