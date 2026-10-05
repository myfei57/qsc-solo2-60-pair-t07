"""Facade wiring the metering components for the console runtime."""

from __future__ import annotations

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .pipeline import READINGS_KEY, ReadingLog, SettlementBook
from .core import ShiftSchedule, load_records
from .registry import MeterRegistry, VersionBook
from .report import MeteringState
from .workflow import CalibrationWorkflow, ReplacementWorkflow


class Metering:
    """Calibration management: archive, versions, workflows and settlement."""

    def __init__(
        self,
        store: Store,
        shifts: ShiftSchedule | None = None,
        compat_window_hours: int = 24,
    ) -> None:
        self._store = store
        auditor = Auditor(store)
        self.meters = MeterRegistry(store)
        self.versions = VersionBook(store)
        self.readings = ReadingLog(store, auditor, compat_window_hours)
        self.settlements = SettlementBook(store, auditor)
        self.calibrations = CalibrationWorkflow(store, auditor, shifts)
        self.replacements = ReplacementWorkflow(store, auditor, shifts)

    def state(self) -> MeteringState:
        meters = self.meters.all()
        calibrations = self.calibrations.all()
        replacements = self.replacements.all()
        settlements = self.settlements.all()
        return MeteringState(
            meters=len(meters),
            active_meters=sum(1 for meter in meters if meter["status"] == "ACTIVE"),
            versions=len(self.versions.all()),
            calibrations_open=sum(
                1 for item in calibrations if item["status"] in ("VALIDATED", "APPLYING")
            ),
            calibrations_failed=sum(
                1 for item in calibrations if item["status"] == "FAILED"
            ),
            replacements_open=sum(
                1 for item in replacements if item["status"] in ("VALIDATED", "APPLYING")
            ),
            replacements_failed=sum(
                1 for item in replacements if item["status"] == "FAILED"
            ),
            readings=len(load_records(self._store, READINGS_KEY)),
            settlements_open=sum(1 for item in settlements if item["status"] == "OPEN"),
            settlements_finalized=sum(
                1 for item in settlements if item["status"] == "FINALIZED"
            ),
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"metering meters={state.meters} active={state.active_meters} "
            f"versions={state.versions} calibrations(open={state.calibrations_open} "
            f"failed={state.calibrations_failed}) replacements(open={state.replacements_open} "
            f"failed={state.replacements_failed}) readings={state.readings} "
            f"settlements(open={state.settlements_open} finalized={state.settlements_finalized})"
        )
