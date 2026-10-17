"""Asynchronous, single-owner CSV persistence for completed laps."""
from dataclasses import dataclass
import csv
import os
import re
import threading
import time
from typing import Optional

try:
    from .domain_models import MetricSummary, TyreSummary
    from .lap_tracker import LapCompleted
    from .raw_telemetry_reader import DataQuality
    from .telemetry_sampler import BoundedEventQueue
except ImportError:
    from domain_models import MetricSummary, TyreSummary
    from lap_tracker import LapCompleted
    from raw_telemetry_reader import DataQuality
    from telemetry_sampler import BoundedEventQueue


@dataclass(frozen=True, slots=True)
class LapRecord:
    schema_version: int
    session_id: str
    session_type: int
    track: str
    car_model: str
    tyre_compound: str
    lap_number: int
    lap_time_ms: int
    is_best_lap: bool
    valid: Optional[bool]
    data_quality: DataQuality
    timestamp_ns: int
    fuel_start_l: Optional[float]
    fuel_end_l: Optional[float]
    fuel_delta_net_l: Optional[float]
    fuel_used_l: Optional[float]
    refuel_detected: Optional[bool]
    sectors_ms: tuple[Optional[int], ...]
    tyre_summary: Optional[TyreSummary]
    tyre_temperature_c: Optional[tuple[float, float, float, float]]
    tyre_pressure_raw: Optional[tuple[float, float, float, float]]
    tyre_wear_raw: Optional[tuple[float, float, float, float]]

    @classmethod
    def from_lap(
        cls,
        lap: LapCompleted,
        *,
        session_id: str,
        session_type: int,
        track: str,
        car_model: str,
        tyre_compound: str,
    ):
        return cls(
            schema_version=1,
            session_id=session_id,
            session_type=session_type,
            track=track,
            car_model=car_model,
            tyre_compound=tyre_compound,
            lap_number=lap.lap_number,
            lap_time_ms=lap.time_ms,
            is_best_lap=lap.is_best_lap,
            valid=lap.valid,
            data_quality=lap.data_quality,
            timestamp_ns=lap.timestamp_ns,
            fuel_start_l=lap.fuel_start,
            fuel_end_l=lap.fuel_end,
            fuel_delta_net_l=lap.fuel_delta_net,
            fuel_used_l=lap.fuel_used,
            refuel_detected=lap.refuel_detected,
            sectors_ms=lap.sectors_ms,
            tyre_summary=lap.tyre_summary,
            tyre_temperature_c=lap.tyre_temperature_c,
            tyre_pressure_raw=lap.tyre_pressure_raw,
            tyre_wear_raw=lap.tyre_wear_raw,
        )


@dataclass(frozen=True, slots=True)
class CsvWriterMetrics:
    running: bool
    queued_records: int
    written_records: int
    write_errors: int
    dropped_records: int
    shutdown_discarded: int
    last_error: Optional[str]


