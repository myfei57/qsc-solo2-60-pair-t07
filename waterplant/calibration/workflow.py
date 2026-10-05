"""Calibration and meter-replacement workflows with recoverable steps.

Both workflows run the same four idempotent steps:

1. ``stage``    – persist the new coefficient as a PENDING version and pin the
                  planned effective time on the workflow record. The planned
                  time follows the chosen policy: IMMEDIATE takes ``now``,
                  SHIFT_BOUNDARY snaps to the next shift boundary.
2. ``close``    – close the currently effective version at the planned time.
3. ``activate`` – flip the staged version to EFFECTIVE and sync the dosing
                  factor, so chemical dosing follows the version in force.
4. ``finalize`` – mark the workflow record EFFECTIVE and write the audit entry.

A failure anywhere is a partial failure: completed steps stay committed and
``recover`` re-runs the remaining ones. Every step is safe to re-run, so
recovery converges to the same state without duplicating versions, intervals
or audit entries. State is written before its audit entry, so a crash can
lose an audit line but can never duplicate one.
"""

from __future__ import annotations

import json
from typing import Callable

from waterplant.audit import Auditor
from waterplant.flow.calibration import Calibration
from waterplant.flow.report import validate_factor
from waterplant.store.store import Store

from .core import (
    InjectedFault,
    InvalidEffectiveTime,
    MeterRetired,
    MeteringError,
    ShiftSchedule,
    WorkflowStateError,
    load_records,
    new_id,
    save_records,
    utcnow,
)
from .registry import MeterRegistry, VersionBook

CALIBRATIONS_KEY = "metering:calibrations"
REPLACEMENTS_KEY = "metering:replacements"

POLICIES = ("IMMEDIATE", "SHIFT_BOUNDARY")
OPEN_STATUSES = ("VALIDATED", "APPLYING")


class _Workflow:
    """Shared step machine: run idempotent steps, mark FAILED on error."""

    _key = ""
    _id_field = ""
    _kind = ""

    def __init__(self, store: Store, auditor: Auditor, shifts: ShiftSchedule | None) -> None:
        self._store = store
        self._auditor = auditor
        self._meters = MeterRegistry(store)
        self._versions = VersionBook(store)
        self._shifts = shifts or ShiftSchedule()

    # -- record helpers -------------------------------------------------

    def all(self) -> list[dict]:
        return load_records(self._store, self._key)

    def get(self, record_id: str) -> dict:
        for record in self.all():
            if record[self._id_field] == record_id:
                return record
        raise WorkflowStateError(f"unknown {self._kind} {record_id}")

    def _update(self, record_id: str, now: str, **fields: object) -> None:
        records = load_records(self._store, self._key)
        for record in records:
            if record[self._id_field] == record_id:
                record.update(fields)
                record["updated_at"] = now
        save_records(self._store, self._key, records)

    def _audit(self, kind: str, payload: dict) -> None:
        self._auditor.record(kind, json.dumps(payload, ensure_ascii=False, sort_keys=True))

    # -- step machine ----------------------------------------------------

    def _execute(
        self,
        record_id: str,
        steps: dict[str, Callable[[dict, str], None]],
        now: str,
        fail_at: tuple[str, ...],
    ) -> dict:
        record = self.get(record_id)
        if record["status"] == "EFFECTIVE":
            return record
        if record["status"] == "FAILED":
            raise WorkflowStateError(
                f"{self._kind} {record_id} is FAILED; call recover() to resume it"
            )
        self._update(record_id, now, status="APPLYING")
        try:
            for name, step in steps.items():
                if name in fail_at:
                    raise InjectedFault(f"injected fault at step {name}")
                record = self.get(record_id)
                step(record, now)
        except Exception as exc:
            self._update(record_id, now, status="FAILED", failure=str(exc))
            self._audit(f"{self._kind}.failed", {self._id_field: record_id, "error": str(exc)})
            raise
        return self.get(record_id)

    def _recover(self, record_id: str, steps, now: str, fail_at: tuple[str, ...]) -> dict:
        record = self.get(record_id)
        if record["status"] == "EFFECTIVE":
            return record
        if record["status"] == "FAILED":
            self._update(record_id, now, status="APPLYING", failure=None)
            self._audit(f"{self._kind}.recovered", {self._id_field: record_id})
        return self._execute(record_id, steps, now, fail_at)

    def _planned_from(self, policy: str, now: str) -> str:
        if policy == "IMMEDIATE":
            return now
        if policy == "SHIFT_BOUNDARY":
            return self._shifts.next_boundary(now)
        raise MeteringError(f"unknown effective policy {policy}")

    def _step_close(self, meter_id: str, planned_from: str, version_id: str) -> None:
        self._versions.close_current(meter_id, planned_from, exclude=version_id)

    def _step_activate(self, record: dict, now: str) -> None:
        version, changed = self._versions.activate(record["version_id"])
        if changed:
            # Dosing reads the legacy single factor; keep it on the version in
            # force so the dose tracks the meter actually measuring.
            Calibration(self._store).replace(version["factor"])
            self._audit(
                "version.activated",
                {
                    "version_id": version["version_id"],
                    "meter_id": version["meter_id"],
                    "factor": version["factor"],
                    "source": version["source"],
                    "source_id": version["source_id"],
                },
            )


