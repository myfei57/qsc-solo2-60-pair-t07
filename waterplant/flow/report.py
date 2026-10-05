"""Flow meter, factor version and calibration projections."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MeterProfile:
    """Archive record for one physical flow meter."""

    serial: str
    model: str
    manufacturer: str
    location: str
    commissioned_at: int
    retired_at: int | None = None
    attributes: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "serial": self.serial,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "location": self.location,
            "commissioned_at": self.commissioned_at,
            "retired_at": self.retired_at,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class FactorVersion:
    """Immutable factor version bound to a meter and an effective interval."""

    version_id: str
    meter_serial: str
    factor: float
    basis: str
    samples: list[dict[str, float]]
    created_at: int
    effective_at: int
    superseded_at: int | None = None
    retired_at: int | None = None
    change_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "meter_serial": self.meter_serial,
            "factor": self.factor,
            "basis": self.basis,
            "samples": [dict(sample) for sample in self.samples],
            "created_at": self.created_at,
            "effective_at": self.effective_at,
            "superseded_at": self.superseded_at,
            "retired_at": self.retired_at,
            "change_id": self.change_id,
        }


@dataclass(frozen=True)
class ChangeOrder:
    """A meter replacement order governing when a new version takes effect."""

    change_id: str
    meter_serial: str
    version_id: str
    previous_version_id: str
    mode: str
    requested_at: int
    effective_at: int
    shift_boundary: str
    status: str
    request_id: str
    compatibility_until: int = 0
    completed_at: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "meter_serial": self.meter_serial,
            "version_id": self.version_id,
            "previous_version_id": self.previous_version_id,
            "mode": self.mode,
            "requested_at": self.requested_at,
            "effective_at": self.effective_at,
            "shift_boundary": self.shift_boundary,
            "status": self.status,
            "request_id": self.request_id,
            "compatibility_until": self.compatibility_until,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class CalibrationJob:
    """Recoverable state for a multi-step calibration saga."""

    job_id: str
    request_id: str
    serial: str
    model: str
    manufacturer: str
    location: str
    mode: str
    shift_boundary: str
    requested_at: int
    samples: list[dict[str, float]]
    factor: float
    fail_before: str
    last_stage: str
    status: str
    version_id: str
    change_id: str
    audit_ids: dict[str, str] = field(default_factory=dict)
    effective_at: int = 0
    completed_at: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "request_id": self.request_id,
            "serial": self.serial,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "location": self.location,
            "mode": self.mode,
            "shift_boundary": self.shift_boundary,
            "requested_at": self.requested_at,
            "samples": [dict(sample) for sample in self.samples],
            "factor": self.factor,
            "fail_before": self.fail_before,
            "last_stage": self.last_stage,
            "status": self.status,
            "version_id": self.version_id,
            "change_id": self.change_id,
            "audit_ids": dict(self.audit_ids),
            "effective_at": self.effective_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class RawReading:
    """Immutable signal as supplied by the reading system."""

    reading_id: str
    observed_at: int
    raw_value: float
    meter_serial: str
    source_version_id: str
    accepted_version_ids: list[str]
    quality: str
    request_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "reading_id": self.reading_id,
            "observed_at": self.observed_at,
            "raw_value": self.raw_value,
            "meter_serial": self.meter_serial,
            "source_version_id": self.source_version_id,
            "accepted_version_ids": list(self.accepted_version_ids),
            "quality": self.quality,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class CalibratedValue:
    """A raw value multiplied by a factor recognized by the reader."""

    reading_id: str
    version_id: str
    meter_serial: str
    factor: float
    calibrated_value: float

    def as_dict(self) -> dict[str, object]:
        return {
            "reading_id": self.reading_id,
            "version_id": self.version_id,
            "meter_serial": self.meter_serial,
            "factor": self.factor,
            "calibrated_value": self.calibrated_value,
        }


@dataclass(frozen=True)
class SettlementValue:
    """The immutable official settlement value selected for a reading."""

    reading_id: str
    version_id: str
    factor: float
    calibrated_value: float
    settlement_value: float
    basis: str
    state: str

    def as_dict(self) -> dict[str, object]:
        return {
            "reading_id": self.reading_id,
            "version_id": self.version_id,
            "factor": self.factor,
            "calibrated_value": self.calibrated_value,
            "settlement_value": self.settlement_value,
            "basis": self.basis,
            "state": self.state,
        }


@dataclass(frozen=True)
class ThreeTierReading:
    """Raw reading, reader recognized values, and official settlement together."""

    raw: RawReading
    calibrated: list[CalibratedValue]
    settlement: SettlementValue | None

    def as_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw.as_dict(),
            "calibrated": [item.as_dict() for item in self.calibrated],
            "settlement": None if self.settlement is None else self.settlement.as_dict(),
        }


@dataclass(frozen=True)
class Resolution:
    """Result of matching a reading to source and settlement factor versions."""

    source_version: FactorVersion | None
    settlement_version: FactorVersion | None
    accepted_versions: list[FactorVersion]
    ambiguity: bool
    reason: str

    @property
    def pending(self) -> bool:
        return self.settlement_version is None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_version_id": "" if self.source_version is None else self.source_version.version_id,
            "settlement_version_id": (
                "" if self.settlement_version is None else self.settlement_version.version_id
            ),
            "accepted_version_ids": [version.version_id for version in self.accepted_versions],
            "ambiguity": self.ambiguity,
            "pending": self.pending,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CalibState:
    """Current factor plus the complete traceable calibration projection."""

    factor: float
    current_version: FactorVersion | None = None
    meters: list[MeterProfile] = field(default_factory=list)
    versions: list[FactorVersion] = field(default_factory=list)
    changes: list[ChangeOrder] = field(default_factory=list)
    jobs: list[CalibrationJob] = field(default_factory=list)
    readings: list[ThreeTierReading] = field(default_factory=list)
    pending_changes: list[ChangeOrder] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "factor": self.factor,
            "current_version": (
                None if self.current_version is None else self.current_version.as_dict()
            ),
            "meters": [meter.as_dict() for meter in self.meters],
            "versions": [version.as_dict() for version in self.versions],
            "changes": [change.as_dict() for change in self.changes],
            "jobs": [job.as_dict() for job in self.jobs],
            "readings": [reading.as_dict() for reading in self.readings],
            "pending_changes": [change.as_dict() for change in self.pending_changes],
        }


def validate_factor(factor: float) -> None:
    """Reject calibration factors the meters cannot produce."""

    if factor <= 0:
        raise ValueError("factor must be positive")
    if factor > 100:
        raise ValueError("factor exceeds the supported range")


def validate_serial(serial: str) -> None:
    if not serial or not serial.strip():
        raise ValueError("meter serial is required")
    if len(serial) > 128:
        raise ValueError("meter serial is too long")


def validate_mode(mode: str) -> None:
    if mode not in ("immediate", "shift_boundary"):
        raise ValueError("mode must be immediate or shift_boundary")


def validate_samples(samples: list[dict[str, float]]) -> None:
    if not samples:
        raise ValueError("at least one calibration sample is required")
    for sample in samples:
        raw_value = sample.get("raw")
        reference = sample.get("reference")
        if raw_value is None or reference is None:
            raise ValueError("each sample requires raw and reference")
        if raw_value <= 0:
            raise ValueError("raw sample must be positive")
        if reference < 0:
            raise ValueError("reference sample must be non-negative")


def factor_from_samples(samples: list[dict[str, float]]) -> float:
    """Mean of reference/raw ratios, matching the physical calibration procedure."""

    validate_samples(samples)
    return sum(float(sample["reference"]) / float(sample["raw"]) for sample in samples) / len(samples)
