"""Raw readings, calibrated values and settlements: one consistent caliber.

Three stored tiers must always agree (三段口径一致):

- raw reading:  ``raw`` as reported by the meter, never rewritten.
- calibrated:   ``calibrated = raw * factor`` of the coefficient version hit
                by the reading's event time; stored on the reading itself.
- settlement:   the sum of calibrated values over a period, split into
                per-version segments; stored on the settlement record.

Pinned settlement rule (定死的结算口径): every reading settles under the
version whose half-open interval ``[effective_from, effective_to)`` contains
its timestamp. A period spanning a coefficient switch settles in per-version
segments and the period total is the sum of segment subtotals. A FINALIZED
settlement is immutable and is never recomputed; recomputation is only
allowed while OPEN and is idempotent for identical inputs.
"""

from __future__ import annotations

import json
import math

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .core import (
    ConsistencyError,
    ImmutableSettlement,
    IncompatibleVersion,
    MeteringError,
    ReadingRejected,
    VersionNotFound,
    load_records,
    new_id,
    normalize,
    save_records,
    utcnow,
)
from .registry import COMPAT_WINDOW_HOURS, MeterRegistry, VersionBook

READINGS_KEY = "metering:readings"
SETTLEMENTS_KEY = "metering:settlements"


def _finalized_covering(store: Store, meter_id: str, ts: str) -> dict | None:
    for settlement in load_records(store, SETTLEMENTS_KEY):
        if (
            settlement["meter_id"] == meter_id
            and settlement["status"] == "FINALIZED"
            and settlement["period_start"] <= ts < settlement["period_end"]
        ):
            return settlement
    return None


