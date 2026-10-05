"""Behavioural tests for metering calibration management."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.calibration import (
    ConsistencyError,
    ImmutableSettlement,
    IncompatibleVersion,
    Metering,
    ReadingRejected,
    VersionNotFound,
    WorkflowStateError,
)
from waterplant.calibration.core import InjectedFault, load_records, save_records
from waterplant.calibration.pipeline import READINGS_KEY
from waterplant.console.server import Server
from waterplant.flow import Calibration
from waterplant.store import Store

T0 = "2026-10-01T00:00:00Z"
T1 = "2026-10-02T00:00:00Z"
T2 = "2026-10-03T00:00:00Z"


class MeteringCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.metering = Metering(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _meter(self, serial: str = "FM-101", factor: float = 1.0) -> dict:
        return self.metering.meters.register(
            serial, initial_factor=factor, effective_from=T0, now=T0
        )

    def _calibrate(
        self, meter_id: str, factor: float, now: str, policy: str = "IMMEDIATE"
    ) -> dict:
        record = self.metering.calibrations.start(
            meter_id, factor, policy=policy, request_id=f"cal-{factor}-{now}", now=now
        )
        return self.metering.calibrations.apply(record["calibration_id"], now=now)

    # -- archive / version / effective-time binding -----------------

    def test_archive_binds_version_and_effective_time(self) -> None:
        meter = self._meter()
        versions = self.metering.versions.timeline(meter["meter_id"])
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["factor"], 1.0)
        self.assertEqual(versions[0]["effective_from"], T0)
        self.assertEqual(versions[0]["source"], "INITIAL")
        self.assertEqual(versions[0]["status"], "EFFECTIVE")

    def test_version_at_traces_version_in_force(self) -> None:
        meter = self._meter()
        cal = self._calibrate(meter["meter_id"], 1.25, T1)
        before = self.metering.versions.version_at(meter["meter_id"], "2026-10-01T12:00:00Z")
        after = self.metering.versions.version_at(meter["meter_id"], "2026-10-02T12:00:00Z")
        self.assertEqual(before["factor"], 1.0)
        self.assertEqual(after["factor"], 1.25)
        self.assertEqual(after["source"], "CALIBRATION")
        self.assertEqual(after["source_id"], cal["calibration_id"])
        boundary = self.metering.versions.version_at(meter["meter_id"], T1)
        self.assertEqual(boundary["factor"], 1.25)  # 边界点归新版本

    def test_shift_boundary_policy_defers_to_next_shift(self) -> None:
        meter = self._meter()
        cal = self._calibrate(meter["meter_id"], 2.0, "2026-10-02T10:00:00Z", "SHIFT_BOUNDARY")
        self.assertEqual(cal["planned_from"], "2026-10-02T16:00:00Z")
        early = self.metering.versions.version_at(meter["meter_id"], "2026-10-02T15:59:59Z")
        late = self.metering.versions.version_at(meter["meter_id"], "2026-10-02T16:00:00Z")
        self.assertEqual(early["factor"], 1.0)
        self.assertEqual(late["factor"], 2.0)

    # -- replacement: the original bug ------------------------------

    def test_replacement_binds_new_factor_to_new_meter(self) -> None:
        old = self._meter("FM-OLD", 1.0)
        self.metering.readings.ingest(
            old["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="r-old"
        )
        rep = self.metering.replacements.start(
            old["meter_id"], "FM-NEW", 2.5, request_id="rep-1", now=T1
        )
        rep = self.metering.replacements.apply(rep["replacement_id"], now=T1)
        self.assertEqual(rep["status"], "EFFECTIVE")
        self.assertEqual(self.metering.meters.get(old["meter_id"])["status"], "RETIRED")

        new_reading = self.metering.readings.ingest(
            rep["new_meter_id"], "2026-10-02T08:00:00Z", 100.0, idempotency_key="r-new"
        )
        self.assertEqual(new_reading["factor"], 2.5)
        self.assertEqual(new_reading["calibrated"], 250.0)

        # 旧表历史读数保持旧系数，不按新系数重算
        old_reading = self.metering.readings.all(old["meter_id"])[0]
        self.assertEqual(old_reading["factor"], 1.0)
        self.assertEqual(old_reading["calibrated"], 100.0)

        # 旧表生效点之后的读数无版本可用；生效点前的迟到数据仍按旧系数
        with self.assertRaises(VersionNotFound):
            self.metering.readings.ingest(
                old["meter_id"], "2026-10-02T08:00:00Z", 1.0, idempotency_key="r-late-reject"
            )
        late = self.metering.readings.ingest(
            old["meter_id"], "2026-10-01T23:00:00Z", 10.0, idempotency_key="r-late-ok"
        )
        self.assertEqual(late["factor"], 1.0)

        # 加药系数跟随当前生效版本
        self.assertEqual(Calibration(self.store).current(), 2.5)

    def test_replacement_shift_boundary(self) -> None:
        old = self._meter("FM-OLD", 1.0)
        rep = self.metering.replacements.start(
            old["meter_id"], "FM-NEW", 2.5, policy="SHIFT_BOUNDARY", request_id="rep-2", now="2026-10-02T09:00:00Z"
        )
        rep = self.metering.replacements.apply(rep["replacement_id"], now="2026-10-02T09:00:00Z")
        self.assertEqual(rep["planned_from"], "2026-10-02T16:00:00Z")
        before = self.metering.versions.version_at(old["meter_id"], "2026-10-02T15:00:00Z")
        self.assertEqual(before["factor"], 1.0)
        after = self.metering.versions.version_at(rep["new_meter_id"], "2026-10-02T16:00:00Z")
        self.assertEqual(after["factor"], 2.5)

    # -- reader compatibility while versions coexist -----------------

    def test_reader_accepts_coexisting_versions(self) -> None:
        meter = self._meter()
        old_version = self.metering.versions.current(meter["meter_id"])
        cal = self._calibrate(meter["meter_id"], 1.5, T1)
        new_version_id = cal["version_id"]

        accepted = {
            v["version_id"]
            for v in self.metering.versions.accepted(meter["meter_id"], "2026-10-02T01:00:00Z")
        }
        self.assertIn(old_version["version_id"], accepted)
        self.assertIn(new_version_id, accepted)

        # 迟到数据带旧版本标签：接受，按事件时间仍命中旧版本
        late = self.metering.readings.ingest(
            meter["meter_id"],
            "2026-10-01T23:00:00Z",
            10.0,
            idempotency_key="c1",
            client_version_id=old_version["version_id"],
            now="2026-10-02T01:00:00Z",
        )
        self.assertEqual(late["version_id"], old_version["version_id"])

        # 标签与事件时间冲突：以事件时间为准，落审计
        mismatched = self.metering.readings.ingest(
            meter["meter_id"],
            "2026-10-02T02:00:00Z",
            10.0,
            idempotency_key="c2",
            client_version_id=old_version["version_id"],
            now="2026-10-02T02:00:00Z",
        )
        self.assertEqual(mismatched["version_id"], new_version_id)
        self.assertEqual(mismatched["client_version_id"], old_version["version_id"])
        mismatches = Auditor(self.store).filter("reading.version_mismatch")
        self.assertEqual(len(mismatches), 1)

        # 窗口外的未知标签：拒绝
        with self.assertRaises(IncompatibleVersion):
            self.metering.readings.ingest(
                meter["meter_id"],
                "2026-10-02T03:00:00Z",
                10.0,
                idempotency_key="c3",
                client_version_id="bogus-version",
                now="2026-10-02T03:00:00Z",
            )

    # -- immutability -------------------------------------------------

    def test_history_is_never_recomputed(self) -> None:
        meter = self._meter()
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="h1"
        )
        self._calibrate(meter["meter_id"], 3.0, T1)
        stored = self.metering.readings.all(meter["meter_id"])[0]
        self.assertEqual(stored["factor"], 1.0)
        self.assertEqual(stored["calibrated"], 100.0)

    def test_finalized_settlement_is_immutable(self) -> None:
        meter = self._meter()
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="f1"
        )
        record = self.metering.settlements.finalize(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", now=T1
        )
        self.assertEqual(record["status"], "FINALIZED")
        with self.assertRaises(ImmutableSettlement):
            self.metering.settlements.settle(
                meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
            )
        again = self.metering.settlements.finalize(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
        )
        self.assertEqual(again["settlement_id"], record["settlement_id"])
        with self.assertRaises(ReadingRejected):
            self.metering.readings.ingest(
                meter["meter_id"], "2026-10-01T12:00:00Z", 5.0, idempotency_key="f2"
            )

    # -- partial failure and recovery ---------------------------------

    def test_calibration_partial_failure_recovers_from_every_step(self) -> None:
        for step in ("stage", "close", "activate", "finalize"):
            with self.subTest(step=step):
                self.store.clear()
                meter = self._meter()
                record = self.metering.calibrations.start(
                    meter["meter_id"], 1.25, request_id=f"cal-{step}", now=T1
                )
                with self.assertRaises(InjectedFault):
                    self.metering.calibrations.apply(
                        record["calibration_id"], now=T1, fail_at=(step,)
                    )
                self.assertEqual(
                    self.metering.calibrations.get(record["calibration_id"])["status"], "FAILED"
                )
                with self.assertRaises(WorkflowStateError):
                    self.metering.calibrations.apply(record["calibration_id"], now=T1)

                recovered = self.metering.calibrations.recover(record["calibration_id"], now=T1)
                self.assertEqual(recovered["status"], "EFFECTIVE")
                # 恢复幂等：再恢复一次状态不变
                again = self.metering.calibrations.recover(record["calibration_id"], now=T1)
                self.assertEqual(again, recovered)

                versions = self.metering.versions.timeline(meter["meter_id"])
                self.assertEqual(len(versions), 2)
                self.assertEqual(versions[0]["effective_to"], T1)
                self.assertEqual(versions[1]["effective_from"], T1)
                self.assertEqual(versions[1]["status"], "EFFECTIVE")
                applied = Auditor(self.store).filter("calibration.applied")
                self.assertEqual(len(applied), 1)

    def test_replacement_partial_failure_recovers(self) -> None:
        old = self._meter("FM-OLD", 1.0)
        rep = self.metering.replacements.start(
            old["meter_id"], "FM-NEW", 2.5, request_id="rep-fail", now=T1
        )
        with self.assertRaises(InjectedFault):
            self.metering.replacements.apply(
                rep["replacement_id"], now=T1, fail_at=("close",)
            )
        self.assertEqual(
            self.metering.replacements.get(rep["replacement_id"])["status"], "FAILED"
        )
        # 部分失败现场保留：新表档案与 PENDING 版本已暂存，旧版本仍生效
        self.assertIsNotNone(
            self.metering.versions.staged_for("REPLACEMENT", rep["replacement_id"])
        )
        self.assertEqual(self.metering.meters.get(old["meter_id"])["status"], "ACTIVE")

        recovered = self.metering.replacements.recover(rep["replacement_id"], now=T1)
        self.assertEqual(recovered["status"], "EFFECTIVE")
        self.assertEqual(self.metering.meters.get(old["meter_id"])["status"], "RETIRED")
        self.assertEqual(len(self.metering.meters.all()), 2)
        new_versions = self.metering.versions.timeline(recovered["new_meter_id"])
        self.assertEqual(len(new_versions), 1)

    def test_crash_between_stage_and_record_update_recovers(self) -> None:
        meter = self._meter()
        record = self.metering.calibrations.start(
            meter["meter_id"], 1.25, request_id="cal-crash", now=T1
        )
        # 模拟 stage 写版本后、写记录前崩溃：手动暂存一个孤儿版本
        staged = self.metering.versions.stage(
            meter["meter_id"], 1.25, T1, "CALIBRATION", record["calibration_id"], T1
        )
        applied = self.metering.calibrations.apply(record["calibration_id"], now=T1)
        self.assertEqual(applied["status"], "EFFECTIVE")
        self.assertEqual(applied["version_id"], staged["version_id"])
        self.assertEqual(len(self.metering.versions.timeline(meter["meter_id"])), 2)

    # -- idempotency ----------------------------------------------------

    def test_ingest_and_start_are_idempotent(self) -> None:
        meter = self._meter()
        first = self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="i1"
        )
        second = self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="i1"
        )
        self.assertEqual(first["reading_id"], second["reading_id"])
        self.assertEqual(len(self.metering.readings.all()), 1)

        cal1 = self.metering.calibrations.start(
            meter["meter_id"], 1.5, request_id="same-request", now=T1
        )
        cal2 = self.metering.calibrations.start(
            meter["meter_id"], 1.5, request_id="same-request", now=T1
        )
        self.assertEqual(cal1["calibration_id"], cal2["calibration_id"])

    def test_open_settlement_recompute_is_idempotent(self) -> None:
        meter = self._meter()
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="s1"
        )
        first = self.metering.settlements.settle(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", now=T1
        )
        second = self.metering.settlements.settle(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", now=T1
        )
        self.assertEqual(first, second)
        self.assertEqual(second["revision"], 1)

        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T09:00:00Z", 50.0, idempotency_key="s2"
        )
        third = self.metering.settlements.settle(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", now=T1
        )
        self.assertEqual(third["revision"], 2)
        self.assertEqual(third["total"], 150.0)

    # -- pinned settlement rule across two versions ---------------------

    def test_period_spanning_switch_settles_in_segments(self) -> None:
        meter = self._meter()
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="p1"
        )
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T20:00:00Z", 100.0, idempotency_key="p2"
        )
        self._calibrate(meter["meter_id"], 2.0, T1)
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-02T08:00:00Z", 100.0, idempotency_key="p3"
        )
        record = self.metering.settlements.settle(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z", now=T2
        )
        self.assertEqual(len(record["segments"]), 2)
        old_segment, new_segment = record["segments"]
        self.assertEqual(old_segment["factor"], 1.0)
        self.assertEqual(old_segment["readings"], 2)
        self.assertEqual(old_segment["subtotal"], 200.0)
        self.assertEqual(new_segment["factor"], 2.0)
        self.assertEqual(new_segment["readings"], 1)
        self.assertEqual(new_segment["subtotal"], 200.0)
        self.assertEqual(record["total"], 400.0)

    # -- three-tier consistency -----------------------------------------

    def test_consistency_verify_and_tamper_detection(self) -> None:
        meter = self._meter()
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="v1"
        )
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T09:00:00Z", 60.0, idempotency_key="v2"
        )
        self.metering.settlements.finalize(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", now=T1
        )
        report = self.metering.settlements.verify(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["computed_total"], 160.0)
        self.assertEqual(report["settlement"]["status"], "FINALIZED")

        readings = load_records(self.store, READINGS_KEY)
        readings[0]["calibrated"] += 1.0
        save_records(self.store, READINGS_KEY, readings)
        broken = self.metering.settlements.verify(
            meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
        )
        self.assertFalse(broken["ok"])
        self.assertEqual(len(broken["reading_mismatches"]), 1)
        self.assertFalse(broken["settlement"]["total_ok"])

    def test_finalize_rejects_inconsistent_period(self) -> None:
        meter = self._meter()
        self.metering.readings.ingest(
            meter["meter_id"], "2026-10-01T08:00:00Z", 100.0, idempotency_key="x1"
        )
        readings = load_records(self.store, READINGS_KEY)
        readings[0]["calibrated"] = 999.0
        save_records(self.store, READINGS_KEY, readings)
        with self.assertRaises(ConsistencyError):
            self.metering.settlements.finalize(
                meter["meter_id"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
            )

    # -- audit trail ------------------------------------------------------

    def test_calibration_and_replacement_are_audited(self) -> None:
        meter = self._meter("FM-OLD", 1.0)
        cal = self._calibrate(meter["meter_id"], 1.5, T1)
        rep = self.metering.replacements.start(
            meter["meter_id"], "FM-NEW", 2.5, request_id="rep-audit", now=T2
        )
        self.metering.replacements.apply(rep["replacement_id"], now=T2)

        auditor = Auditor(self.store)
        kinds = auditor.kinds()
        for expected in (
            "calibration.created",
            "calibration.staged",
            "calibration.applied",
            "replacement.created",
            "replacement.staged",
            "replacement.applied",
            "version.activated",
        ):
            self.assertIn(expected, kinds)
        applied = json.loads(auditor.filter("calibration.applied")[0].detail)
        self.assertEqual(applied["calibration_id"], cal["calibration_id"])
        self.assertEqual(applied["factor"], 1.5)


class MeteringConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        return response.status, json.loads(response.body.decode("utf-8"))

    def test_console_calibration_round_trip(self) -> None:
        status, body = self.call(
            "POST", "/metering/meter", {"serial": "FM-101", "factor": 1.0, "effective_from": "2026-10-01T00:00:00Z"}
        )
        self.assertEqual(status, 200)
        meter = body["meter"]

        status, body = self.call(
            "POST",
            "/metering/calibration/start",
            {"meter_id": meter["meter_id"], "factor": 1.25, "request_id": "cal-web-1"},
        )
        self.assertEqual(status, 200)
        calibration = body["calibration"]
        self.assertEqual(calibration["status"], "VALIDATED")

        status, body = self.call(
            "POST", "/metering/calibration/apply", {"calibration_id": calibration["calibration_id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["calibration"]["status"], "EFFECTIVE")

        status, body = self.call("GET", "/metering/timeline", {"meter_id": meter["meter_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["versions"]), 2)

        status, body = self.call("GET", "/metering/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"]["meters"], 1)
        self.assertEqual(body["state"]["versions"], 2)

    def test_console_reading_settlement_and_verify(self) -> None:
        _, body = self.call("POST", "/metering/meter", {"serial": "FM-101", "factor": 2.0, "effective_from": "2026-10-01T00:00:00Z"})
        meter = body["meter"]
        _, body = self.call(
            "POST",
            "/metering/reading",
            {"meter_id": meter["meter_id"], "ts": "2026-10-01T08:00:00Z", "raw": 100.0,
             "idempotency_key": "web-r1"},
        )
        self.assertEqual(body["reading"]["calibrated"], 200.0)

        status, body = self.call(
            "POST",
            "/metering/finalize",
            {"meter_id": meter["meter_id"], "period_start": "2026-10-01T00:00:00Z",
             "period_end": "2026-10-02T00:00:00Z"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["settlement"]["status"], "FINALIZED")
        self.assertEqual(body["settlement"]["total"], 200.0)

        status, body = self.call(
            "POST",
            "/metering/verify",
            {"meter_id": meter["meter_id"], "period_start": "2026-10-01T00:00:00Z",
             "period_end": "2026-10-02T00:00:00Z"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["report"]["ok"])

        # 终态后重算被拒绝
        status, body = self.call(
            "POST",
            "/metering/settle",
            {"meter_id": meter["meter_id"], "period_start": "2026-10-01T00:00:00Z",
             "period_end": "2026-10-02T00:00:00Z"},
        )
        self.assertEqual(status, 400)

    def test_console_replacement_and_audit_visibility(self) -> None:
        _, body = self.call("POST", "/metering/meter", {"serial": "FM-OLD", "factor": 1.0, "effective_from": "2026-10-01T00:00:00Z"})
        old = body["meter"]
        status, body = self.call(
            "POST",
            "/metering/replacement/start",
            {"old_meter_id": old["meter_id"], "new_serial": "FM-NEW", "factor": 2.5,
             "request_id": "rep-web-1"},
        )
        self.assertEqual(status, 200)
        replacement = body["replacement"]
        status, body = self.call(
            "POST", "/metering/replacement/apply", {"replacement_id": replacement["replacement_id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["replacement"]["status"], "EFFECTIVE")

        status, body = self.call("GET", "/metering/replacements")
        self.assertEqual(len(body["replacements"]), 1)
        status, body = self.call("POST", "/audit/filter", {"kind": "replacement.applied"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
