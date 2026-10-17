"""Lap boundary detection with explicit reset and data-gap events."""
from dataclasses import dataclass
import math
from typing import Optional

try:
    from .domain_models import TyreSummary
    from .raw_telemetry_reader import DataQuality, TelemetrySnapshot
except ImportError:
    from domain_models import TyreSummary
    from raw_telemetry_reader import DataQuality, TelemetrySnapshot


@dataclass(frozen=True, slots=True)
class LapCompleted:
    lap_number: int
    time_ms: int
    fuel_start: Optional[float]
    fuel_end: Optional[float]
    fuel_delta_net: Optional[float]
    fuel_used: Optional[float]
    is_best_lap: bool
    sectors_ms: tuple[Optional[int], ...]
    timestamp_ns: int
    data_quality: DataQuality
    refuel_detected: Optional[bool]
    tyre_summary: Optional[TyreSummary] = None
    tyre_temperature_c: Optional[tuple[float, float, float, float]] = None
    tyre_pressure_raw: Optional[tuple[float, float, float, float]] = None
    tyre_wear_raw: Optional[tuple[float, float, float, float]] = None
    valid: Optional[bool] = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class LapGapDetected:
    previous_lap: int
    current_lap: int
    missing_laps: int
    timestamp_ns: int
    data_quality: DataQuality = DataQuality.INCOMPLETE


@dataclass(frozen=True, slots=True)
class SessionReset:
    previous_session: Optional[int]
    current_session: Optional[int]
    completed_laps: int
    timestamp_ns: int
    data_quality: DataQuality = DataQuality.SESSION_RESET


@dataclass(frozen=True, slots=True)
class SectorCompleted:
    sector_index: int
    time_ms: Optional[int]
    timestamp_ns: int
    data_quality: DataQuality


LapEvent = LapCompleted | LapGapDetected | SessionReset | SectorCompleted


