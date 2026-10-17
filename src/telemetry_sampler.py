"""Threaded polling and bounded publication for raw telemetry snapshots."""
from dataclasses import dataclass
from queue import Empty, Full, Queue
import threading
import time
from typing import Generic, Optional, TypeVar

try:
    from .raw_telemetry_reader import RawTelemetryReader, TelemetrySnapshot
except ImportError:
    from raw_telemetry_reader import RawTelemetryReader, TelemetrySnapshot


T = TypeVar('T')


class LatestSnapshotMailbox:
    """Capacity-one mailbox; a new sample replaces an unread older sample."""

    def __init__(self):
        self._queue = Queue(maxsize=1)
        self._publish_lock = threading.Lock()
        self._dropped = 0

    @property
    def dropped_count(self) -> int:
        with self._publish_lock:
            return self._dropped

    def publish(self, snapshot: TelemetrySnapshot) -> bool:
        """Publish without waiting; return True when an unread item was replaced."""
        with self._publish_lock:
            dropped = False
            try:
                self._queue.put_nowait(snapshot)
            except Full:
                try:
                    self._queue.get_nowait()
                    dropped = True
                except Empty:
                    # A consumer may have drained the item between Full and get.
                    pass
                self._queue.put_nowait(snapshot)
            if dropped:
                self._dropped += 1
            return dropped

    def get_nowait(self) -> TelemetrySnapshot:
        return self._queue.get_nowait()

    def get(self, timeout: Optional[float] = None) -> TelemetrySnapshot:
        return self._queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self._queue.empty()


class BoundedEventQueue(Generic[T]):
    """FIFO event queue that drops the newest event when full.

    Producers never block. Events already accepted remain in order; on overflow
    the rejected event increments ``dropped_count``.
    """

    def __init__(self, maxsize: int):
        if maxsize < 1:
            raise ValueError('maxsize deve ser pelo menos 1')
        self._queue = Queue(maxsize=maxsize)
        self._dropped = 0
        self._counter_lock = threading.Lock()

    @property
    def dropped_count(self) -> int:
        with self._counter_lock:
            return self._dropped

    def publish(self, event: T) -> bool:
        """Return False if the queue is full; no accepted event is reordered."""
        try:
            self._queue.put_nowait(event)
            return True
        except Full:
            with self._counter_lock:
                self._dropped += 1
            return False

    def get(self, timeout: Optional[float] = None) -> T:
        return self._queue.get(timeout=timeout)

    def get_nowait(self) -> T:
        return self._queue.get_nowait()

    def qsize(self) -> int:
        return self._queue.qsize()


@dataclass(frozen=True, slots=True)
class SamplerMetrics:
    running: bool
    read_attempts: int
    snapshots_read: int
    snapshots_published: int
    snapshots_dropped: int
    read_errors: int
    missed_deadlines: int
    last_iteration_ns: int
    max_iteration_ns: int


class TelemetrySampler:
    """Poll a ``RawTelemetryReader`` on a normal-priority dedicated thread."""

    def __init__(
        self,
        reader: RawTelemetryReader,
        period_seconds: float = 0.1,
        stop_event: Optional[threading.Event] = None,
        mailbox: Optional[LatestSnapshotMailbox] = None,
    ):
        if period_seconds <= 0:
            raise ValueError('period_seconds deve ser maior que zero')
        self.reader = reader
        self.period_ns = int(period_seconds * 1_000_000_000)
        if self.period_ns < 1:
            raise ValueError('period_seconds é pequeno demais')
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.mailbox = mailbox if mailbox is not None else LatestSnapshotMailbox()
        self._metrics_lock = threading.Lock()
        self._read_attempts = 0
        self._snapshots_read = 0
        self._snapshots_published = 0
        self._read_errors = 0
        self._missed_deadlines = 0
        self._last_iteration_ns = 0
        self._max_iteration_ns = 0
        self._thread = None
        self._lifecycle_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start exactly one non-daemon sampler thread for this instance."""
        with self._lifecycle_lock:
            if self._thread is not None:
                raise RuntimeError('este sampler já foi iniciado')
            if self.stop_event.is_set():
                raise RuntimeError('stop_event já está sinalizado')
            self._thread = threading.Thread(
                target=self._run,
                name='TelemetrySampler',
                daemon=False,
            )
            self._thread.start()

    def stop(self, timeout: Optional[float] = None) -> bool:
        """Signal the shared stop event and wait up to ``timeout`` seconds."""
        self.stop_event.set()
        thread = self._thread
        if thread is None:
            self.reader.close()
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def metrics(self) -> SamplerMetrics:
        with self._metrics_lock:
            attempts = self._read_attempts
            read = self._snapshots_read
            published = self._snapshots_published
            errors = self._read_errors
            missed = self._missed_deadlines
            last_duration = self._last_iteration_ns
            max_duration = self._max_iteration_ns
        return SamplerMetrics(
            running=self.is_running,
            read_attempts=attempts,
            snapshots_read=read,
            snapshots_published=published,
            snapshots_dropped=self.mailbox.dropped_count,
            read_errors=errors,
            missed_deadlines=missed,
            last_iteration_ns=last_duration,
            max_iteration_ns=max_duration,
        )

    def _run(self) -> None:
        deadline_ns = time.monotonic_ns()
        try:
            while not self.stop_event.is_set():
                iteration_start_ns = time.monotonic_ns()
                with self._metrics_lock:
                    self._read_attempts += 1
                snapshot = None
                try:
                    snapshot = self.reader.read()
                except Exception:
                    # Keep the polling lifecycle alive; callers can observe the
                    # error counter and the reader will be retried next period.
                    with self._metrics_lock:
                        self._read_errors += 1
                    try:
                        self.reader.close()
                    except Exception:
                        pass

                if snapshot is not None:
                    with self._metrics_lock:
                        self._snapshots_read += 1
                    self.mailbox.publish(snapshot)
                    with self._metrics_lock:
                        self._snapshots_published += 1

                iteration_end_ns = time.monotonic_ns()
                duration_ns = iteration_end_ns - iteration_start_ns
                with self._metrics_lock:
                    self._last_iteration_ns = duration_ns
                    self._max_iteration_ns = max(self._max_iteration_ns, duration_ns)

                deadline_ns += self.period_ns
                now_ns = time.monotonic_ns()
                if deadline_ns <= now_ns:
                    skipped = ((now_ns - deadline_ns) // self.period_ns) + 1
                    deadline_ns += skipped * self.period_ns
                    with self._metrics_lock:
                        self._missed_deadlines += skipped
                self.stop_event.wait(max(0, deadline_ns - time.monotonic_ns()) / 1e9)
        finally:
            self.reader.close()
