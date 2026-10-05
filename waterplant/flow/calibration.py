"""Bound meter archives, immutable coefficient versions, and effective times."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from waterplant.audit import Auditor
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .persistence import (
    append_json,
    has_json,
    load_json_list,
    load_json_object,
    save_json_object,
    update_json_list,
)
from .report import (
    CalibState,
    CalibratedValue,
    CalibrationJob,
    ChangeOrder,
    FactorVersion,
    MeterProfile,
    RawReading,
    Resolution,
    SettlementValue,
    ThreeTierReading,
    factor_from_samples,
    validate_factor,
    validate_mode,
    validate_serial,
)

CALIBRATION_KEY = "flow:calibration"
METER_KEY = "flow:calibration:meters"
VERSION_KEY = "flow:calibration:versions"
CHANGE_KEY = "flow:calibration:changes"
ACTIVATION_KEY = "flow:calibration:activations"
JOB_KEY = "flow:calibration:jobs"
IDEMPOTENCY_KEY = "flow:calibration:requests"
RAW_KEY = "flow:calibration:raw-readings"
CALIBRATED_KEY = "flow:calibration:calibrated-values"
SETTLEMENT_KEY = "flow:calibration:settlement-values"

DEFAULT_FACTOR = 1.0
DEFAULT_BOUNDARY = "08:00"
IMMEDIATE_COMPATIBILITY_SECONDS = 4 * 60 * 60
BASELINE_ID = "factor-baseline"
BASELINE_METER = "legacy-meter"
JOB_STAGES = (
    "validate",
    "register_meter",
    "create_version",
    "create_change",
    "wait_for_effective_time",
    "activate",
    "audit_activation",
    "settle_pending",
    "complete",
)


def now_timestamp() -> int:
    return int(time.time())


def make_id(prefix: str, *parts: object) -> str:
    if parts:
        digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]
        return f"{prefix}-{digest}"
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def parse_shift_boundary(value: str) -> str:
    """Validate and normalise an HH:MM shift boundary."""

    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("shift_boundary must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("shift_boundary must use HH:MM")
    return f"{hour:02d}:{minute:02d}"


def resolve_effective_time(
    mode: str,
    requested_at: int,
    effective_at: int | None,
    shift_boundary: str = DEFAULT_BOUNDARY,
) -> int:
    """Resolve an operation's fixed effective instant."""

    validate_mode(mode)
    if mode == "immediate":
        return int(effective_at if effective_at is not None else requested_at)
    if effective_at is not None and effective_at <= requested_at:
        raise ValueError("shift boundary effective time must be in the future")
    boundary = parse_shift_boundary(shift_boundary)
    if effective_at is not None and effective_at > requested_at:
        return int(effective_at)
    hour, minute = (int(part) for part in boundary.split(":", 1))
    candidate = time.gmtime(requested_at)
    boundary_today = int(
        time.mktime(
            (candidate.tm_year, candidate.tm_mon, candidate.tm_mday, hour, minute, 0, 0, 0, 0)
        )
    )
    # time.mktime interprets local time; subtract tm_gmtoff to obtain UTC epoch.
    boundary_today -= candidate.tm_gmtoff
    return boundary_today if boundary_today > requested_at else boundary_today + 24 * 60 * 60


def _meter_from_payload(payload: dict[str, Any]) -> MeterProfile:
    return MeterProfile(
        serial=str(payload["serial"]),
        model=str(payload.get("model", "")),
        manufacturer=str(payload.get("manufacturer", "")),
        location=str(payload.get("location", "")),
        commissioned_at=int(payload.get("commissioned_at", 0)),
        retired_at=None if payload.get("retired_at") is None else int(payload["retired_at"]),
        attributes=dict(payload.get("attributes", {})),
    )


def _version_from_payload(payload: dict[str, Any]) -> FactorVersion:
    return FactorVersion(
        version_id=str(payload["version_id"]),
        meter_serial=str(payload["meter_serial"]),
        factor=float(payload["factor"]),
        basis=str(payload.get("basis", "manual")),
        samples=[dict(sample) for sample in payload.get("samples", [])],
        created_at=int(payload.get("created_at", 0)),
        effective_at=int(payload.get("effective_at", 0)),
        superseded_at=None if payload.get("superseded_at") is None else int(payload["superseded_at"]),
        retired_at=None if payload.get("retired_at") is None else int(payload["retired_at"]),
        change_id=str(payload.get("change_id", "")),
    )


def _change_from_payload(payload: dict[str, Any]) -> ChangeOrder:
    return ChangeOrder(
        change_id=str(payload["change_id"]),
        meter_serial=str(payload["meter_serial"]),
        version_id=str(payload["version_id"]),
        previous_version_id=str(payload.get("previous_version_id", "")),
        mode=str(payload.get("mode", "immediate")),
        requested_at=int(payload.get("requested_at", 0)),
        effective_at=int(payload.get("effective_at", 0)),
        shift_boundary=str(payload.get("shift_boundary", DEFAULT_BOUNDARY)),
        status=str(payload.get("status", "pending")),
        request_id=str(payload.get("request_id", "")),
        compatibility_until=int(payload.get("compatibility_until", 0)),
        completed_at=None if payload.get("completed_at") is None else int(payload["completed_at"]),
    )


