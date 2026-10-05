"""Shared primitives for the metering calibration package.

Pinned rules honoured by every module in this package:

- A coefficient version owns a half-open interval ``[effective_from, effective_to)``;
  a reading exactly at ``effective_from`` belongs to the NEW version.
- Applicability is resolved by event time against those intervals, never by
  "latest version wins", so historical readings keep the factor in force then.
- History is immutable: stored readings and FINALIZED settlements are never
  updated, and new factors only apply at and after their effective time.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from waterplant.store.store import Store

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class MeteringError(ValueError):
    """Base metering error, a ValueError so the console maps it to HTTP 400."""


class MeterNotFound(MeteringError):
    """Raised when a meter id is not in the archive."""


class MeterRetired(MeteringError):
    """Raised when an operation targets a retired meter."""


class VersionNotFound(MeteringError):
    """Raised when no coefficient version covers a timestamp."""


class IncompatibleVersion(MeteringError):
    """Raised when a reader presents a version tag outside the compat window."""


class InvalidEffectiveTime(MeteringError):
    """Raised when an effective time would rewrite history."""


class WorkflowStateError(MeteringError):
    """Raised for illegal workflow transitions."""


class ReadingRejected(MeteringError):
    """Raised when a reading cannot be accepted without breaking immutability."""


class ImmutableSettlement(MeteringError):
    """Raised when a finalized settlement would be recomputed."""


class ConsistencyError(MeteringError):
    """Raised when raw/calibrated/settlement tiers disagree."""


class InjectedFault(Exception):
    """Test hook simulating a crash between workflow steps."""


def utcnow() -> str:
    """Current UTC time in the store's canonical text form."""

    return time.strftime(TIME_FORMAT, time.gmtime())


def parse(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp, normalizing to aware UTC."""

    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def fmt(moment: datetime) -> str:
    """Render a datetime in the store's canonical text form."""

    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def normalize(ts: str) -> str:
    """Normalize any ISO-8601 timestamp to the canonical comparable form."""

    return fmt(parse(ts))


def new_id() -> str:
    return uuid.uuid4().hex


DEFAULT_SHIFTS = ("00:00", "08:00", "16:00")


class ShiftSchedule:
    """Daily shift boundaries (UTC) that deferred effective times snap to."""

    def __init__(self, boundaries: tuple[str, ...] = DEFAULT_SHIFTS) -> None:
        if not boundaries:
            raise MeteringError("shift boundaries must not be empty")
        self._boundaries = tuple(sorted(boundaries))

    @property
    def boundaries(self) -> tuple[str, ...]:
        return self._boundaries

    def next_boundary(self, after: str) -> str:
        """First shift boundary strictly after ``after``."""

        moment = parse(after)
        for day_offset in (0, 1):
            day = (moment + timedelta(days=day_offset)).date()
            for bound in self._boundaries:
                hour, minute = (int(part) for part in bound.split(":"))
                candidate = datetime(
                    day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc
                )
                if candidate > moment:
                    return fmt(candidate)
        raise AssertionError("unreachable")


def load_records(store: Store, key: str) -> list[dict]:
    """Load a JSON record list, tolerating a missing or corrupt document."""

    raw, present = store.get(key)
    if not present:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def save_records(store: Store, key: str, items: list[dict]) -> None:
    store.put(key, json.dumps(items, ensure_ascii=False))
