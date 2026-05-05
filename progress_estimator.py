"""Heuristic progress estimation for scans with unknown totals."""

import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from scanner import ScanProgress


@dataclass(frozen=True)
class ScanEstimate:
    """Progress values safe to pass to Rich's determinate progress task."""

    completed: int
    total: int
    is_estimating: bool
    eta_seconds: Optional[float]
    eta_text: str


class ScanProgressEstimator:
    """Estimate scan completion from discovered and completed directory work."""

    def __init__(
        self,
        placeholder_total: int = 1_000,
        min_directories_for_ratio: int = 4,
        upward_smoothing: float = 0.45,
        downward_smoothing: float = 0.35,
        max_running_completion: float = 0.995,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.placeholder_total = placeholder_total
        self.min_directories_for_ratio = min_directories_for_ratio
        self.upward_smoothing = upward_smoothing
        self.downward_smoothing = downward_smoothing
        self.max_running_completion = max_running_completion
        self.clock = clock
        self._estimated_total = None
        self._start_time = None

    def update(self, progress: ScanProgress) -> ScanEstimate:
        """Return a smoothed estimate for the latest scan progress snapshot."""
        now = self.clock()
        if self._start_time is None:
            self._start_time = now

        completed = progress.files_scanned
        if progress.is_finished:
            self._estimated_total = float(completed)
            return ScanEstimate(
                completed=completed,
                total=completed,
                is_estimating=False,
                eta_seconds=0.0,
                eta_text="ETA 0:00:00",
            )

        raw_total, is_estimating = self._raw_total(progress)
        if self._estimated_total is None:
            smoothed_total = raw_total
        elif raw_total >= self._estimated_total:
            smoothed_total = self._smooth(raw_total, self.upward_smoothing)
        else:
            smoothed_total = self._smooth(raw_total, self.downward_smoothing)

        total = max(math.ceil(smoothed_total), completed + 1)
        if completed > 0 and progress.directories_remaining > 0:
            total = max(total, math.ceil(completed / self.max_running_completion))

        self._estimated_total = float(total)
        eta_seconds = self._eta_seconds(completed, total, now)
        return ScanEstimate(
            completed=completed,
            total=total,
            is_estimating=is_estimating,
            eta_seconds=eta_seconds,
            eta_text=self._format_eta(eta_seconds),
        )

    def _raw_total(self, progress: ScanProgress) -> tuple[float, bool]:
        discovered = max(progress.directories_discovered, 1)
        completed_dirs = progress.directories_completed
        files_scanned = progress.files_scanned

        if (
            completed_dirs <= 0
            or discovered < self.min_directories_for_ratio
            or files_scanned <= 0
        ):
            raw_total = max(
                self.placeholder_total,
                files_scanned + self.placeholder_total,
            )
            return float(raw_total), True

        completion_ratio = min(max(completed_dirs / discovered, 0.01), 1.0)
        estimated_total = files_scanned / completion_ratio
        remaining_dirs = max(discovered - completed_dirs, 0)
        estimated_total = max(estimated_total, files_scanned + remaining_dirs)
        return float(estimated_total), False

    def _smooth(self, raw_total: float, weight: float) -> float:
        return (self._estimated_total * (1 - weight)) + (raw_total * weight)

    def _eta_seconds(self, completed: int, total: int, now: float) -> Optional[float]:
        if self._start_time is None or completed <= 0 or total <= completed:
            return None

        elapsed = now - self._start_time
        if elapsed <= 0:
            return None

        files_per_second = completed / elapsed
        if files_per_second <= 0:
            return None

        return (total - completed) / files_per_second

    def _format_eta(self, seconds: Optional[float]) -> str:
        if seconds is None:
            return "ETA estimating"
        if seconds < 1:
            return "ETA <1s"

        rounded_seconds = math.ceil(seconds)
        minutes, remaining_seconds = divmod(rounded_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"ETA {hours:d}:{minutes:02d}:{remaining_seconds:02d}"