class CsvLapWriter:
    """A bounded queue and one writer thread own all CSV file operations."""

    BASE_COLUMNS = (
        'schema_version', 'session_id', 'session_type', 'track', 'car_model',
        'tyre_compound', 'lap_number', 'lap_time_ms', 'is_best_lap', 'valid',
        'data_quality', 'timestamp_ns', 'fuel_start_l', 'fuel_end_l',
        'fuel_delta_net_l', 'fuel_used_l', 'refuel_detected',
    )
    LINE_COLUMNS = tuple(
        f'{position}_{metric}_at_line'
        for position in ('fl', 'fr', 'rl', 'rr')
        for metric in ('temperature_c', 'pressure_raw', 'wear_raw')
    )
    SECTOR_COLUMNS = tuple(f'sector_{i}_ms' for i in range(1, 9))
    TYRE_COLUMNS = tuple(
        f'{position}_{metric}_{stat}'
        for position in ('fl', 'fr', 'rl', 'rr')
        for metric in ('temperature_c', 'pressure_raw', 'wear_raw')
        for stat in ('avg', 'sum', 'min', 'max', 'count')
    )
    COLUMNS = BASE_COLUMNS + SECTOR_COLUMNS + LINE_COLUMNS + TYRE_COLUMNS

    def __init__(self, output_dir='telemetry_logs', queue_capacity=64):
        self.output_dir = os.fspath(output_dir)
        self.queue = BoundedEventQueue[LapRecord](queue_capacity)
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._accepting = False
        self._drain_deadline_ns = None
        self._written_records = 0
        self._write_errors = 0
        self._shutdown_discarded = 0
        self._last_error = None

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError('este escritor já foi iniciado')
            self._accepting = True
            self._thread = threading.Thread(
                target=self._run, name='CsvLapWriter', daemon=False
            )
            self._thread.start()

    def publish(self, record: LapRecord) -> bool:
        """Queue a record without blocking; full queues reject the newest row."""
        with self._lock:
            if not self._accepting:
                return False
        return self.queue.publish(record)

    def stop(self, drain_timeout: float = 2.0) -> bool:
        if drain_timeout < 0:
            raise ValueError('drain_timeout não pode ser negativo')
        with self._lock:
            self._accepting = False
            thread = self._thread
            self._drain_deadline_ns = time.monotonic_ns() + int(drain_timeout * 1e9)
            self._stop_event.set()
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(drain_timeout + 0.2)
        return not thread.is_alive()

    def metrics(self) -> CsvWriterMetrics:
        with self._lock:
            thread = self._thread
            return CsvWriterMetrics(
                running=thread is not None and thread.is_alive(),
                queued_records=self.queue.qsize(),
                written_records=self._written_records,
                write_errors=self._write_errors,
                dropped_records=self.queue.dropped_count,
                shutdown_discarded=self._shutdown_discarded,
                last_error=self._last_error,
            )

    def _run(self):
        active_path = None
        active_file = None
        writer = None
        try:
            while True:
                if self._stop_event.is_set():
                    if self.queue.qsize() == 0:
                        break
                    if (self._drain_deadline_ns is not None
                            and time.monotonic_ns() >= self._drain_deadline_ns):
                        self._discard_remaining()
                        break
                try:
                    record = self.queue.get(timeout=0.05)
                except Exception:
                    continue
                try:
                    path = self._path_for(record)
                    if path != active_path:
                        if active_file is not None:
                            active_file.close()
                        os.makedirs(self.output_dir, exist_ok=True)
                        needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
                        active_file = open(path, 'a', encoding='utf-8', newline='')
                        writer = csv.DictWriter(
                            active_file, fieldnames=self.COLUMNS, extrasaction='ignore'
                        )
                        if needs_header:
                            writer.writeheader()
                        active_path = path
                    writer.writerow(self._row_for(record))
                    active_file.flush()
                    with self._lock:
                        self._written_records += 1
                        self._last_error = None
                except Exception as exc:
                    with self._lock:
                        self._write_errors += 1
                        self._last_error = f'{type(exc).__name__}: {exc}'
                    if active_file is not None:
                        try:
                            active_file.close()
                        except OSError:
                            pass
                    active_file = writer = active_path = None
        finally:
            if active_file is not None:
                try:
                    active_file.flush()
                    active_file.close()
                except OSError as exc:
                    with self._lock:
                        self._write_errors += 1
                        self._last_error = f'{type(exc).__name__}: {exc}'

    def _discard_remaining(self):
        discarded = 0
        while True:
            try:
                self.queue.get_nowait()
                discarded += 1
            except Exception:
                break
        with self._lock:
            self._shutdown_discarded += discarded

    def _path_for(self, record):
        parts = (
            record.session_id, record.track, record.car_model,
            record.tyre_compound,
        )
        filename = '_'.join(self._slug(part) for part in parts) + '.csv'
        return os.path.join(self.output_dir, filename)

    @staticmethod
    def _slug(value):
        slug = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value)).strip('.-_')
        return slug or 'unknown'

    @classmethod
    def _row_for(cls, record):
        row = {name: getattr(record, name) for name in cls.BASE_COLUMNS}
        row['data_quality'] = record.data_quality.value
        for index in range(8):
            row[f'sector_{index + 1}_ms'] = (
                record.sectors_ms[index]
                if index < len(record.sectors_ms) else None
            )
        line_values = (
            ('temperature_c', record.tyre_temperature_c),
            ('pressure_raw', record.tyre_pressure_raw),
            ('wear_raw', record.tyre_wear_raw),
        )
        for metric_name, values in line_values:
            for index, wheel in enumerate(('fl', 'fr', 'rl', 'rr')):
                row[f'{wheel}_{metric_name}_at_line'] = (
                    values[index] if values is not None else None
                )
        summary = record.tyre_summary
        metrics = (
            ('temperature_c', summary.temperature_c if summary else None),
            ('pressure_raw', summary.pressure_raw if summary else None),
            ('wear_raw', summary.wear_raw if summary else None),
        )
        wheel_names = ('fl', 'fr', 'rl', 'rr')
        for metric_name, values in metrics:
            for index, wheel in enumerate(wheel_names):
                value = values[index] if values else None
                row[f'{wheel}_{metric_name}_avg'] = value.average if value else None
                row[f'{wheel}_{metric_name}_sum'] = value.sample_sum if value else None
                row[f'{wheel}_{metric_name}_min'] = value.minimum if value else None
                row[f'{wheel}_{metric_name}_max'] = value.maximum if value else None
                row[f'{wheel}_{metric_name}_count'] = value.count if value else 0
        return row
