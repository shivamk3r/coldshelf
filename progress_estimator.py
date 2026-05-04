"""Heuristic progress estimation for scans with unknown totals."""

import math
from dataclasses import dataclass

from scanner import ScanProgress


@dataclass(frozen=True)
class ScanEstimate:
    """Progress values safe to pass to Rich's determinate progress task."""

    completed: int
    total: int
    is_estimating: bool


class ScanProgressEstimator:
    """Estimate scan completion from discovered and completed directory work."""

    def __init__(
        self,
        placeholder_total: int = 1_000,
        min_directories_for_ratio: int = 4,
        upward_smoothing: float = 0.45,
        downward_smoothing: float = 0.35,
    ):
        self.placeholder_total = placeholder_total
        self.min_directories_for_ratio = min_directories_for_ratio
        self.upward_smoothing = upward_smoothing
        self.downward_smoothing = downward_smoothing
        self._estimated_total = None

    def update(self, progress: ScanProgress) -> ScanEstimate:
        """Return a smoothed estimate for the latest scan progress snapshot."""
        completed = progress.files_scanned
        if progress.is_finished:
            self._estimated_total = float(completed)
            return ScanEstimate(
                completed=completed,
                total=completed,
                is_estimating=False,
            )

        raw_total, is_estimating = self._raw_total(progress)
        if self._estimated_total is None:
            smoothed_total = raw_total
        elif raw_total >= self._estimated_total:
            smoothed_total = self._smooth(raw_total, self.upward_smoothing)
        else:
            smoothed_total = self._smooth(raw_total, self.downward_smoothing)

        total = max(math.ceil(smoothed_total), completed + 1)
        self._estimated_total = float(total)
        return ScanEstimate(
            completed=completed,
            total=total,
            is_estimating=is_estimating,
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
