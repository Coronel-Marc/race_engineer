import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lap_tracker import LapTracker, format_lap_completed, format_lap_time


class LapTrackerTest(unittest.TestCase):
    def test_emits_one_event_for_each_completed_lap(self):
        tracker = LapTracker()

        samples = (
            (0, 0, 0, 31.00),
            (0, 0, 0, 30.20),
            (1, 102_381, 102_381, 28.66),
            (1, 102_381, 102_381, 28.20),
            (2, 101_900, 101_900, 26.35),
            (3, 103_050, 101_900, 24.01),
        )

        events = [
            event
            for completed, last_time, best_time, fuel in samples
            if (
                event := tracker.update(
                    completed_laps=completed,
                    last_lap_time_ms=last_time,
                    best_lap_time_ms=best_time,
                    fuel=fuel,
                )
            )
            is not None
        ]

        self.assertEqual([event.lap_number for event in events], [1, 2, 3])
        self.assertEqual([event.time_ms for event in events], [102_381, 101_900, 103_050])
        self.assertAlmostEqual(events[0].fuel_used, 2.34)
        self.assertAlmostEqual(events[1].fuel_used, 2.31)
        self.assertAlmostEqual(events[2].fuel_used, 2.34)
        self.assertEqual([event.is_best_lap for event in events], [True, True, False])

    def test_does_not_duplicate_event_between_laps(self):
        tracker = LapTracker()
        tracker.update(
            completed_laps=0, last_lap_time_ms=0, best_lap_time_ms=0, fuel=10.0
        )

        first = tracker.update(
            completed_laps=1,
            last_lap_time_ms=90_000,
            best_lap_time_ms=90_000,
            fuel=8.0,
        )
        duplicate = tracker.update(
            completed_laps=1,
            last_lap_time_ms=90_000,
            best_lap_time_ms=90_000,
            fuel=7.9,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)

    def test_rebases_when_session_restarts(self):
        tracker = LapTracker()
        tracker.update(
            completed_laps=3, last_lap_time_ms=90_000, best_lap_time_ms=89_000, fuel=12.0
        )

        restart = tracker.update(
            completed_laps=0, last_lap_time_ms=0, best_lap_time_ms=0, fuel=30.0
        )
        event = tracker.update(
            completed_laps=1,
            last_lap_time_ms=100_000,
            best_lap_time_ms=100_000,
            fuel=27.5,
        )

        self.assertIsNone(restart)
        self.assertEqual(event.lap_number, 1)
        self.assertAlmostEqual(event.fuel_used, 2.5)

    def test_formats_event_for_terminal(self):
        tracker = LapTracker()
        tracker.update(
            completed_laps=0, last_lap_time_ms=0, best_lap_time_ms=0, fuel=10.0
        )
        event = tracker.update(
            completed_laps=1,
            last_lap_time_ms=102_381,
            best_lap_time_ms=102_381,
            fuel=7.66,
        )

        self.assertEqual(format_lap_time(102_381), "01:42.381")
        self.assertEqual(
            format_lap_completed(event),
            "Volta 1 concluída\n"
            "Tempo: 01:42.381\n"
            "Combustível consumido: 2.34 L\n"
            "Melhor volta: sim",
        )


if __name__ == "__main__":
    unittest.main()
