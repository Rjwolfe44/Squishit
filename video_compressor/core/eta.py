"""Rolling ETA estimation utilities for compression jobs."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Deque, Tuple


@dataclass
class ETASnapshot:
    """Snapshot of progress speed and ETA."""

    units_per_second: float = 0.0
    eta_seconds: float = 0.0
    progress_percent: float = 0.0


class ETACalculator:
    """Estimate remaining time using a rolling progress window and smoothing."""

    def __init__(self, window_seconds: float = 5.0, smoothing: float = 0.25):
        self.window_seconds = max(1.0, window_seconds)
        self.smoothing = max(0.0, min(1.0, smoothing))
        self._samples: Deque[Tuple[float, int]] = deque()
        self._smoothed_speed = 0.0
        self._smoothed_eta = 0.0

    def update(self, completed_units: int, total_units: int, now: float | None = None) -> ETASnapshot:
        """Update the calculator and return the current estimate."""
        current_time = monotonic() if now is None else now
        completed_units = max(0, completed_units)
        total_units = max(0, total_units)

        self._samples.append((current_time, completed_units))
        while len(self._samples) > 1 and current_time - self._samples[0][0] > self.window_seconds:
            self._samples.popleft()

        raw_speed = 0.0
        if len(self._samples) >= 2:
            start_time, start_units = self._samples[0]
            elapsed = max(0.001, current_time - start_time)
            progressed = max(0, completed_units - start_units)
            raw_speed = progressed / elapsed

        if raw_speed > 0:
            if self._smoothed_speed <= 0:
                self._smoothed_speed = raw_speed
            else:
                self._smoothed_speed = (
                    self._smoothed_speed * (1.0 - self.smoothing)
                    + raw_speed * self.smoothing
                )

        remaining = max(0, total_units - completed_units)
        raw_eta = remaining / self._smoothed_speed if self._smoothed_speed > 0 else 0.0

        if raw_eta > 0:
            if self._smoothed_eta <= 0:
                self._smoothed_eta = raw_eta
            else:
                self._smoothed_eta = (
                    self._smoothed_eta * (1.0 - self.smoothing)
                    + raw_eta * self.smoothing
                )
        elif completed_units >= total_units and total_units > 0:
            self._smoothed_eta = 0.0

        progress = (completed_units / total_units) * 100 if total_units > 0 else 0.0
        return ETASnapshot(
            units_per_second=max(0.0, self._smoothed_speed),
            eta_seconds=max(0.0, self._smoothed_eta),
            progress_percent=max(0.0, min(100.0, progress)),
        )
