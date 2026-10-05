"""Meter archive and coefficient versions, bound together by effective time.

The archive (仪表档案), the coefficient version (系数版本) and the effective
interval (生效时间) are stored as one fact: every version row names its meter
and owns a half-open interval ``[effective_from, effective_to)``. Resolution
is purely interval driven, so any reading, calibration or replacement can be
traced back to the version that was in force at its event time.
"""

from __future__ import annotations

from datetime import timedelta

from waterplant.flow.report import validate_factor
from waterplant.store.store import Store

from .core import (
    MeterNotFound,
    fmt,
    load_records,
    new_id,
    normalize,
    parse,
    save_records,
    utcnow,
)

METERS_KEY = "metering:meters"
VERSIONS_KEY = "metering:versions"

COMPAT_WINDOW_HOURS = 24


class MeterRegistry:
    """Flow meter archive: one record per physical meter, keyed by serial."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def register(
        self,
        serial: str,
        *,
        location: str = "",
        medium: str = "",
        initial_factor: float | None = None,
        effective_from: str = "",
        now: str = "",
    ) -> dict:
        """Add a meter to the archive, idempotent by serial number.

        When ``initial_factor`` is given, an INITIAL coefficient version is
        commissioned at ``effective_from`` (default: ``now``) so the meter can
        accept readings immediately.
        """

        now = now or utcnow()
        meters = load_records(self._store, METERS_KEY)
        for meter in meters:
            if meter["serial"] == serial:
                return meter
        meter = {
            "meter_id": new_id(),
            "serial": serial,
            "location": location,
            "medium": medium,
            "status": "ACTIVE",
            "installed_at": now,
            "retired_at": None,
        }
        meters.append(meter)
        save_records(self._store, METERS_KEY, meters)
        if initial_factor is not None:
            validate_factor(initial_factor)
            start = normalize(effective_from) if effective_from else now
            book = VersionBook(self._store)
            version = book.stage(meter["meter_id"], initial_factor, start, "INITIAL", "", now)
            book.activate(version["version_id"])
        return meter

    def get(self, meter_id: str) -> dict:
        for meter in self.all():
            if meter["meter_id"] == meter_id:
                return meter
        raise MeterNotFound(f"unknown meter {meter_id}")

    def find_by_serial(self, serial: str) -> dict | None:
        for meter in self.all():
            if meter["serial"] == serial:
                return meter
        return None

    def all(self) -> list[dict]:
        return load_records(self._store, METERS_KEY)

    def retire(self, meter_id: str, retired_at: str) -> tuple[dict, bool]:
        """Mark a meter retired, idempotently; returns (meter, changed)."""

        meters = load_records(self._store, METERS_KEY)
        changed = False
        found: dict | None = None
        for meter in meters:
            if meter["meter_id"] == meter_id:
                found = meter
                if meter["status"] == "ACTIVE":
                    meter["status"] = "RETIRED"
                    meter["retired_at"] = retired_at
                    changed = True
        if found is None:
            raise MeterNotFound(f"unknown meter {meter_id}")
        if changed:
            save_records(self._store, METERS_KEY, meters)
        return found, changed


class VersionBook:
    """Coefficient versions: staged, closed and activated by the workflows."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def stage(
        self,
        meter_id: str,
        factor: float,
        effective_from: str,
        source: str,
        source_id: str,
        now: str,
    ) -> dict:
        """Persist a PENDING version; the workflow flips it EFFECTIVE later."""

        version = {
            "version_id": new_id(),
            "meter_id": meter_id,
            "factor": factor,
            "effective_from": effective_from,
            "effective_to": None,
            "source": source,
            "source_id": source_id,
            "status": "PENDING",
            "created_at": now,
        }
        versions = load_records(self._store, VERSIONS_KEY)
        versions.append(version)
        save_records(self._store, VERSIONS_KEY, versions)
        return version

    def staged_for(self, source: str, source_id: str) -> dict | None:
        """PENDING version previously staged by a workflow, for crash recovery."""

        for version in load_records(self._store, VERSIONS_KEY):
            if (
                version["source"] == source
                and version["source_id"] == source_id
                and version["status"] == "PENDING"
            ):
                return version
        return None

    def close_current(
        self, meter_id: str, effective_to: str, *, exclude: str = ""
    ) -> dict | None:
        """Close the open EFFECTIVE version at ``effective_to``, idempotently."""

        versions = load_records(self._store, VERSIONS_KEY)
        closed: dict | None = None
        for version in versions:
            if (
                version["meter_id"] == meter_id
                and version["status"] == "EFFECTIVE"
                and version["effective_to"] is None
                and version["version_id"] != exclude
            ):
                version["status"] = "SUPERSEDED"
                version["effective_to"] = effective_to
                closed = version
        if closed is not None:
            save_records(self._store, VERSIONS_KEY, versions)
        return closed

    def activate(self, version_id: str) -> tuple[dict, bool]:
        """Flip a PENDING version to EFFECTIVE; returns (version, changed)."""

        versions = load_records(self._store, VERSIONS_KEY)
        for version in versions:
            if version["version_id"] == version_id:
                if version["status"] == "PENDING":
                    version["status"] = "EFFECTIVE"
                    save_records(self._store, VERSIONS_KEY, versions)
                    return version, True
                return version, False
        raise MeterNotFound(f"unknown version {version_id}")

    def get(self, version_id: str) -> dict:
        for version in self.all():
            if version["version_id"] == version_id:
                return version
        raise MeterNotFound(f"unknown version {version_id}")

    def current(self, meter_id: str) -> dict | None:
        for version in self.all():
            if version["meter_id"] == meter_id and version["status"] == "EFFECTIVE":
                return version
        return None

    def version_at(self, meter_id: str, ts: str) -> dict | None:
        """The version whose half-open interval contains ``ts``.

        This is the single resolution rule for the whole plant: event time
        against stored intervals, so the answer is identical for ingestion,
        settlement and historical trace-back.
        """

        ts = normalize(ts)
        best: dict | None = None
        for version in self.all():
            if version["meter_id"] != meter_id:
                continue
            if version["effective_from"] <= ts and (
                version["effective_to"] is None or version["effective_to"] > ts
            ):
                if best is None or version["effective_from"] > best["effective_from"]:
                    best = version
        return best

    def timeline(self, meter_id: str) -> list[dict]:
        versions = [v for v in self.all() if v["meter_id"] == meter_id]
        return sorted(versions, key=lambda v: v["effective_from"])

    def all(self) -> list[dict]:
        return load_records(self._store, VERSIONS_KEY)

    def accepted(
        self, meter_id: str, at: str, window_hours: int = COMPAT_WINDOW_HOURS
    ) -> list[dict]:
        """Version tags a reader must accept at ``at`` (compatibility window).

        While an old and a new coefficient coexist, readers accept: the
        EFFECTIVE version, versions superseded within the window (late or
        replayed data), and PENDING versions due within the window (pre-staged
        by an in-flight workflow).
        """

        moment = parse(at)
        low = fmt(moment - timedelta(hours=window_hours))
        high = fmt(moment + timedelta(hours=window_hours))
        accepted: list[dict] = []
        for version in self.timeline(meter_id):
            if version["status"] == "EFFECTIVE":
                accepted.append(version)
            elif (
                version["status"] == "SUPERSEDED"
                and version["effective_to"] is not None
                and version["effective_to"] >= low
            ):
                accepted.append(version)
            elif version["status"] == "PENDING" and version["effective_from"] <= high:
                accepted.append(version)
        return accepted