def _job_from_payload(payload: dict[str, Any]) -> CalibrationJob:
    return CalibrationJob(
        job_id=str(payload["job_id"]),
        request_id=str(payload.get("request_id", "")),
        serial=str(payload["serial"]),
        model=str(payload.get("model", "")),
        manufacturer=str(payload.get("manufacturer", "")),
        location=str(payload.get("location", "")),
        mode=str(payload.get("mode", "immediate")),
        shift_boundary=str(payload.get("shift_boundary", DEFAULT_BOUNDARY)),
        requested_at=int(payload.get("requested_at", 0)),
        samples=[dict(sample) for sample in payload.get("samples", [])],
        factor=float(payload.get("factor", 0.0)),
        fail_before=str(payload.get("fail_before", "")),
        last_stage=str(payload.get("last_stage", "")),
        status=str(payload.get("status", "pending")),
        version_id=str(payload.get("version_id", "")),
        change_id=str(payload.get("change_id", "")),
        audit_ids=dict(payload.get("audit_ids", {})),
        effective_at=int(payload.get("effective_at", 0)),
        completed_at=None if payload.get("completed_at") is None else int(payload["completed_at"]),
    )


def _raw_from_payload(payload: dict[str, Any]) -> RawReading:
    return RawReading(
        reading_id=str(payload["reading_id"]),
        observed_at=int(payload["observed_at"]),
        raw_value=float(payload["raw_value"]),
        meter_serial=str(payload.get("meter_serial", "")),
        source_version_id=str(payload.get("source_version_id", "")),
        accepted_version_ids=[str(item) for item in payload.get("accepted_version_ids", [])],
        quality=str(payload.get("quality", "good")),
        request_id=str(payload.get("request_id", "")),
    )


def _calibrated_from_payload(payload: dict[str, Any]) -> CalibratedValue:
    return CalibratedValue(
        reading_id=str(payload["reading_id"]),
        version_id=str(payload["version_id"]),
        meter_serial=str(payload.get("meter_serial", "")),
        factor=float(payload["factor"]),
        calibrated_value=float(payload["calibrated_value"]),
    )


def _settlement_from_payload(payload: dict[str, Any]) -> SettlementValue:
    return SettlementValue(
        reading_id=str(payload["reading_id"]),
        version_id=str(payload["version_id"]),
        factor=float(payload["factor"]),
        calibrated_value=float(payload["calibrated_value"]),
        settlement_value=float(payload["settlement_value"]),
        basis=str(payload.get("basis", "official")),
        state=str(payload.get("state", "settled")),
    )


