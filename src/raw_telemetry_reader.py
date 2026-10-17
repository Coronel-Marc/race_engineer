"""Owned, copy-only reader for Assetto Corsa's named shared-memory pages."""
from dataclasses import dataclass
from enum import Enum
import ctypes
import mmap
import time
from typing import Callable, Optional

try:  # Support both ``import raw_telemetry_reader`` and ``import src...``.
    from .ac_shared_memory import (
        AC_LIVE, AC_OFF, AC_PAUSE, SPageFilePhysics, SPageFileGraphic,
        SPageFileStatic,
    )
except ImportError:
    from ac_shared_memory import (
        AC_LIVE, AC_OFF, AC_PAUSE, SPageFilePhysics, SPageFileGraphic,
        SPageFileStatic,
    )


class DataQuality(str, Enum):
    OK = 'OK'
    STALE = 'STALE'
    INCOMPLETE = 'INCOMPLETE'
    SESSION_RESET = 'SESSION_RESET'
    RECONNECTING = 'RECONNECTING'


@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    """Immutable values copied from one validated read of the shared pages.

    Tire tuples always use front-left, front-right, rear-left, rear-right.
    Pressure and wear preserve the simulator's raw scales.
    """

    sequence: int
    monotonic_ns: int
    physics_packet_id: int
    graphics_packet_id: int
    session: int
    status: int
    completed_laps: int
    current_lap_time_ms: int
    last_lap_time_ms: int
    best_lap_time_ms: int
    current_sector_index: int
    last_sector_time_ms: int
    sector_count: int
    is_in_pit: bool
    is_in_pit_lane: bool
    flag: int
    car_damage_raw: tuple[float, float, float, float, float]
    speed_kmh: float
    gear: int
    rpm: int
    fuel_l: float
    tyre_temperature_c: tuple[float, float, float, float]
    tyre_pressure: tuple[float, float, float, float]
    tyre_wear_raw: tuple[float, float, float, float]
    # No normalized wear scale is published by the shared-memory contract.
    tyre_wear_pct: tuple[Optional[float], Optional[float], Optional[float], Optional[float]]
    track: str
    car_model: str
    tyre_compound: str
    data_quality: DataQuality


class RawTelemetryReader:
    """Open, read, and explicitly close the three Assetto Corsa mappings.

    ``read`` returns ``None`` while maps are unavailable or after an unstable
    read. In those cases ``data_quality`` explains the reader state. A repeated
    packet pair is returned as a STALE snapshot, except while the simulator is
    paused, when an unchanged packet is expected.
    """

    _PAGES = (
        ('physics', 'acpmf_physics', SPageFilePhysics),
        ('graphics', 'acpmf_graphics', SPageFileGraphic),
        ('static', 'acpmf_static', SPageFileStatic),
    )

    def __init__(
        self,
        mapping_factory: Optional[Callable] = None,
        consistency_retries: int = 3,
    ):
        if consistency_retries < 1:
            raise ValueError('consistency_retries deve ser pelo menos 1')
        self._mapping_factory = mapping_factory or mmap.mmap
        self._consistency_retries = consistency_retries
        self._maps = {}
        self._sequence = 0
        self._last_packet_pair = None
        self._data_quality = DataQuality.RECONNECTING

    @property
    def data_quality(self) -> DataQuality:
        return self._data_quality

    @property
    def is_open(self) -> bool:
        return len(self._maps) == len(self._PAGES)

    def open(self) -> bool:
        """Open all pages; return False and release partial opens if AC is down."""
        if self.is_open:
            return True
        self.close()
        try:
            for key, name, structure in self._PAGES:
                self._maps[key] = self._mapping_factory(
                    0, ctypes.sizeof(structure), name
                )
        except (OSError, ValueError):
            self.close()
            self._data_quality = DataQuality.RECONNECTING
            return False
        return True

    def read(self) -> Optional[TelemetrySnapshot]:
        """Copy a stable sample into Python values, without retaining mmap views."""
        if not self.open():
            return None

        for _ in range(self._consistency_retries):
            try:
                physics_before = self._packet_id('physics')
                graphics_before = self._packet_id('graphics')

                # from_buffer_copy owns its memory; no ctypes object references
                # any live mmap after these copies complete.
                physics = SPageFilePhysics.from_buffer_copy(
                    self._maps['physics'][:]
                )
                graphics = SPageFileGraphic.from_buffer_copy(
                    self._maps['graphics'][:]
                )
                static = SPageFileStatic.from_buffer_copy(self._maps['static'][:])

                physics_after = self._packet_id('physics')
                graphics_after = self._packet_id('graphics')
            except (OSError, ValueError, BufferError):
                self.close()
                self._data_quality = DataQuality.RECONNECTING
                return None

            if physics_before != physics_after or graphics_before != graphics_after:
                continue

            packet_pair = (physics_after, graphics_after)
            quality = DataQuality.OK
            if graphics.status == AC_OFF:
                quality = DataQuality.RECONNECTING
            elif (packet_pair == self._last_packet_pair
                  and graphics.status not in (AC_PAUSE,)):
                quality = DataQuality.STALE

            self._last_packet_pair = packet_pair
            self._sequence += 1
            self._data_quality = quality
            return TelemetrySnapshot(
                sequence=self._sequence,
                monotonic_ns=time.monotonic_ns(),
                physics_packet_id=physics_after,
                graphics_packet_id=graphics_after,
                session=int(graphics.session),
                status=int(graphics.status),
                completed_laps=int(graphics.completedLaps),
                current_lap_time_ms=int(graphics.iCurrentTime),
                last_lap_time_ms=int(graphics.iLastTime),
                best_lap_time_ms=int(graphics.iBestTime),
                current_sector_index=int(graphics.currentSectorIndex),
                last_sector_time_ms=int(graphics.lastSectorTime),
                sector_count=int(static.sectorCount),
                is_in_pit=bool(graphics.isInPit),
                is_in_pit_lane=bool(graphics.isInPitLane),
                flag=int(graphics.flag),
                car_damage_raw=tuple(float(v) for v in physics.carDamage),
                speed_kmh=float(physics.speedKmh),
                gear=int(physics.gear),
                rpm=int(physics.rpms),
                fuel_l=float(physics.fuel),
                tyre_temperature_c=tuple(float(v) for v in physics.tyreCoreTemperature),
                tyre_pressure=tuple(float(v) for v in physics.wheelsPressure),
                tyre_wear_raw=tuple(float(v) for v in physics.tyreWear),
                tyre_wear_pct=(None, None, None, None),
                track=str(static.track),
                car_model=str(static.carModel),
                tyre_compound=str(graphics.tyreCompound),
                data_quality=quality,
            )

        self._data_quality = DataQuality.INCOMPLETE
        return None

    def _packet_id(self, page: str) -> int:
        # packetId is the first aligned int32 in physics and graphics pages.
        return int.from_bytes(self._maps[page][:4], byteorder='little', signed=True)

    def close(self) -> None:
        """Close every opened mapping; safe to call repeatedly."""
        mappings, self._maps = self._maps, {}
        self._last_packet_pair = None
        for mapping in mappings.values():
            try:
                mapping.close()
            except (OSError, ValueError, BufferError):
                # No exported views are held by this reader. Keep closing other
                # pages even if an injected mapping implementation fails.
                pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_exc):
        self.close()