class ReadingLog:
    """Ingests raw readings and stamps them with the version in force."""

    def __init__(
        self, store: Store, auditor: Auditor, compat_window_hours: int = COMPAT_WINDOW_HOURS
    ) -> None:
        self._store = store
        self._auditor = auditor
        self._meters = MeterRegistry(store)
        self._versions = VersionBook(store)
        self._window_hours = compat_window_hours

    def ingest(
        self,
        meter_id: str,
        ts: str,
        raw: float,
        *,
        idempotency_key: str,
        client_version_id: str = "",
        now: str = "",
    ) -> dict:
        """Store a reading, resolving its coefficient version by event time.

        Idempotent by ``idempotency_key``: a retried ingest returns the first
        stored record. While old and new versions coexist, ``client_version_id``
        may name either; a tag that disagrees with event-time resolution is
        accepted but the event-time version wins and the mismatch is audited.
        Readings landing inside a FINALIZED settlement period are rejected so
        history stays immutable.
        """

        now = now or utcnow()
        if not idempotency_key:
            raise MeteringError("idempotency_key is required")
        ts = normalize(ts)
        readings = load_records(self._store, READINGS_KEY)
        for reading in readings:
            if reading["idempotency_key"] == idempotency_key:
                return reading
        self._meters.get(meter_id)
        covering = _finalized_covering(self._store, meter_id, ts)
        if covering is not None:
            raise ReadingRejected(
                f"reading at {ts} falls in finalized settlement "
                f"{covering['settlement_id']}; history is immutable"
            )
        version = self._versions.version_at(meter_id, ts)
        if version is None:
            raise VersionNotFound(f"no coefficient version for meter {meter_id} at {ts}")
        if client_version_id:
            accepted = {
                item["version_id"]
                for item in self._versions.accepted(meter_id, now, self._window_hours)
            }
            if client_version_id not in accepted:
                raise IncompatibleVersion(
                    f"version {client_version_id} is not accepted for meter {meter_id}"
                )
            if client_version_id != version["version_id"]:
                self._auditor.record(
                    "reading.version_mismatch",
                    json.dumps(
                        {
                            "meter_id": meter_id,
                            "ts": ts,
                            "client_version_id": client_version_id,
                            "resolved_version_id": version["version_id"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
        reading = {
            "reading_id": new_id(),
            "meter_id": meter_id,
            "ts": ts,
            "raw": float(raw),
            "factor": version["factor"],
            "calibrated": float(raw) * version["factor"],
            "version_id": version["version_id"],
            "client_version_id": client_version_id or None,
            "idempotency_key": idempotency_key,
            "created_at": now,
        }
        readings.append(reading)
        save_records(self._store, READINGS_KEY, readings)
        return reading

    def between(self, meter_id: str, period_start: str, period_end: str) -> list[dict]:
        start, end = normalize(period_start), normalize(period_end)
        rows = [
            reading
            for reading in load_records(self._store, READINGS_KEY)
            if reading["meter_id"] == meter_id and start <= reading["ts"] < end
        ]
        return sorted(rows, key=lambda reading: reading["ts"])

    def all(self, meter_id: str = "") -> list[dict]:
        readings = load_records(self._store, READINGS_KEY)
        if meter_id:
            readings = [r for r in readings if r["meter_id"] == meter_id]
        return sorted(readings, key=lambda reading: reading["ts"])


class SettlementBook:
    """Computes, finalizes and verifies period settlements."""

    def __init__(self, store: Store, auditor: Auditor) -> None:
        self._store = store
        self._auditor = auditor
        self._readings = ReadingLog(store, auditor)

    def settle(self, meter_id: str, period_start: str, period_end: str, *, now: str = "") -> dict:
        """Compute an OPEN settlement, or recompute it idempotently.

        Re-running with unchanged inputs returns the stored record untouched;
        new readings in the period bump the revision. A FINALIZED period is
        immutable and raises instead of recomputing.
        """

        now = now or utcnow()
        start, end = self._period(period_start, period_end)
        settlements = load_records(self._store, SETTLEMENTS_KEY)
        existing = self._find(settlements, meter_id, start, end)
        if existing is not None and existing["status"] == "FINALIZED":
            raise ImmutableSettlement(
                f"settlement {existing['settlement_id']} is FINALIZED; "
                "historical data is never recomputed"
            )
        segments = self._segments(meter_id, start, end)
        total = sum((segment["subtotal"] for segment in segments), 0.0)
        if existing is not None:
            if existing["segments"] == segments and existing["total"] == total:
                return existing
            existing["segments"] = segments
            existing["total"] = total
            existing["revision"] += 1
            existing["updated_at"] = now
            save_records(self._store, SETTLEMENTS_KEY, settlements)
            return existing
        record = {
            "settlement_id": new_id(),
            "meter_id": meter_id,
            "period_start": start,
            "period_end": end,
            "status": "OPEN",
            "segments": segments,
            "total": total,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "finalized_at": None,
        }
        settlements.append(record)
        save_records(self._store, SETTLEMENTS_KEY, settlements)
        return record

    def finalize(
        self, meter_id: str, period_start: str, period_end: str, *, now: str = ""
    ) -> dict:
        """Freeze a settlement after a mandatory consistency check.

        Idempotent: finalizing an already FINALIZED period returns the stored
        record unchanged.
        """

        now = now or utcnow()
        start, end = self._period(period_start, period_end)
        existing = self._find(load_records(self._store, SETTLEMENTS_KEY), meter_id, start, end)
        if existing is not None and existing["status"] == "FINALIZED":
            return existing
        record = self.settle(meter_id, start, end, now=now)
        report = self.verify(meter_id, start, end)
        if not report["ok"]:
            raise ConsistencyError(json.dumps(report, ensure_ascii=False, sort_keys=True))
        settlements = load_records(self._store, SETTLEMENTS_KEY)
        for settlement in settlements:
            if settlement["settlement_id"] == record["settlement_id"]:
                settlement["status"] = "FINALIZED"
                settlement["finalized_at"] = now
                settlement["updated_at"] = now
                record = settlement
        save_records(self._store, SETTLEMENTS_KEY, settlements)
        self._auditor.record(
            "settlement.finalized",
            json.dumps(
                {
                    "settlement_id": record["settlement_id"],
                    "meter_id": meter_id,
                    "period_start": start,
                    "period_end": end,
                    "total": record["total"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return record

    def verify(self, meter_id: str, period_start: str, period_end: str) -> dict:
        """Check that raw, calibrated and settlement tiers agree."""

        start, end = normalize(period_start), normalize(period_end)
        readings = self._readings.between(meter_id, start, end)
        mismatches = []
        for reading in readings:
            expected = reading["raw"] * reading["factor"]
            if not math.isclose(reading["calibrated"], expected, rel_tol=1e-9, abs_tol=1e-12):
                mismatches.append(
                    {
                        "reading_id": reading["reading_id"],
                        "raw": reading["raw"],
                        "factor": reading["factor"],
                        "calibrated": reading["calibrated"],
                        "expected": expected,
                    }
                )
        computed_total = sum((reading["calibrated"] for reading in readings), 0.0)
        settlement = self._find(
            load_records(self._store, SETTLEMENTS_KEY), meter_id, start, end
        )
        settlement_check = None
        if settlement is not None:
            segments = self._segments(meter_id, start, end)
            settlement_check = {
                "settlement_id": settlement["settlement_id"],
                "status": settlement["status"],
                "stored_total": settlement["total"],
                "computed_total": computed_total,
                "total_ok": math.isclose(
                    settlement["total"], computed_total, rel_tol=1e-9, abs_tol=1e-12
                ),
                "segments_ok": settlement["segments"] == segments,
            }
        ok = not mismatches and (
            settlement_check is None
            or (settlement_check["total_ok"] and settlement_check["segments_ok"])
        )
        return {
            "ok": ok,
            "meter_id": meter_id,
            "period_start": start,
            "period_end": end,
            "readings": len(readings),
            "reading_mismatches": mismatches,
            "computed_total": computed_total,
            "settlement": settlement_check,
        }

    def get(self, meter_id: str, period_start: str, period_end: str) -> dict | None:
        start, end = normalize(period_start), normalize(period_end)
        return self._find(load_records(self._store, SETTLEMENTS_KEY), meter_id, start, end)

    def all(self, meter_id: str = "") -> list[dict]:
        settlements = load_records(self._store, SETTLEMENTS_KEY)
        if meter_id:
            settlements = [s for s in settlements if s["meter_id"] == meter_id]
        return sorted(settlements, key=lambda s: (s["meter_id"], s["period_start"]))

    # -- internals ---------------------------------------------------

    def _period(self, period_start: str, period_end: str) -> tuple[str, str]:
        start, end = normalize(period_start), normalize(period_end)
        if end <= start:
            raise MeteringError("period_end must be after period_start")
        return start, end

    def _find(
        self, settlements: list[dict], meter_id: str, start: str, end: str
    ) -> dict | None:
        for settlement in settlements:
            if (
                settlement["meter_id"] == meter_id
                and settlement["period_start"] == start
                and settlement["period_end"] == end
            ):
                return settlement
        return None

    def _segments(self, meter_id: str, start: str, end: str) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for reading in self._readings.between(meter_id, start, end):
            groups.setdefault(reading["version_id"], []).append(reading)
        segments = []
        for version_id, rows in groups.items():
            segments.append(
                {
                    "version_id": version_id,
                    "factor": rows[0]["factor"],
                    "from": min(row["ts"] for row in rows),
                    "to": max(row["ts"] for row in rows),
                    "readings": len(rows),
                    "subtotal": sum((row["calibrated"] for row in rows), 0.0),
                }
            )
        return sorted(segments, key=lambda segment: segment["from"])
