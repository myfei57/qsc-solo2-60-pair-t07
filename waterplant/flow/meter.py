"""Physical flow meter replacement helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .report import MeterProfile, validate_factor, validate_serial


@dataclass(frozen=True)
class Meter:
    """Backwards compatible meter serial number paired with a factor."""

    serial: str
    factor: float

    def as_dict(self) -> dict[str, object]:
        return {"serial": self.serial, "factor": self.factor}


def calibrate_meter(serial: str, factor: float) -> Meter:
    """Bind a factor to a meter so the swap can be audited."""

    validate_serial(serial)
    validate_factor(factor)
    return Meter(serial=serial, factor=factor)


def meter_profile(
    serial: str,
    commissioned_at: int,
    model: str = "",
    manufacturer: str = "",
    location: str = "",
    attributes: dict[str, object] | None = None,
) -> MeterProfile:
    validate_serial(serial)
    return MeterProfile(
        serial=serial,
        model=model,
        manufacturer=manufacturer,
        location=location,
        commissioned_at=commissioned_at,
        attributes=dict(attributes or {}),
    )
