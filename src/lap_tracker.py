from dataclasses import dataclass


@dataclass(frozen=True)
class LapCompleted:
    """Dados consolidados no instante em que uma volta termina."""

    lap_number: int
    time_ms: int
    fuel_used: float
    is_best_lap: bool


class LapTracker:
    """Transforma amostras de telemetria em eventos de volta concluída."""

    def __init__(self) -> None:
        self._completed_laps: int | None = None
        self._fuel_at_lap_start: float | None = None

    def update(
        self,
        *,
        completed_laps: int,
        last_lap_time_ms: int,
        best_lap_time_ms: int,
        fuel: float,
    ) -> LapCompleted | None:
        """Recebe uma amostra e devolve um evento apenas ao fechar uma volta.

        Na primeira amostra o tracker apenas estabelece a linha de base. Se a
        sessão for reiniciada (o contador de voltas diminuir), uma nova linha
        de base também é criada.
        """

        if self._completed_laps is None or completed_laps < self._completed_laps:
            self._completed_laps = completed_laps
            self._fuel_at_lap_start = fuel
            return None

        if completed_laps == self._completed_laps:
            return None

        # A leitura normal avança uma volta por vez. Caso amostras sejam
        # perdidas, emitimos a volta mais recente, pois a memória compartilhada
        # só conserva o tempo da última volta.
        starting_fuel = (
            self._fuel_at_lap_start
            if self._fuel_at_lap_start is not None
            else fuel
        )
        event = LapCompleted(
            lap_number=completed_laps,
            time_ms=last_lap_time_ms,
            fuel_used=max(0.0, starting_fuel - fuel),
            is_best_lap=(
                last_lap_time_ms > 0
                and best_lap_time_ms > 0
                and last_lap_time_ms == best_lap_time_ms
            ),
        )

        self._completed_laps = completed_laps
        self._fuel_at_lap_start = fuel
        return event


def format_lap_time(time_ms: int) -> str:
    """Formata milissegundos como MM:SS.mmm."""

    minutes, remainder = divmod(max(0, time_ms), 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def format_lap_completed(event: LapCompleted) -> str:
    """Cria o bloco exibido no terminal para uma volta concluída."""

    return "\n".join(
        (
            f"Volta {event.lap_number} concluída",
            f"Tempo: {format_lap_time(event.time_ms)}",
            f"Combustível consumido: {event.fuel_used:.2f} L",
            f"Melhor volta: {'sim' if event.is_best_lap else 'não'}",
        )
    )
