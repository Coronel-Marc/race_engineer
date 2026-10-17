"""Single-owner in-memory reduction of immutable telemetry snapshots."""
from collections import deque
from dataclasses import dataclass, replace
import math
import statistics
import uuid
from typing import Optional

try:
    from .alert_engine import AlertEngine, AlertEvent
    from .domain_models import (
        DerivedTelemetry, MetricSummary, ReducerState, TyreSummary,
    )
    from .lap_tracker import (
        LapCompleted, LapEvent, LapTracker, SectorCompleted, SessionReset,
    )
    from .raw_telemetry_reader import AC_LIVE, DataQuality, TelemetrySnapshot
except ImportError:
    from alert_engine import AlertEngine, AlertEvent
    from domain_models import DerivedTelemetry, MetricSummary, ReducerState, TyreSummary
    from lap_tracker import LapCompleted, LapEvent, LapTracker, SectorCompleted, SessionReset
    from raw_telemetry_reader import AC_LIVE, DataQuality, TelemetrySnapshot


@dataclass(frozen=True, slots=True)
class TimedSectorValue:
    timestamp_ns: int
    sector_index: int
    time_ms: Optional[int]
    data_quality: DataQuality


@dataclass(frozen=True, slots=True)
class ReducerOutput:
    state: ReducerState
    lap_events: tuple[LapEvent, ...]
    alert_events: tuple[AlertEvent, ...]
    accepted: bool


class _FourWheelAccumulator:
    """Time-weighted means with raw-unit extrema and sample counts."""

    def __init__(self):
        self.weighted_sum = [0.0] * 4
        self.weight_seconds = [0.0] * 4
        self.sample_sum = [0.0] * 4
        self.minimum = [None] * 4
        self.maximum = [None] * 4
        self.count = [0] * 4

    def add(self, current, previous, dt_seconds):
        for i, value in enumerate(current):
            if value is None or not math.isfinite(value):
                continue
            self.count[i] += 1
            self.sample_sum[i] += value
            self.minimum[i] = value if self.minimum[i] is None else min(self.minimum[i], value)
            self.maximum[i] = value if self.maximum[i] is None else max(self.maximum[i], value)
            previous_value = previous[i] if previous is not None else None
            if (previous_value is not None and dt_seconds > 0
                    and math.isfinite(previous_value)):
                self.weighted_sum[i] += (previous_value + value) * 0.5 * dt_seconds
                self.weight_seconds[i] += dt_seconds

    def summary(self):
        return tuple(
            MetricSummary(
                average=(self.weighted_sum[i] / self.weight_seconds[i]
                         if self.weight_seconds[i] > 0 else None),
                sample_sum=(self.sample_sum[i] if self.count[i] else None),
                minimum=self.minimum[i], maximum=self.maximum[i], count=self.count[i],
            )
            for i in range(4)
        )


class _TyreAccumulator:
    def __init__(self):
        self.temperature = _FourWheelAccumulator()
        self.pressure = _FourWheelAccumulator()
        self.wear = _FourWheelAccumulator()

    def add(self, current, previous, dt_seconds):
        self.temperature.add(
            current.tyre_temperature_c,
            previous.tyre_temperature_c if previous else None,
            dt_seconds,
        )
        self.pressure.add(
            current.tyre_pressure,
            previous.tyre_pressure if previous else None,
            dt_seconds,
        )
        self.wear.add(
            current.tyre_wear_raw,
            previous.tyre_wear_raw if previous else None,
            dt_seconds,
        )

    def summary(self):
        return TyreSummary(
            temperature_c=self.temperature.summary(),
            pressure_raw=self.pressure.summary(),
            wear_raw=self.wear.summary(),
        )


