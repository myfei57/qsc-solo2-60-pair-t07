"""Console projection for the metering calibration package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MeteringState:
    """Counts the console renders for calibration management."""

    meters: int
    active_meters: int
    versions: int
    calibrations_open: int
    calibrations_failed: int
    replacements_open: int
    replacements_failed: int
    readings: int
    settlements_open: int
    settlements_finalized: int

    def as_dict(self) -> dict[str, int]:
        return {
            "meters": self.meters,
            "active_meters": self.active_meters,
            "versions": self.versions,
            "calibrations_open": self.calibrations_open,
            "calibrations_failed": self.calibrations_failed,
            "replacements_open": self.replacements_open,
            "replacements_failed": self.replacements_failed,
            "readings": self.readings,
            "settlements_open": self.settlements_open,
            "settlements_finalized": self.settlements_finalized,
        }