class LapTracker:
    """Detect laps, counter gaps, refueling and session counter resets.

    ``process`` returns all domain events. ``update`` remains as a small legacy
    adapter for the original terminal loop and returns only a completed lap.
    """

    def __init__(self, refuel_tolerance_l: float = 0.05):
        if refuel_tolerance_l < 0:
            raise ValueError('refuel_tolerance_l não pode ser negativo')
        self.refuel_tolerance_l = refuel_tolerance_l
        self._completed_laps: Optional[int] = None
        self._session: Optional[int] = None
        self._fuel_at_lap_start: Optional[float] = None
        self._previous_fuel: Optional[float] = None
        self._last_sequence: Optional[int] = None
        self._lap_incomplete = False
        self._refuel_during_lap = False
        self._sector_count = 3
        self._sectors: list[Optional[int]] = [None, None, None]
        self._previous_sector: Optional[int] = None

    def process(self, snapshot: TelemetrySnapshot) -> tuple[LapEvent, ...]:
        return self.process_sample(
            completed_laps=snapshot.completed_laps,
            last_lap_time_ms=snapshot.last_lap_time_ms,
            best_lap_time_ms=snapshot.best_lap_time_ms,
            fuel=snapshot.fuel_l,
            session=snapshot.session,
            sequence=snapshot.sequence,
            timestamp_ns=snapshot.monotonic_ns,
            data_quality=snapshot.data_quality,
            current_sector_index=snapshot.current_sector_index,
            last_sector_time_ms=snapshot.last_sector_time_ms,
            sector_count=snapshot.sector_count,
            tyre_temperature_c=snapshot.tyre_temperature_c,
            tyre_pressure_raw=snapshot.tyre_pressure,
            tyre_wear_raw=snapshot.tyre_wear_raw,
        )

    def process_sample(
        self,
        *,
        completed_laps: int,
        last_lap_time_ms: int,
        best_lap_time_ms: int,
        fuel: Optional[float],
        session: Optional[int] = None,
        sequence: Optional[int] = None,
        timestamp_ns: int = 0,
        data_quality: DataQuality = DataQuality.OK,
        current_sector_index: int = 0,
        last_sector_time_ms: int = 0,
        sector_count: int = 3,
        tyre_temperature_c=None,
        tyre_pressure_raw=None,
        tyre_wear_raw=None,
    ) -> tuple[LapEvent, ...]:
        fuel = float(fuel) if fuel is not None and math.isfinite(fuel) else None
        sector_count = max(1, min(int(sector_count or 3), 8))
        if self._completed_laps is None:
            self._rebase(completed_laps, fuel, session, sequence, sector_count)
            self._lap_incomplete = fuel is None or data_quality != DataQuality.OK
            self._capture_sector(current_sector_index, last_sector_time_ms,
                                 timestamp_ns, data_quality)
            return ()

        events: list[LapEvent] = []
        reset = (
            completed_laps < self._completed_laps
            or (session is not None and self._session is not None
                and session != self._session)
        )
        if reset:
            events.append(SessionReset(
                previous_session=self._session,
                current_session=session,
                completed_laps=completed_laps,
                timestamp_ns=timestamp_ns,
            ))
            self._rebase(completed_laps, fuel, session, sequence, sector_count)
            self._lap_incomplete = fuel is None or data_quality != DataQuality.OK
            self._capture_sector(current_sector_index, last_sector_time_ms,
                                 timestamp_ns, data_quality)
            return tuple(events)

        if (sequence is not None and self._last_sequence is not None
                and sequence > self._last_sequence + 1):
            self._lap_incomplete = True
        if data_quality != DataQuality.OK:
            self._lap_incomplete = True
        if fuel is None:
            self._lap_incomplete = True
        if (fuel is not None and self._previous_fuel is not None
                and fuel > self._previous_fuel + self.refuel_tolerance_l):
            self._refuel_during_lap = True

        # A changed sector index means lastSectorTime belongs to the sector we
        # just left. On the lap wrap this captures the final sector as well.
        sector_event = self._capture_sector(
            current_sector_index, last_sector_time_ms, timestamp_ns, data_quality
        )
        if sector_event is not None:
            events.append(sector_event)

        if completed_laps > self._completed_laps:
            advance = completed_laps - self._completed_laps
            quality = (
                DataQuality.INCOMPLETE
                if advance > 1 or self._lap_incomplete or last_lap_time_ms <= 0
                else data_quality
            )
            if advance > 1:
                events.append(LapGapDetected(
                    previous_lap=self._completed_laps,
                    current_lap=completed_laps,
                    missing_laps=advance - 1,
                    timestamp_ns=timestamp_ns,
                ))
            fuel_start = None if advance > 1 else self._fuel_at_lap_start
            fuel_end = fuel
            fuel_delta = (
                fuel_start - fuel_end
                if fuel_start is not None and fuel_end is not None
                else None
            )
            sectors = tuple(self._sectors)
            if advance > 1:
                sectors = tuple(None for _ in range(sector_count))
            events.append(LapCompleted(
                lap_number=completed_laps,
                time_ms=int(last_lap_time_ms),
                fuel_start=fuel_start,
                fuel_end=fuel_end,
                fuel_delta_net=fuel_delta,
                fuel_used=(
                    fuel_delta
                    if fuel_delta is not None and not self._refuel_during_lap
                    and quality == DataQuality.OK
                    else None
                ),
                is_best_lap=(last_lap_time_ms > 0
                             and best_lap_time_ms > 0
                             and last_lap_time_ms == best_lap_time_ms),
                sectors_ms=sectors,
                timestamp_ns=timestamp_ns,
                data_quality=quality,
                refuel_detected=(
                    True if self._refuel_during_lap
                    else False if quality == DataQuality.OK else None
                ),
                tyre_temperature_c=tyre_temperature_c,
                tyre_pressure_raw=tyre_pressure_raw,
                tyre_wear_raw=tyre_wear_raw,
            ))
            self._completed_laps = completed_laps
            self._fuel_at_lap_start = fuel
            # This boundary becomes the new baseline. A detected gap marks the
            # lap just emitted incomplete, not all subsequent laps.
            self._lap_incomplete = data_quality != DataQuality.OK
            self._refuel_during_lap = False
            self._sectors = [None] * sector_count

        self._session = session if session is not None else self._session
        self._last_sequence = sequence if sequence is not None else self._last_sequence
        self._previous_fuel = fuel
        return tuple(events)

    def update(
        self,
        *,
        completed_laps: int,
        last_lap_time_ms: int,
        best_lap_time_ms: int,
        fuel: float,
    ) -> Optional[LapCompleted]:
        events = self.process_sample(
            completed_laps=completed_laps,
            last_lap_time_ms=last_lap_time_ms,
            best_lap_time_ms=best_lap_time_ms,
            fuel=fuel,
        )
        return next((e for e in events if isinstance(e, LapCompleted)), None)

    def _capture_sector(self, current_sector: int, last_sector_time_ms: int,
                        timestamp_ns: int, data_quality: DataQuality):
        current_sector = int(current_sector)
        completed = None
        if self._previous_sector is not None and current_sector != self._previous_sector:
            left_sector = self._previous_sector
            if (0 <= left_sector < len(self._sectors)
                    and last_sector_time_ms > 0):
                self._sectors[left_sector] = int(last_sector_time_ms)
                completed = SectorCompleted(
                    sector_index=left_sector,
                    time_ms=int(last_sector_time_ms),
                    timestamp_ns=timestamp_ns,
                    data_quality=(
                        DataQuality.INCOMPLETE
                        if self._lap_incomplete or data_quality != DataQuality.OK
                        else DataQuality.OK
                    ),
                )
        self._previous_sector = current_sector
        return completed

    def _rebase(
        self,
        completed_laps: int,
        fuel: Optional[float],
        session: Optional[int],
        sequence: Optional[int],
        sector_count: int,
    ) -> None:
        self._completed_laps = completed_laps
        self._session = session
        self._fuel_at_lap_start = fuel
        self._previous_fuel = fuel
        self._last_sequence = sequence
        self._lap_incomplete = False
        self._refuel_during_lap = False
        self._sector_count = sector_count
        self._sectors = [None] * sector_count
        self._previous_sector = None


def format_lap_time(time_ms: int) -> str:
    """Format milliseconds as MM:SS.mmm."""
    minutes, remainder = divmod(max(0, time_ms), 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def format_lap_completed(event: LapCompleted) -> str:
    """Keep the original terminal output available to legacy ``main.py``."""
    fuel_line = (
        f"Combustível consumido: {event.fuel_used:.2f} L"
        if event.fuel_used is not None
        else "Combustível consumido: desconhecido"
    )
    return "\n".join((
        f"Volta {event.lap_number} concluída",
        f"Tempo: {format_lap_time(event.time_ms)}",
        fuel_line,
        f"Melhor volta: {'sim' if event.is_best_lap else 'não'}",
    ))