class StateReducer:
    """Own all volatile state; call ``update`` from exactly one consumer thread.

    Tire averages are trapezoidal time-weighted means. Samples in pit, pit lane,
    pause, replay, or with non-OK quality are excluded. Wear is kept in the
    simulator's raw scale; no percentage normalization is assumed.
    """

    def __init__(
        self,
        *,
        history_size: int = 512,
        trend_size: int = 5,
        max_derivative_gap_seconds: float = 2.0,
        alert_engine: Optional[AlertEngine] = None,
    ):
        if history_size < 1 or trend_size < 1:
            raise ValueError('os limites de histórico devem ser positivos')
        if max_derivative_gap_seconds <= 0:
            raise ValueError('max_derivative_gap_seconds deve ser positivo')
        self.max_gap_ns = int(max_derivative_gap_seconds * 1_000_000_000)
        if self.max_gap_ns < 1:
            raise ValueError('max_derivative_gap_seconds é pequeno demais')
        self._snapshot_history = deque(maxlen=history_size)
        self._sector_history = deque(maxlen=history_size)
        self._lap_history = deque(maxlen=history_size)
        self._trend_history = deque(maxlen=history_size)
        self._temperature_windows = [deque(maxlen=trend_size) for _ in range(4)]
        self._pressure_windows = [deque(maxlen=trend_size) for _ in range(4)]
        self._wear_windows = [deque(maxlen=trend_size) for _ in range(4)]
        self._lap_tracker = LapTracker()
        # Ownership of an injected engine is transferred to the reducer.
        self._alert_engine = alert_engine if alert_engine is not None else AlertEngine()
        self._session_id = None
        self._identity = None
        self._previous_snapshot = None
        self._last_sequence = None
        self._best_lap_time_ms = None
        self._best_sector_times_ms = []
        self._last_lap_delta_ms = None
        self._session_tyre_stats = _TyreAccumulator()
        self._lap_tyre_stats = _TyreAccumulator()
        self._state = ReducerState(
            session_id=None,
            latest_snapshot=None,
            derived=None,
            best_lap_time_ms=None,
            best_sector_times_ms=(),
            last_lap_delta_ms=None,
            tyre_summary=self._session_tyre_stats.summary(),
            data_quality=DataQuality.RECONNECTING,
        )

    @property
    def state(self) -> ReducerState:
        return self._state

    @property
    def snapshot_history(self):
        return tuple(self._snapshot_history)

    @property
    def sector_history(self):
        return tuple(self._sector_history)

    @property
    def lap_history(self):
        return tuple(self._lap_history)

    @property
    def trend_history(self):
        return tuple(self._trend_history)

    def update(self, snapshot: TelemetrySnapshot) -> ReducerOutput:
        """Reduce one snapshot; duplicate sequence numbers are ignored."""
        session_reset_events = []
        identity = (snapshot.session, snapshot.track, snapshot.car_model)
        sequence_reset = (
            self._last_sequence is not None and snapshot.sequence < self._last_sequence
        )
        identity_changed = self._identity is not None and identity != self._identity
        lap_counter_reset = (
            self._previous_snapshot is not None
            and snapshot.completed_laps < self._previous_snapshot.completed_laps
        )

        if self._last_sequence is not None and snapshot.sequence == self._last_sequence:
            return ReducerOutput(self._state, (), (), False)

        previous_session = self._previous_snapshot.session if self._previous_snapshot else None
        if identity_changed or sequence_reset or lap_counter_reset:
            self._reset_session(identity)
            session_reset_events.append(SessionReset(
                previous_session=previous_session,
                current_session=snapshot.session,
                completed_laps=snapshot.completed_laps,
                timestamp_ns=snapshot.monotonic_ns,
            ))

        if self._session_id is None:
            self._reset_session(identity)

        previous = self._previous_snapshot
        dt_ns = snapshot.monotonic_ns - previous.monotonic_ns if previous else 0
        data_quality = snapshot.data_quality
        if previous and (dt_ns <= 0 or dt_ns > self.max_gap_ns):
            data_quality = DataQuality.INCOMPLETE
        if previous and snapshot.sequence <= previous.sequence:
            data_quality = DataQuality.INCOMPLETE

        effective_snapshot = (
            snapshot if data_quality == snapshot.data_quality
            else replace(snapshot, data_quality=data_quality)
        )
        derived = self._derive(effective_snapshot, previous, dt_ns, data_quality)
        self._snapshot_history.append(effective_snapshot)

        eligible = self._eligible_for_tyre_stats(effective_snapshot, data_quality)
        previous_eligible = (
            previous if previous and self._eligible_for_tyre_stats(previous, previous.data_quality)
            and 0 < dt_ns <= self.max_gap_ns else None
        )
        if eligible:
            dt_seconds = dt_ns / 1_000_000_000 if previous_eligible else 0.0
            self._session_tyre_stats.add(effective_snapshot, previous_eligible, dt_seconds)
            self._lap_tyre_stats.add(effective_snapshot, previous_eligible, dt_seconds)

        tracker_events = self._lap_tracker.process(effective_snapshot)
        lap_events: list[LapEvent] = session_reset_events + list(tracker_events)
        for event in tracker_events:
            if isinstance(event, LapCompleted):
                tyre_summary = (
                    self._lap_tyre_stats.summary()
                    if event.data_quality == DataQuality.OK else None
                )
                event = replace(event, tyre_summary=tyre_summary)
                self._lap_history.append(event)
                if event.data_quality == DataQuality.OK and event.time_ms > 0:
                    if self._best_lap_time_ms is not None:
                        self._last_lap_delta_ms = event.time_ms - self._best_lap_time_ms
                    else:
                        self._last_lap_delta_ms = None
                    if self._best_lap_time_ms is None or event.time_ms < self._best_lap_time_ms:
                        self._best_lap_time_ms = event.time_ms
                if len(self._best_sector_times_ms) < len(event.sectors_ms):
                    self._best_sector_times_ms.extend(
                        [None] * (len(event.sectors_ms) - len(self._best_sector_times_ms))
                    )
                if event.data_quality == DataQuality.OK:
                    for index, sector_ms in enumerate(event.sectors_ms):
                        if sector_ms and (self._best_sector_times_ms[index] is None
                                          or sector_ms < self._best_sector_times_ms[index]):
                            self._best_sector_times_ms[index] = sector_ms
                self._lap_tyre_stats = _TyreAccumulator()
            elif isinstance(event, SessionReset):
                # Counter reset detected by LapTracker; replace accumulated
                # per-session state but retain this current sample below.
                self._reset_session(identity, reset_tracker=False)
                self._lap_tracker.process(snapshot)
            elif isinstance(event, SectorCompleted):
                self._sector_history.append(event)
        # Rebuild lap event tuple after any immutable replacement above.
        for index, event in enumerate(lap_events):
            if isinstance(event, LapCompleted):
                stored = next((lap for lap in reversed(self._lap_history)
                               if lap.timestamp_ns == event.timestamp_ns
                               and lap.lap_number == event.lap_number), None)
                if stored is not None:
                    lap_events[index] = stored

        self._previous_snapshot = effective_snapshot
        self._last_sequence = snapshot.sequence
        self._identity = identity
        self._state = ReducerState(
            session_id=self._session_id,
            latest_snapshot=effective_snapshot,
            derived=derived,
            best_lap_time_ms=self._best_lap_time_ms,
            best_sector_times_ms=tuple(self._best_sector_times_ms),
            last_lap_delta_ms=self._last_lap_delta_ms,
            tyre_summary=self._session_tyre_stats.summary(),
            data_quality=data_quality,
        )
        alerts = self._alert_engine.evaluate(
            effective_snapshot,
            session_id=self._session_id,
            smoothed_temperature_c=derived.smoothed_temperature_c,
            smoothed_pressure_raw=derived.smoothed_pressure_raw,
            smoothed_wear_raw=derived.smoothed_wear_raw,
        )
        return ReducerOutput(self._state, tuple(lap_events), alerts, True)

    def _reset_session(self, identity, reset_tracker=True):
        self._session_id = uuid.uuid4().hex
        self._identity = identity
        self._snapshot_history.clear()
        self._sector_history.clear()
        self._lap_history.clear()
        self._trend_history.clear()
        for windows in (self._temperature_windows, self._pressure_windows, self._wear_windows):
            for window in windows:
                window.clear()
        if reset_tracker:
            self._lap_tracker = LapTracker()
        self._best_lap_time_ms = None
        self._best_sector_times_ms = []
        self._last_lap_delta_ms = None
        self._session_tyre_stats = _TyreAccumulator()
        self._lap_tyre_stats = _TyreAccumulator()
        self._previous_snapshot = None
        self._alert_engine.reset()

    def _derive(self, snapshot, previous, dt_ns, quality):
        fresh = self._eligible_for_tyre_stats(snapshot, quality)
        dt_seconds = dt_ns / 1_000_000_000 if dt_ns > 0 else 0.0
        temperature_rates = []
        pressure_rates = []
        smooth_temperature = []
        smooth_pressure = []
        smooth_wear = []
        for i in range(4):
            temp = self._finite(snapshot.tyre_temperature_c[i])
            pressure = self._finite(snapshot.tyre_pressure[i])
            wear = self._finite(snapshot.tyre_wear_raw[i])
            temp_rate = pressure_rate = None
            if (fresh and previous
                    and self._eligible_for_tyre_stats(previous, previous.data_quality)
                    and 0 < dt_ns <= self.max_gap_ns):
                prev_temp = self._finite(previous.tyre_temperature_c[i])
                prev_pressure = self._finite(previous.tyre_pressure[i])
                if temp is not None and prev_temp is not None:
                    temp_rate = (temp - prev_temp) / dt_seconds
                if pressure is not None and prev_pressure is not None:
                    pressure_rate = (pressure - prev_pressure) / dt_seconds
            temperature_rates.append(temp_rate)
            pressure_rates.append(pressure_rate)
            if fresh:
                if temp is not None:
                    self._temperature_windows[i].append(temp)
                if pressure is not None:
                    self._pressure_windows[i].append(pressure)
                if wear is not None:
                    self._wear_windows[i].append(wear)
            smooth_temperature.append(
                statistics.median(self._temperature_windows[i])
                if self._temperature_windows[i] else None
            )
            smooth_pressure.append(
                statistics.median(self._pressure_windows[i])
                if self._pressure_windows[i] else None
            )
            smooth_wear.append(
                statistics.median(self._wear_windows[i])
                if self._wear_windows[i] else None
            )
        result = DerivedTelemetry(
            timestamp_ns=snapshot.monotonic_ns,
            data_quality=quality,
            temperature_rate_c_per_s=tuple(temperature_rates),
            pressure_rate_per_s=tuple(pressure_rates),
            smoothed_temperature_c=tuple(smooth_temperature),
            smoothed_pressure_raw=tuple(smooth_pressure),
            smoothed_wear_raw=tuple(smooth_wear),
        )
        self._trend_history.append(result)
        return result

    @staticmethod
    def _finite(value):
        return float(value) if value is not None and math.isfinite(value) else None

    @staticmethod
    def _eligible_for_tyre_stats(snapshot, quality):
        return (
            quality == DataQuality.OK
            and snapshot.status == AC_LIVE
            and not snapshot.is_in_pit
            and not snapshot.is_in_pit_lane
        )