class CalibrationManager:
    """Application service for meter archives, versions, replacement and readings."""

    def __init__(self, store: Store, auditor: Auditor | None = None) -> None:
        self._store = store
        self._auditor = auditor if auditor is not None else Auditor(store)

    def ensure_baseline(self) -> FactorVersion:
        """Create the idempotent baseline archive used by unmanaged legacy state."""

        existing = self.version(BASELINE_ID)
        if existing is not None:
            return existing
        legacy, present = load_float(self._store, CALIBRATION_KEY)
        factor = legacy if present and legacy > 0 else DEFAULT_FACTOR
        validate_factor(factor)
        timestamp = now_timestamp()
        meter = MeterProfile(
            serial=BASELINE_METER,
            model="legacy",
            manufacturer="unknown",
            location="",
            commissioned_at=timestamp,
        )
        if self.meter(BASELINE_METER) is None:
            append_json(self._store, METER_KEY, meter.as_dict())
        version = FactorVersion(
            version_id=BASELINE_ID,
            meter_serial=BASELINE_METER,
            factor=factor,
            basis="legacy",
            samples=[],
            created_at=timestamp,
            effective_at=0,
        )
        append_json(self._store, VERSION_KEY, version.as_dict())
        append_json(self._store, ACTIVATION_KEY, {
            "change_id": "baseline",
            "version_id": BASELINE_ID,
            "activated_at": 0,
            "previous_version_id": "",
        })
        return version

    def register_meter(
        self,
        serial: str,
        model: str = "",
        manufacturer: str = "",
        location: str = "",
        commissioned_at: int | None = None,
        attributes: dict[str, object] | None = None,
        request_id: str = "",
    ) -> MeterProfile:
        validate_serial(serial)
        if request_id:
            completed = self._completed_request(request_id)
            if completed:
                meter = self.meter(serial)
                if meter is not None:
                    return meter
        existing = self.meter(serial)
        if existing is not None:
            return existing
        meter = MeterProfile(
            serial=serial,
            model=model,
            manufacturer=manufacturer,
            location=location,
            commissioned_at=commissioned_at or now_timestamp(),
            attributes=dict(attributes or {}),
        )
        append_json(self._store, METER_KEY, meter.as_dict())
        self._audit_once(
            "meter_archive",
            f"{serial} model={model} location={location}",
            serial,
            "register",
        )
        self._remember_request(request_id, {"kind": "meter", "serial": serial})
        return meter

    def calibrate(
        self,
        serial: str,
        samples: list[dict[str, float]],
        basis: str = "samples",
        factor: float | None = None,
        version_id: str = "",
        created_at: int | None = None,
        request_id: str = "",
    ) -> FactorVersion:
        """Create an immutable draft factor version without activating it."""

        validate_serial(serial)
        if self.meter(serial) is None:
            self.register_meter(serial)
        calculated = factor_from_samples(samples) if factor is None else float(factor)
        validate_factor(calculated)
        version_id = version_id or make_id(
            "factor", serial, calculated, request_id or uuid.uuid4().hex
        )
        if request_id:
            completed = self._completed_request(request_id)
            if completed:
                existing = self.version(version_id)
                if existing is not None:
                    return existing
        existing = self.version(version_id)
        if existing is not None:
            return existing
        version = FactorVersion(
            version_id=version_id,
            meter_serial=serial,
            factor=calculated,
            basis=basis,
            samples=[dict(sample) for sample in samples],
            created_at=created_at or now_timestamp(),
            effective_at=0,
        )
        append_json(self._store, VERSION_KEY, version.as_dict())
        self._audit_once(
            "calibration",
            f"{serial} factor={calculated:.4f} version={version_id} basis={basis}",
            version_id,
            "draft",
        )
        self._remember_request(request_id, {"kind": "version", "version_id": version_id})
        return version

    def replace_meter(
        self,
        serial: str,
        factor: float,
        mode: str = "immediate",
        effective_at: int | None = None,
        shift_boundary: str = DEFAULT_BOUNDARY,
        model: str = "",
        manufacturer: str = "",
        location: str = "",
        samples: list[dict[str, float]] | None = None,
        compatibility_until: int | None = None,
        request_id: str = "",
    ) -> ChangeOrder:
        """Archive a meter/version and bind it to a fixed replacement effective time."""

        validate_serial(serial)
        validate_factor(factor)
        validate_mode(mode)
        boundary = parse_shift_boundary(shift_boundary) if mode == "shift_boundary" else boundary_value(shift_boundary)
        request_time = now_timestamp()
        if request_id:
            completed = self._ensure_request_identity(
                request_id, "change", {"serial": serial, "factor": factor}
            )
            if completed and completed.get("kind") == "change":
                existing = self.change(str(completed.get("change_id", "")))
                if existing is not None:
                    return existing
        if self.meter(serial) is None:
            self.register_meter(
                serial, model=model, manufacturer=manufacturer, location=location,
                commissioned_at=request_time, request_id=request_id + ":meter" if request_id else "",
            )
        current_line = self.current_version(None, request_time)
        previous_id = "" if current_line is None else current_line.version_id
        fixed_effective = resolve_effective_time(mode, request_time, effective_at, boundary)
        version_id = make_id("factor", serial, factor, fixed_effective, request_id)
        existing = self.version(version_id)
        if existing is None:
            version = FactorVersion(
                version_id=version_id,
                meter_serial=serial,
                factor=factor,
                basis="replacement",
                samples=[dict(sample) for sample in (samples or [])],
                created_at=request_time,
                effective_at=fixed_effective,
                change_id="",
            )
            append_json(self._store, VERSION_KEY, version.as_dict())
        change_id = make_id("change", serial, version_id, request_id)
        existing_change = self.change(change_id)
        if existing_change is not None:
            return existing_change
        pending = [
            item for item in self.pending_changes(None, request_time)
            if item.effective_at == fixed_effective
        ]
        if pending:
            raise ValueError("a pending replacement already uses this effective time")
        immediate_compatibility = (
            compatibility_until
            if compatibility_until is not None
            else fixed_effective + IMMEDIATE_COMPATIBILITY_SECONDS
        )
        change = ChangeOrder(
            change_id=change_id,
            meter_serial=serial,
            version_id=version_id,
            previous_version_id=previous_id,
            mode=mode,
            requested_at=request_time,
            effective_at=fixed_effective,
            shift_boundary=boundary,
            status="scheduled",
            request_id=request_id,
            compatibility_until=immediate_compatibility,
            completed_at=request_time if mode == "immediate" else None,
        )
        append_json(self._store, CHANGE_KEY, change.as_dict())
        if mode == "immediate":
            self._activate_change(change, request_time)
            self._audit_once(
                "meter_replace",
                f"{serial} factor={factor:.4f} version={version_id} mode=immediate",
                change_id,
                "replace",
            )
        self._remember_request(
            request_id,
            {"kind": "change", "change_id": change_id, "serial": serial, "factor": factor},
        )
        return self.change(change_id) or change

    def activate_due(self, at: int | None = None) -> list[ChangeOrder]:
        """Activate every scheduled order whose shift boundary has arrived."""

        timestamp = at if at is not None else now_timestamp()
        activated: list[ChangeOrder] = []
        for change in self.changes():
            if change.status != "scheduled":
                continue
            if change.effective_at > timestamp:
                continue
            activated.append(self._activate_change(change, timestamp))
        return activated

    def meters(self) -> list[MeterProfile]:
        return [_meter_from_payload(item) for item in load_json_list(self._store, METER_KEY)]

    def meter(self, serial: str) -> MeterProfile | None:
        for meter in self.meters():
            if meter.serial == serial:
                return meter
        return None

    def versions(self, serial: str | None = None) -> list[FactorVersion]:
        versions = [_version_from_payload(item) for item in load_json_list(self._store, VERSION_KEY)]
        if serial is not None:
            versions = [version for version in versions if version.meter_serial == serial]
        return sorted(versions, key=lambda item: (item.created_at, item.version_id))

    def version(self, version_id: str) -> FactorVersion | None:
        for version in self.versions():
            if version.version_id == version_id:
                return version
        return None

    def changes(self, serial: str | None = None) -> list[ChangeOrder]:
        changes = [_change_from_payload(item) for item in load_json_list(self._store, CHANGE_KEY)]
        if serial is not None:
            changes = [change for change in changes if change.meter_serial == serial]
        return sorted(changes, key=lambda item: (item.requested_at, item.change_id))

    def change(self, change_id: str) -> ChangeOrder | None:
        for change in self.changes():
            if change.change_id == change_id:
                return change
        return None

    def pending_changes(self, serial: str | None = None, at: int | None = None) -> list[ChangeOrder]:
        timestamp = at if at is not None else now_timestamp()
        return [
            change
            for change in self.changes(serial)
            if change.status == "scheduled" and change.effective_at > timestamp
        ]

    def activations(self, serial: str | None = None) -> list[dict[str, Any]]:
        activations = load_json_list(self._store, ACTIVATION_KEY)
        if serial is None:
            return activations
        return [item for item in activations if item.get("meter_serial") == serial]

    def current_version(self, serial: str | None = None, at: int | None = None) -> FactorVersion | None:
        """Official version in effect at an instant.

        With no serial the manager follows the physical meter installed on the
        single logical flow line. Supplying a serial limits the answer to that
        archive record.
        """

        timestamp = at if at is not None else now_timestamp()
        activated_version_ids = {
            str(item.get("version_id"))
            for item in load_json_list(self._store, ACTIVATION_KEY)
        }
        candidates = [
            version
            for version in self.versions(serial)
            if version.version_id in activated_version_ids
            and version.effective_at <= timestamp
            and (version.superseded_at is None or timestamp < version.superseded_at)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.effective_at, item.created_at, item.version_id))

    def current(self, at: int | None = None) -> float:
        """Backwards compatible current multiplier, seeding the archive on first use."""

        legacy, present = load_float(self._store, CALIBRATION_KEY)
        has_archive = bool(load_json_list(self._store, ACTIVATION_KEY))
        if present and not has_archive:
            validate_factor(legacy)
            return legacy
        version = self.current_version(None, at)
        if version is None:
            version = self.ensure_baseline()
        return version.factor

    def replace(self, factor: float) -> None:
        """Backwards compatible immediate replacement used by legacy callers."""

        validate_factor(factor)
        self.replace_meter(BASELINE_METER, factor, mode="immediate", model="legacy")

    def staged_version(self, serial: str, at: int | None = None) -> FactorVersion | None:
        timestamp = at if at is not None else now_timestamp()
        for change in self.pending_changes(serial, timestamp):
            return self.version(change.version_id)
        return None

    def _staged_line_version(self, at: int) -> FactorVersion | None:
        for change in self.pending_changes(None, at):
            version = self.version(change.version_id)
            if version is not None:
                return version
        return None

    def _official_version(self, observed_at: int) -> FactorVersion | None:
        """Official coefficient for a reading time, including superseded history."""

        activated_version_ids = {
            str(item.get("version_id"))
            for item in load_json_list(self._store, ACTIVATION_KEY)
        }
        candidates = [
            version
            for version in self.versions(None)
            if version.version_id in activated_version_ids
            and version.effective_at <= observed_at
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.effective_at, item.created_at, item.version_id))

    def resolve_reading(
        self,
        observed_at: int,
        meter_serial: str = "",
        source_version_id: str = "",
        settlement_version_id: str = "",
        at: int | None = None,
    ) -> Resolution:
        """Resolve reader source, accepted versions, and the fixed settlement version."""

        timestamp = at if at is not None else now_timestamp()
        serial = meter_serial or BASELINE_METER
        source: FactorVersion | None = None
        if source_version_id:
            source = self.version(source_version_id)
            if source is None:
                raise ValueError(f"unknown source version {source_version_id}")
        elif meter_serial:
            source = self._source_for_serial(meter_serial, observed_at, timestamp)
        else:
            source = self.current_version(None, observed_at)

        staged = self._staged_line_version(timestamp)
        if staged is None and meter_serial:
            staged = self.staged_version(serial, timestamp)
        previous: FactorVersion | None = None
        if staged is not None:
            staged_change = self._change_for_version(staged.version_id)
            if staged_change is not None:
                previous = self.version(staged_change.previous_version_id)

        if settlement_version_id:
            settlement = self.version(settlement_version_id)
            if settlement is None:
                raise ValueError(f"unknown settlement version {settlement_version_id}")
        elif staged is not None and previous is not None and observed_at < staged.effective_at:
            # Scheduled replacement: every pre-boundary reading settles with the
            # old coefficient, regardless of which physical meter sent it.
            settlement = previous
        else:
            settlement = self._official_version(observed_at)
            if settlement is None and staged is not None and observed_at >= staged.effective_at:
                settlement = staged

        current_line = self.current_version(None, timestamp)
        current_change = None if current_line is None else self._change_for_version(current_line.version_id)
        if current_line is not None and current_change is not None:
            candidate_previous = self.version(current_change.previous_version_id)
            if (
                candidate_previous is not None
                and current_line.effective_at <= observed_at <= current_change.compatibility_until
            ):
                previous = candidate_previous
        accepted: list[FactorVersion] = []
        for candidate in (source, previous, settlement, staged):
            if candidate is not None and candidate.version_id not in {item.version_id for item in accepted}:
                accepted.append(candidate)
        ambiguity = False
        reason = "settled"
        if (
            source is not None
            and settlement is not None
            and source.version_id != settlement.version_id
        ):
            if staged is not None and observed_at < staged.effective_at:
                reason = "shift boundary pending; old coefficient settles, new coefficient is reader compatible"
            else:
                reason = "coexistence window; official settlement coefficient is fixed"
            ambiguity = True
        if settlement is None:
            reason = "pending official coefficient"
            if staged is not None:
                reason += "; staged coefficient accepted by reader"
        return Resolution(
            source_version=source,
            settlement_version=settlement,
            accepted_versions=accepted,
            ambiguity=ambiguity,
            reason=reason,
        )

    def record_reading(
        self,
        raw_value: float,
        observed_at: int | None = None,
        meter_serial: str = "",
        source_version_id: str = "",
        settlement_version_id: str = "",
        reading_id: str = "",
        quality: str = "good",
        request_id: str = "",
        at: int | None = None,
    ) -> ThreeTierReading:
        if raw_value < 0:
            raise ValueError("raw flow must be non-negative")
        timestamp = at if at is not None else now_timestamp()
        observed = observed_at if observed_at is not None else timestamp
        if reading_id:
            existing = self.reading(reading_id)
            if existing is not None:
                return existing
        if request_id:
            completed = self._ensure_request_identity(
                request_id,
                "reading",
                {"raw_value": raw_value, "observed_at": observed, "meter_serial": meter_serial},
            )
            if completed and completed.get("kind") == "reading":
                existing = self.reading(str(completed.get("reading_id", "")))
                if existing is not None:
                    return existing
        resolution = self.resolve_reading(
            observed, meter_serial, source_version_id, settlement_version_id, timestamp
        )
        reading_id = reading_id or make_id(
            "reading", raw_value, observed, meter_serial, source_version_id, request_id
        )
        raw = RawReading(
            reading_id=reading_id,
            observed_at=observed,
            raw_value=raw_value,
            meter_serial=meter_serial,
            source_version_id="" if resolution.source_version is None else resolution.source_version.version_id,
            accepted_version_ids=[version.version_id for version in resolution.accepted_versions],
            quality=quality,
            request_id=request_id,
        )
        append_json(self._store, RAW_KEY, raw.as_dict())
        calibrated: list[CalibratedValue] = []
        for version in resolution.accepted_versions:
            item = CalibratedValue(
                reading_id=reading_id,
                version_id=version.version_id,
                meter_serial=version.meter_serial,
                factor=version.factor,
                calibrated_value=raw_value * version.factor,
            )
            append_json(self._store, CALIBRATED_KEY, item.as_dict())
            calibrated.append(item)
        settlement_record: SettlementValue | None = None
        if resolution.settlement_version is not None:
            official = resolution.settlement_version
            official_calibrated = raw_value * official.factor
            basis = "official"
            if len(raw.accepted_version_ids) > 1 or resolution.ambiguity:
                basis = "coexistence-official"
            settlement_record = SettlementValue(
                reading_id=reading_id,
                version_id=official.version_id,
                factor=official.factor,
                calibrated_value=official_calibrated,
                settlement_value=official_calibrated,
                basis=basis,
                state="settled",
            )
            append_json(self._store, SETTLEMENT_KEY, settlement_record.as_dict())
        self._remember_request(
            request_id,
            {
                "kind": "reading",
                "reading_id": reading_id,
                "raw_value": raw_value,
                "observed_at": observed,
                "meter_serial": meter_serial,
                "resolution": resolution.as_dict(),
            },
        )
        return ThreeTierReading(raw=raw, calibrated=calibrated, settlement=settlement_record)

    def readings(self) -> list[ThreeTierReading]:
        calibrated = {}
        for item in load_json_list(self._store, CALIBRATED_KEY):
            value = _calibrated_from_payload(item)
            calibrated.setdefault(value.reading_id, []).append(value)
        settlements = {
            value.reading_id: value
            for value in (
                _settlement_from_payload(item)
                for item in load_json_list(self._store, SETTLEMENT_KEY)
            )
        }
        result: list[ThreeTierReading] = []
        for item in load_json_list(self._store, RAW_KEY):
            raw = _raw_from_payload(item)
            result.append(ThreeTierReading(
                raw=raw,
                calibrated=list(calibrated.get(raw.reading_id, [])),
                settlement=settlements.get(raw.reading_id),
            ))
        return sorted(result, key=lambda item: (item.raw.observed_at, item.raw.reading_id))

    def reading(self, reading_id: str) -> ThreeTierReading | None:
        for reading in self.readings():
            if reading.raw.reading_id == reading_id:
                return reading
        return None

    def pending_readings(self, at: int | None = None) -> list[ThreeTierReading]:
        timestamp = at if at is not None else now_timestamp()
        return [
            reading
            for reading in self.readings()
            if reading.settlement is None
            and self.resolve_reading(
                reading.raw.observed_at,
                reading.raw.meter_serial,
                reading.raw.source_version_id,
                at=timestamp,
            ).settlement_version
            is not None
        ]

    def recompute_reading(self, reading_id: str, at: int | None = None) -> ThreeTierReading:
        """Idempotently fill a missing child projection; settled values never move."""

        reading = self.reading(reading_id)
        if reading is None:
            raise ValueError("unknown reading")
        if reading.settlement is not None:
            return reading
        timestamp = at if at is not None else now_timestamp()
        resolution = self.resolve_reading(
            reading.raw.observed_at,
            reading.raw.meter_serial,
            reading.raw.source_version_id,
            at=timestamp,
        )
        existing_calibrated = {
            self._calibrated_key(item): item for item in load_json_list(self._store, CALIBRATED_KEY)
        }
        def add_calibrated(items: list[dict[str, Any]]) -> None:
            for version in resolution.accepted_versions:
                key = f"{reading_id}:{version.version_id}"
                if key in existing_calibrated:
                    continue
                value = CalibratedValue(
                    reading_id=reading_id,
                    version_id=version.version_id,
                    meter_serial=version.meter_serial,
                    factor=version.factor,
                    calibrated_value=reading.raw.raw_value * version.factor,
                )
                payload = value.as_dict()
                existing_calibrated[key] = payload
                items.append(payload)
        update_json_list(self._store, CALIBRATED_KEY, add_calibrated)
        if resolution.settlement_version is not None:
            settlement_ids = {
                str(item.get("reading_id")) for item in load_json_list(self._store, SETTLEMENT_KEY)
            }
            if reading_id not in settlement_ids:
                official = resolution.settlement_version
                value = reading.raw.raw_value * official.factor
                append_json(self._store, SETTLEMENT_KEY, SettlementValue(
                    reading_id=reading_id,
                    version_id=official.version_id,
                    factor=official.factor,
                    calibrated_value=value,
                    settlement_value=value,
                    basis=(
                        "coexistence-official"
                        if len(reading.raw.accepted_version_ids) > 1 or resolution.ambiguity
                        else "official"
                    ),
                    state="settled",
                ).as_dict())
        refreshed = self.reading(reading_id)
        if refreshed is None:
            raise ValueError("unknown reading after recompute")
        return refreshed

    def recompute_pending(self, at: int | None = None) -> dict[str, object]:
        """Settle pending readings after activation. Existing settlement rows are immutable."""

        timestamp = at if at is not None else now_timestamp()
        pending = self.pending_readings(timestamp)
        settled = [self.recompute_reading(reading.raw.reading_id, timestamp) for reading in pending]
        return {
            "considered": len(pending),
            "settled": sum(item.settlement is not None for item in settled),
            "reading_ids": [item.raw.reading_id for item in settled if item.settlement is not None],
        }

    def consistency_check(self) -> dict[str, object]:
        """Verify raw, calibration and settlement rows agree on one immutable value."""

        errors: list[str] = []
        for reading in self.readings():
            for value in reading.calibrated:
                expected = reading.raw.raw_value * value.factor
                if abs(value.calibrated_value - expected) > 1e-9:
                    errors.append(f"calibrated mismatch for {reading.raw.reading_id}:{value.version_id}")
            if reading.settlement is not None:
                matching = [
                    value
                    for value in reading.calibrated
                    if value.version_id == reading.settlement.version_id
                ]
                if not matching:
                    errors.append(f"settlement has no calibrated tier for {reading.raw.reading_id}")
                elif abs(matching[0].calibrated_value - reading.settlement.settlement_value) > 1e-9:
                    errors.append(f"settlement mismatch for {reading.raw.reading_id}")
        return {"ok": not errors, "errors": errors, "readings": len(self.readings())}

    def jobs(self) -> list[CalibrationJob]:
        return [_job_from_payload(item) for item in load_json_list(self._store, JOB_KEY)]

    def job(self, job_id: str) -> CalibrationJob | None:
        for job in self.jobs():
            if job.job_id == job_id:
                return job
        return None

    def run_calibration_job(
        self,
        serial: str,
        samples: list[dict[str, float]],
        mode: str = "immediate",
        shift_boundary: str = DEFAULT_BOUNDARY,
        model: str = "",
        manufacturer: str = "",
        location: str = "",
        factor: float | None = None,
        fail_before: str = "",
        job_id: str = "",
        request_id: str = "",
        effective_at: int | None = None,
    ) -> CalibrationJob:
        validate_serial(serial)
        validate_mode(mode)
        if fail_before and fail_before not in JOB_STAGES:
            raise ValueError("unknown calibration stage")
        calculated = factor_from_samples(samples) if factor is None else float(factor)
        validate_factor(calculated)
        job_id = job_id or make_id("job", serial, calculated, request_id)
        if request_id:
            completed = self._ensure_request_identity(
                request_id, "job", {"serial": serial, "factor": calculated, "job_id": job_id}
            )
            if completed and completed.get("kind") == "job":
                existing = self.job(str(completed.get("job_id", "")))
                if existing is not None:
                    return existing
        existing = self.job(job_id)
        if existing is not None and existing.status in ("complete", "scheduled"):
            return existing
        requested_at = now_timestamp()
        boundary = parse_shift_boundary(shift_boundary) if mode == "shift_boundary" else boundary_value(shift_boundary)
        fixed_effective = resolve_effective_time(mode, requested_at, effective_at, boundary)
        version_id = make_id("factor", serial, calculated, fixed_effective, job_id)
        change_id = make_id("change", serial, version_id, job_id)
        if existing is None:
            job = CalibrationJob(
                job_id=job_id,
                request_id=request_id,
                serial=serial,
                model=model,
                manufacturer=manufacturer,
                location=location,
                mode=mode,
                shift_boundary=boundary,
                requested_at=requested_at,
                samples=[dict(sample) for sample in samples],
                factor=calculated,
                fail_before=fail_before,
                last_stage="",
                status="running",
                version_id=version_id,
                change_id=change_id,
                effective_at=fixed_effective,
            )
            append_json(self._store, JOB_KEY, job.as_dict())
        self._advance_job(job_id, now_timestamp())
        refreshed = self.job(job_id)
        if refreshed is None:
            raise ValueError("calibration job disappeared")
        return refreshed

    def recover_job(self, job_id: str, at: int | None = None) -> CalibrationJob:
        """Resume a partially failed calibration saga exactly once per unfinished step."""

        job = self.job(job_id)
        if job is None:
            raise ValueError("unknown calibration job")
        if job.status == "complete":
            return job
        self._advance_job(job_id, at if at is not None else now_timestamp(), recovering=True)
        refreshed = self.job(job_id)
        if refreshed is None:
            raise ValueError("calibration job disappeared")
        return refreshed

    def state(self) -> CalibState:
        current = self.current_version(None)
        return CalibState(
            factor=self.current(),
            current_version=current,
            meters=self.meters(),
            versions=self.versions(),
            changes=self.changes(),
            jobs=self.jobs(),
            readings=self.readings(),
            pending_changes=self.pending_changes(),
        )

    def describe(self) -> str:
        current = self.current_version(None)
        version_id = "" if current is None else current.version_id
        failed = sum(job.status == "failed" for job in self.jobs())
        pending = len(self.pending_changes())
        return (
            f"flow calibration factor={self.current():.4f} version={version_id} "
            f"meters={len(self.meters())} versions={len(self.versions())} "
            f"pending_changes={pending} failed_jobs={failed}"
        )

    def _activate_change(self, change: ChangeOrder, timestamp: int) -> ChangeOrder:
        if change.status in ("immediate", "active"):
            return change
        activation_exists = has_json(
            self._store,
            ACTIVATION_KEY,
            lambda item: str(item.get("change_id")) == change.change_id,
        )
        if not activation_exists:
            append_json(self._store, ACTIVATION_KEY, {
                "change_id": change.change_id,
                "meter_serial": change.meter_serial,
                "version_id": change.version_id,
                "previous_version_id": change.previous_version_id,
                "activated_at": change.effective_at,
            })

        def mark(items: list[dict[str, Any]]) -> None:
            for item in items:
                if item.get("change_id") == change.change_id:
                    item["status"] = "active"
                    item["completed_at"] = timestamp
        update_json_list(self._store, CHANGE_KEY, mark)
        version = self.version(change.version_id)
        previous = self.version(change.previous_version_id) if change.previous_version_id else None
        if version is not None or previous is not None:
            def mark_version(items: list[dict[str, Any]]) -> None:
                if version is not None:
                    for item in items:
                        if item.get("version_id") == version.version_id:
                            item["effective_at"] = change.effective_at
                            item["change_id"] = change.change_id
                if previous is not None:
                    for item in items:
                        if item.get("version_id") == previous.version_id:
                            item["superseded_at"] = change.effective_at
                            item["retired_at"] = change.compatibility_until
            update_json_list(self._store, VERSION_KEY, mark_version)
        save_float(self._store, CALIBRATION_KEY, version.factor if version else DEFAULT_FACTOR)
        self._audit_once(
            "meter_replace",
            (
                f"{change.meter_serial} factor={version.factor if version else 0:.4f} "
                f"version={change.version_id} mode={change.mode} activated"
            ),
            change.change_id,
            "activate",
        )
        self.recompute_pending(timestamp)
        activated = self.change(change.change_id)
        return activated or change

    def _advance_job(self, job_id: str, timestamp: int, recovering: bool = False) -> None:
        job = self.job(job_id)
        if job is None:
            return
        status = "running" if recovering and job.status == "failed" else job.status
        audit_ids = dict(job.audit_ids)

        def save(last_stage: str, new_status: str, completed_at: int | None = None) -> None:
            def mutate(items: list[dict[str, Any]]) -> None:
                for item in items:
                    if item.get("job_id") == job_id:
                        item["last_stage"] = last_stage
                        item["status"] = new_status
                        item["audit_ids"] = audit_ids
                        if completed_at is not None:
                            item["completed_at"] = completed_at
            update_json_list(self._store, JOB_KEY, mutate)

        def fail(stage: str, message: str) -> None:
            stage_index = JOB_STAGES.index(stage)
            last_completed = JOB_STAGES[stage_index - 1] if stage_index else ""
            if last_completed == "wait_for_effective_time" and self.job(job_id).mode == "immediate":
                last_completed = "create_change"
            save(last_completed, "failed")
            raise CalibrationError(job_id, stage, message)

        for stage in JOB_STAGES:
            current = self.job(job_id)
            if current is None:
                raise ValueError("unknown calibration job")
            order_index = JOB_STAGES.index(stage)
            if stage == "wait_for_effective_time":
                if current.mode == "immediate":
                    continue
                if timestamp < current.effective_at:
                    save(stage, "scheduled")
                    self._remember_job_request(current)
                    return
                if current.last_stage == stage:
                    continue
            if current.last_stage and JOB_STAGES.index(current.last_stage) >= order_index:
                continue
            if not recovering and current.fail_before == stage:
                fail(stage, f"forced failure before {stage}")
            if stage == "validate":
                validate_serial(current.serial)
                validate_factor(current.factor)
            elif stage == "register_meter":
                if self.meter(current.serial) is None:
                    self.register_meter(
                        current.serial,
                        model=current.model,
                        manufacturer=current.manufacturer,
                        location=current.location,
                        commissioned_at=current.requested_at,
                    )
            elif stage == "create_version":
                if self.version(current.version_id) is None:
                    version = FactorVersion(
                        version_id=current.version_id,
                        meter_serial=current.serial,
                        factor=current.factor,
                        basis="job-samples",
                        samples=[dict(sample) for sample in current.samples],
                        created_at=current.requested_at,
                        effective_at=current.effective_at,
                        change_id=current.change_id,
                    )
                    append_json(self._store, VERSION_KEY, version.as_dict())
            elif stage == "create_change":
                if self.change(current.change_id) is None:
                    previous = self.current_version(None, current.requested_at)
                    change = ChangeOrder(
                        change_id=current.change_id,
                        meter_serial=current.serial,
                        version_id=current.version_id,
                        previous_version_id="" if previous is None else previous.version_id,
                        mode=current.mode,
                        requested_at=current.requested_at,
                        effective_at=current.effective_at,
                        shift_boundary=current.shift_boundary,
                        status="scheduled",
                        request_id=current.request_id,
                        compatibility_until=current.effective_at + IMMEDIATE_COMPATIBILITY_SECONDS,
                    )
                    append_json(self._store, CHANGE_KEY, change.as_dict())
            elif stage == "activate":
                change = self.change(current.change_id)
                if change is not None and change.status == "scheduled":
                    self._activate_change(change, timestamp)
            elif stage == "audit_activation":
                entry_id = self._audit_once(
                    "calibration",
                    f"{current.serial} factor={current.factor:.4f} version={current.version_id} completed",
                    current.change_id,
                    "job",
                )
                audit_ids["job"] = entry_id
            elif stage == "settle_pending":
                self.recompute_pending(timestamp)
            elif stage == "complete":
                save(stage, "complete", timestamp)
                self._remember_job_request(current)
                return
            save(stage, "running")

    def _source_for_serial(
        self, serial: str, observed_at: int, at: int
    ) -> FactorVersion | None:
        staged = self._staged_line_version(at)
        if staged is not None and (staged.meter_serial == serial or observed_at < staged.effective_at):
            previous_change = self._change_for_version(staged.version_id)
            previous = (
                None if previous_change is None else self.version(previous_change.previous_version_id)
            )
            if staged.meter_serial == serial:
                return staged
            if previous is not None and previous.meter_serial == serial:
                return previous

        official = self._official_version(observed_at)
        if official is not None and official.meter_serial == serial:
            return official
        if official is not None:
            change = self._change_for_version(official.version_id)
            previous = None if change is None else self.version(change.previous_version_id)
            if previous is not None and previous.meter_serial == serial:
                if official.effective_at <= observed_at <= change.compatibility_until:
                    return previous
        versions = self.versions(serial)
        return max(versions, key=lambda item: item.created_at) if versions else None

    def _change_for_version(self, version_id: str) -> ChangeOrder | None:
        for change in self.changes():
            if change.version_id == version_id:
                return change
        return None

    def _audit_once(self, kind: str, detail: str, dedupe_key: str, phase: str) -> str:
        audit_ids = load_json_object(self._store, "flow:calibration:audit-dedupe")
        key = f"{dedupe_key}:{phase}"
        if key in audit_ids:
            return str(audit_ids[key])
        entry = self._auditor.record(kind, detail)
        audit_ids[key] = entry.id
        save_json_object(self._store, "flow:calibration:audit-dedupe", audit_ids)
        return entry.id

    def _completed_request(self, request_id: str) -> dict[str, Any] | None:
        if not request_id:
            return None
        return load_json_object(self._store, IDEMPOTENCY_KEY).get(request_id)

    def _ensure_request_identity(
        self, request_id: str, kind: str, expected: dict[str, object]
    ) -> dict[str, Any] | None:
        completed = self._completed_request(request_id)
        if completed is None:
            return None
        if completed.get("kind") != kind:
            raise ValueError("request_id is already bound to a different operation")
        for key, value in expected.items():
            if completed.get(key) != value:
                raise ValueError(f"request_id conflicts with existing {key}")
        return completed

    def _remember_request(self, request_id: str, payload: dict[str, Any]) -> None:
        if not request_id:
            return
        values = load_json_object(self._store, IDEMPOTENCY_KEY)
        values[request_id] = dict(payload)
        save_json_object(self._store, IDEMPOTENCY_KEY, values)

    def _remember_job_request(self, job: CalibrationJob) -> None:
        self._remember_request(
            job.request_id,
            {
                "kind": "job",
                "job_id": job.job_id,
                "serial": job.serial,
                "factor": job.factor,
            },
        )

    @staticmethod
    def _calibrated_key(payload: dict[str, Any]) -> str:
        return f"{payload.get('reading_id')}:{payload.get('version_id')}"


def boundary_value(value: str) -> str:
    return parse_shift_boundary(value) if value else DEFAULT_BOUNDARY


class CalibrationError(RuntimeError):
    """A calibration saga failed at a resumable stage."""

    def __init__(self, job_id: str, stage: str, message: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.stage = stage