class CalibrationWorkflow(_Workflow):
    """Four-step calibration: stage, close, activate, finalize."""

    _key = CALIBRATIONS_KEY
    _id_field = "calibration_id"
    _kind = "calibration"

    def start(
        self,
        meter_id: str,
        new_factor: float,
        *,
        policy: str = "IMMEDIATE",
        request_id: str = "",
        reference: str = "",
        operator: str = "",
        now: str = "",
    ) -> dict:
        """Open a calibration record, idempotent by ``request_id``."""

        now = now or utcnow()
        request_id = request_id or f"cal:{new_id()}"
        for record in self.all():
            if record["request_id"] == request_id:
                return record
        meter = self._meters.get(meter_id)
        if meter["status"] != "ACTIVE":
            raise MeterRetired(f"meter {meter_id} is retired")
        validate_factor(new_factor)
        if policy not in POLICIES:
            raise MeteringError(f"unknown effective policy {policy}")
        for record in self.all():
            if record["meter_id"] == meter_id and record["status"] in OPEN_STATUSES:
                raise WorkflowStateError(
                    f"meter {meter_id} already has calibration {record['calibration_id']} in flight"
                )
        record = {
            "calibration_id": new_id(),
            "request_id": request_id,
            "meter_id": meter_id,
            "new_factor": new_factor,
            "reference": reference,
            "operator": operator,
            "policy": policy,
            "planned_from": None,
            "version_id": None,
            "status": "VALIDATED",
            "failure": None,
            "created_at": now,
            "updated_at": now,
        }
        records = self.all()
        records.append(record)
        save_records(self._store, self._key, records)
        self._audit(
            "calibration.created",
            {
                "calibration_id": record["calibration_id"],
                "meter_id": meter_id,
                "new_factor": new_factor,
                "policy": policy,
                "request_id": request_id,
            },
        )
        return record

    def apply(self, calibration_id: str, *, now: str = "", fail_at: tuple[str, ...] = ()) -> dict:
        now = now or utcnow()
        steps = {
            "stage": self._step_stage,
            "close": self._step_close_current,
            "activate": self._step_activate,
            "finalize": self._step_finalize,
        }
        return self._execute(calibration_id, steps, now, fail_at)

    def recover(self, calibration_id: str, *, now: str = "", fail_at: tuple[str, ...] = ()) -> dict:
        now = now or utcnow()
        steps = {
            "stage": self._step_stage,
            "close": self._step_close_current,
            "activate": self._step_activate,
            "finalize": self._step_finalize,
        }
        return self._recover(calibration_id, steps, now, fail_at)

    # -- steps -------------------------------------------------------------

    def _step_stage(self, record: dict, now: str) -> None:
        if record["version_id"]:
            return
        staged = self._versions.staged_for("CALIBRATION", record["calibration_id"])
        if staged is None:
            planned = self._planned_from(record["policy"], now)
            current = self._versions.current(record["meter_id"])
            if current is not None and planned <= current["effective_from"]:
                raise InvalidEffectiveTime(
                    "effective time must be after the current version start; "
                    "history is immutable"
                )
            staged = self._versions.stage(
                record["meter_id"],
                record["new_factor"],
                planned,
                "CALIBRATION",
                record["calibration_id"],
                now,
            )
        planned = staged["effective_from"]
        self._update(
            record["calibration_id"], now, version_id=staged["version_id"], planned_from=planned
        )
        self._audit(
            "calibration.staged",
            {
                "calibration_id": record["calibration_id"],
                "version_id": staged["version_id"],
                "planned_from": planned,
            },
        )

    def _step_close_current(self, record: dict, now: str) -> None:
        if not record["planned_from"] or not record["version_id"]:
            raise WorkflowStateError("calibration has no staged version to close against")
        self._step_close(record["meter_id"], record["planned_from"], record["version_id"])

    def _step_finalize(self, record: dict, now: str) -> None:
        self._update(record["calibration_id"], now, status="EFFECTIVE")
        self._audit(
            "calibration.applied",
            {
                "calibration_id": record["calibration_id"],
                "meter_id": record["meter_id"],
                "version_id": record["version_id"],
                "factor": record["new_factor"],
                "effective_from": record["planned_from"],
            },
        )


