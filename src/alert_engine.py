"""Deterministic, hysteretic alert rules for fresh live telemetry."""
from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional

try:
    from .raw_telemetry_reader import AC_LIVE, DataQuality, TelemetrySnapshot
except ImportError:
    from raw_telemetry_reader import AC_LIVE, DataQuality, TelemetrySnapshot


class AlertSeverity(str, Enum):
    INFO = 'info'
    WARNING = 'warning'
    CRITICAL = 'critical'


@dataclass(frozen=True, slots=True)
class AlertEvent:
    code: str
    context: str
    severity: AlertSeverity
    timestamp_ns: int
    reason: str
    state: str = 'active'
    session_id: Optional[str] = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class AlertConfig:
    fuel_low_l: Optional[float] = 5.0
    fuel_hysteresis_l: float = 2.0
    pressure_min_raw: Optional[float] = None
    pressure_max_raw: Optional[float] = None
    pressure_hysteresis_raw: float = 1.0
    temperature_min_c: Optional[float] = None
    temperature_max_c: Optional[float] = 120.0
    temperature_hysteresis_c: float = 5.0
    tyre_wear_max_raw: Optional[float] = None
    tyre_wear_hysteresis_raw: float = 0.05
    damage_threshold_raw: Optional[float] = None
    damage_hysteresis_raw: float = 0.05
    candidate_samples: int = 2
    cooldown_seconds: float = 30.0

    def __post_init__(self):
        if self.candidate_samples < 1:
            raise ValueError('candidate_samples deve ser pelo menos 1')
        if not math.isfinite(self.cooldown_seconds) or self.cooldown_seconds < 0:
            raise ValueError('cooldown_seconds não pode ser negativo')
        thresholds = (
            self.fuel_low_l, self.fuel_hysteresis_l,
            self.pressure_min_raw, self.pressure_max_raw,
            self.pressure_hysteresis_raw, self.temperature_min_c,
            self.temperature_max_c, self.temperature_hysteresis_c,
            self.tyre_wear_max_raw, self.tyre_wear_hysteresis_raw,
            self.damage_threshold_raw, self.damage_hysteresis_raw,
        )
        if any(value is not None and (not math.isfinite(value) or value < 0)
               for value in thresholds):
            raise ValueError('limiares e histereses devem ser finitos e não negativos')
        if (self.pressure_min_raw is not None and self.pressure_max_raw is not None
                and self.pressure_min_raw >= self.pressure_max_raw):
            raise ValueError('pressure_min_raw deve ser menor que pressure_max_raw')
        if (self.temperature_min_c is not None and self.temperature_max_c is not None
                and self.temperature_min_c >= self.temperature_max_c):
            raise ValueError('temperature_min_c deve ser menor que temperature_max_c')


@dataclass
class _RuleState:
    state: str = 'inactive'
    candidate_count: int = 0
    cooldown_until_ns: int = 0


