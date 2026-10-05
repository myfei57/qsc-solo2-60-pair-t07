"""Flow meter calibration shared with the dosing controllers."""

from .calibration import (
    CALIBRATION_KEY,
    DEFAULT_BOUNDARY,
    CalibrationError,
    CalibrationManager,
    make_id,
    now_timestamp,
    parse_shift_boundary,
    resolve_effective_time,
)
from .meter import Meter, calibrate_meter, meter_profile
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
    validate_samples,
    validate_serial,
)

# Backwards compatible name for code written before versioned calibration.
Calibration = CalibrationManager

__all__ = [
    "CALIBRATION_KEY",
    "DEFAULT_BOUNDARY",
    "CalibState",
    "CalibratedValue",
    "Calibration",
    "CalibrationError",
    "CalibrationJob",
    "CalibrationManager",
    "ChangeOrder",
    "FactorVersion",
    "Meter",
    "MeterProfile",
    "RawReading",
    "Resolution",
    "SettlementValue",
    "ThreeTierReading",
    "calibrate_meter",
    "factor_from_samples",
    "make_id",
    "meter_profile",
    "now_timestamp",
    "parse_shift_boundary",
    "resolve_effective_time",
    "validate_factor",
    "validate_mode",
    "validate_samples",
    "validate_serial",
]