class ReplacementWorkflow(_Workflow):
    """Four-step meter replacement: stage, close, activate, finalize."""

    _key = REPLACEMENTS_KEY
    _id_field = "replacement_id"
    _kind = "replacement"

    def start(
        self,
        old_meter_id: str,
        new_serial: str,
        initial_factor: float,
        *,
        policy: str = "IMMEDIATE",
        request_id: str = "",
        operator: str = "",
        now: str = "",
    ) -> dict:
        """Open a replacement record, idempotent by ``request_id``."""

        now = now or utcnow()
        request_id = request_id or f"rep:{new_id()}"
        for record in self.all():
            if record["request_id"] == request_id:
                return record
        old = self._meters.get(old_meter_id)
        if old["status"] != "ACTIVE":
            raise MeterRetired(f"meter {old_meter_id} is retired")
        validate_factor(initial_factor)
        if policy not in POLICIES:
            raise MeteringError(f"unknown effective policy {policy}")
        if self._meters.find_by_serial(new_serial) is not None:
            raise MeteringError(f"serial {new_serial} is already registered")
        for record in self.all():
            if record["old_meter_id"] == old_meter_id and record["status"] in OPEN_STATUSES:
                raise WorkflowStateError(
                    f"meter {old_meter_id} already has replacement {record['replacement_id']} in flight"
                )
        record = {
            "replacement_id": new_id(),
            "request_id": request_id,
            "old_meter_id": old_meter_id,
            "new_meter_id": None,
            "new_serial": new_serial,
            "initial_factor": initial_factor,
            "operator": operator,
            "policy": policy,
            "planned_from": None,
            "version_id": None,
            "status": "VALIDATED",
            "failure": None,
            "created_at": now,
            "updated_at": now,
        }
        records = self.all()
        records.append(record)
        save_records(self._store, self._key, records)
        self._audit(
            "replacement.created",
            {
                "replacement_id": record["replacement_id"],
                "old_meter_id": old_meter_id,
                "new_serial": new_serial,
                "initial_factor": initial_factor,
                "policy": policy,
                "request_id": request_id,
            },
        )
        return record

    def apply(self, replacement_id: str, *, now: str = "", fail_at: tuple[str, ...] = ()) -> dict:
        now = now or utcnow()
        steps = {
            "stage": self._step_stage,
            "close": self._step_close_old,
            "activate": self._step_activate,
            "finalize": self._step_finalize,
        }
        return self._execute(replacement_id, steps, now, fail_at)

    def recover(self, replacement_id: str, *, now: str = "", fail_at: tuple[str, ...] = ()) -> dict:
        now = now or utcnow()
        steps = {
            "stage": self._step_stage,
            "close": self._step_close_old,
            "activate": self._step_activate,
            "finalize": self._step_finalize,
        }
        return self._recover(replacement_id, steps, now, fail_at)

    # -- steps -------------------------------------------------------------

    def _step_stage(self, record: dict, now: str) -> None:
        if record["version_id"]:
            return
        staged = self._versions.staged_for("REPLACEMENT", record["replacement_id"])
        if staged is None:
            planned = self._planned_from(record["policy"], now)
            current = self._versions.current(record["old_meter_id"])
            if current is not None and planned <= current["effective_from"]:
                raise InvalidEffectiveTime(
                    "effective time must be after the current version start; "
                    "history is immutable"
                )
            meter = self._meters.register(record["new_serial"], now=now)
            staged = self._versions.stage(
                meter["meter_id"],
                record["initial_factor"],
                planned,
                "REPLACEMENT",
                record["replacement_id"],
                now,
            )
        meter_id = staged["meter_id"]
        planned = staged["effective_from"]
        self._update(
            record["replacement_id"],
            now,
            new_meter_id=meter_id,
            version_id=staged["version_id"],
            planned_from=planned,
        )
        self._audit(
            "replacement.staged",
            {
                "replacement_id": record["replacement_id"],
                "new_meter_id": meter_id,
                "version_id": staged["version_id"],
                "planned_from": planned,
            },
        )

    def _step_close_old(self, record: dict, now: str) -> None:
        if not record["planned_from"]:
            raise WorkflowStateError("replacement has no staged version to close against")
        self._versions.close_current(record["old_meter_id"], record["planned_from"])
        self._meters.retire(record["old_meter_id"], record["planned_from"])

    def _step_finalize(self, record: dict, now: str) -> None:
        self._update(record["replacement_id"], now, status="EFFECTIVE")
        self._audit(
            "replacement.applied",
            {
                "replacement_id": record["replacement_id"],
                "old_meter_id": record["old_meter_id"],
                "new_meter_id": record["new_meter_id"],
                "version_id": record["version_id"],
                "factor": record["initial_factor"],
                "effective_from": record["planned_from"],
            },
        )