class AlertEngine:
    """Evaluate rules and emit transitions without blocking on the consumer.

    Pressure and damage limits use raw source values, so their rules are
    disabled until configured from validated telemetry. Wear alerts are also
    disabled by default because the source does not define a normalized range.
    """

    _WHEEL_NAMES = ('fl', 'fr', 'rl', 'rr')

    def __init__(self, config: Optional[AlertConfig] = None, event_queue=None):
        self.config = config if config is not None else AlertConfig()
        self.event_queue = event_queue
        self._states: dict[tuple[str, str], _RuleState] = {}
        self.dropped_events = 0

    @property
    def machine_states(self) -> dict[tuple[str, str], str]:
        return {key: value.state for key, value in self._states.items()}

    def reset(self) -> None:
        self._states.clear()

    def evaluate(
        self,
        snapshot: TelemetrySnapshot,
        *,
        session_id: str,
        smoothed_temperature_c=None,
        smoothed_pressure_raw=None,
        smoothed_wear_raw=None,
    ) -> tuple[AlertEvent, ...]:
        if snapshot.status != AC_LIVE or snapshot.data_quality != DataQuality.OK:
            return ()

        rules = self._build_rules(
            snapshot,
            smoothed_temperature_c,
            smoothed_pressure_raw,
            smoothed_wear_raw,
        )
        emitted = []
        # Rule keys include session and context so a new session cannot inherit
        # cooldown state from the prior run.
        for code, context, severity, reason, active, recovered in rules:
            key = (f'{session_id}:{code}', context)
            event = self._transition(
                key, code, context, severity, reason, active, recovered,
                snapshot.monotonic_ns, session_id,
            )
            if event is not None:
                emitted.append(event)
                if self.event_queue is not None and not self.event_queue.publish(event):
                    self.dropped_events += 1
        return tuple(emitted)

    def _build_rules(self, snapshot, temperatures, pressures, wears):
        cfg = self.config
        rules = []

        def add(code, context, severity, reason, active, recovered):
            rules.append((code, context, severity, reason, active, recovered))

        fuel = snapshot.fuel_l
        if cfg.fuel_low_l is not None and fuel is not None and math.isfinite(fuel):
            active = fuel <= cfg.fuel_low_l
            recovered = fuel >= cfg.fuel_low_l + cfg.fuel_hysteresis_l
            add('fuel.low', 'car', AlertSeverity.CRITICAL,
                f'Combustível em {fuel:.1f} L; limite {cfg.fuel_low_l:.1f} L.',
                active, recovered)

        tyre_rules_enabled = not snapshot.is_in_pit and not snapshot.is_in_pit_lane
        pressures = pressures or snapshot.tyre_pressure
        for index, raw in enumerate(pressures if tyre_rules_enabled else ()):
            if raw is None or not math.isfinite(raw):
                continue
            context = self._WHEEL_NAMES[index]
            if cfg.pressure_min_raw is not None:
                active = raw <= cfg.pressure_min_raw
                recovered = raw >= cfg.pressure_min_raw + cfg.pressure_hysteresis_raw
                add('tyre_pressure.low', context, AlertSeverity.WARNING,
                    f'Pressão {context.upper()} em {raw:.2f} (escala bruta).',
                    active, recovered)
            if cfg.pressure_max_raw is not None:
                active = raw >= cfg.pressure_max_raw
                recovered = raw <= cfg.pressure_max_raw - cfg.pressure_hysteresis_raw
                add('tyre_pressure.high', context, AlertSeverity.WARNING,
                    f'Pressão {context.upper()} em {raw:.2f} (escala bruta).',
                    active, recovered)

        temperatures = temperatures or snapshot.tyre_temperature_c
        for index, value in enumerate(temperatures if tyre_rules_enabled else ()):
            if value is None or not math.isfinite(value):
                continue
            context = self._WHEEL_NAMES[index]
            if cfg.temperature_min_c is not None:
                active = value <= cfg.temperature_min_c
                recovered = value >= cfg.temperature_min_c + cfg.temperature_hysteresis_c
                add('tyre_temperature.low', context, AlertSeverity.WARNING,
                    f'Temperatura {context.upper()} em {value:.1f} °C.', active, recovered)
            if cfg.temperature_max_c is not None:
                active = value >= cfg.temperature_max_c
                recovered = value <= cfg.temperature_max_c - cfg.temperature_hysteresis_c
                add('tyre_temperature.high', context, AlertSeverity.WARNING,
                    f'Temperatura {context.upper()} em {value:.1f} °C.', active, recovered)

        if cfg.tyre_wear_max_raw is not None and tyre_rules_enabled:
            wears = wears or snapshot.tyre_wear_raw
            for index, value in enumerate(wears):
                if value is None or not math.isfinite(value):
                    continue
                context = self._WHEEL_NAMES[index]
                active = value >= cfg.tyre_wear_max_raw
                recovered = value <= cfg.tyre_wear_max_raw - cfg.tyre_wear_hysteresis_raw
                add('tyre_wear.high', context, AlertSeverity.WARNING,
                    f'Desgaste bruto {context.upper()} em {value:.3f}.', active, recovered)

        flag_rules = (
            (1, 'flag.blue', AlertSeverity.INFO, 'Bandeira azul.'),
            (2, 'flag.yellow', AlertSeverity.WARNING, 'Bandeira amarela.'),
            (3, 'flag.black', AlertSeverity.CRITICAL, 'Bandeira preta.'),
            (4, 'flag.white', AlertSeverity.INFO, 'Bandeira branca.'),
            (5, 'flag.checkered', AlertSeverity.INFO, 'Bandeira quadriculada.'),
            (6, 'flag.penalty', AlertSeverity.WARNING,
             'Penalidade indicada pelo simulador.'),
        )
        for flag_value, code, severity, reason in flag_rules:
            add(code, 'session', severity, reason,
                snapshot.flag == flag_value, snapshot.flag != flag_value)

        if cfg.damage_threshold_raw is not None:
            for index, value in enumerate(snapshot.car_damage_raw):
                if value is None or not math.isfinite(value):
                    continue
                context = f'part_{index}'
                active = value >= cfg.damage_threshold_raw
                recovered = value <= max(
                    0.0, cfg.damage_threshold_raw - cfg.damage_hysteresis_raw
                )
                add('car_damage.high', context, AlertSeverity.CRITICAL,
                    f'Dano bruto na parte {index}: {value:.3f}.', active, recovered)
        return rules

    def _transition(
        self, key, code, context, severity, reason, active, recovered,
        timestamp_ns, session_id,
    ):
        machine = self._states.setdefault(key, _RuleState())
        cooldown_ns = int(self.config.cooldown_seconds * 1_000_000_000)

        if machine.state == 'inactive':
            if active:
                machine.state = 'candidate'
                machine.candidate_count = 1
        elif machine.state == 'candidate':
            if active:
                machine.candidate_count += 1
                if machine.candidate_count >= self.config.candidate_samples:
                    machine.state = 'active'
                    machine.candidate_count = 0
                    return AlertEvent(
                        code, context, severity, timestamp_ns, reason,
                        session_id=session_id,
                    )
            else:
                machine.state = 'inactive'
                machine.candidate_count = 0
        elif machine.state == 'active':
            if recovered:
                machine.state = 'cooldown'
                machine.cooldown_until_ns = timestamp_ns + cooldown_ns
                return AlertEvent(
                    code, context, severity, timestamp_ns,
                    f'Condição normalizada: {reason}', 'cleared', session_id,
                )
        elif machine.state == 'cooldown':
            if timestamp_ns >= machine.cooldown_until_ns:
                if active:
                    machine.state = 'candidate'
                    machine.candidate_count = 1
                else:
                    machine.state = 'inactive'
        return None
