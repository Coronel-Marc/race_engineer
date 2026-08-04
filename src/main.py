import time

from sim_info import info

try:
    while True:
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