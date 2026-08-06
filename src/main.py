import time

from lap_tracker import LapTracker, format_lap_completed
from sim_info import info

tracker = LapTracker()

try:
    while True:
        lap_completed = tracker.update(
            completed_laps=info.graphics.completedLaps,
            last_lap_time_ms=info.graphics.iLastTime,
            best_lap_time_ms=info.graphics.iBestTime,
            fuel=info.physics.fuel,
        )

        if lap_completed is not None:
            # Encerra a linha de telemetria antes de imprimir o evento.
            print(f"\r{' ' * 100}\r{format_lap_completed(lap_completed)}\n")

        print(
            f"\r"
            f"velocidade: {info.physics.speedKmh:6.1f} km/h | "
            f"RPM: {info.physics.rpms:5d} | "
            f"Marcha: {info.physics.gear} | "
            f"Combustível: {info.physics.fuel:5.1f} L | ",
            end="",
            flush=True,
        )

        time.sleep(0.1)

except KeyboardInterrupt:
    print("\nLeitura encerrada.")
