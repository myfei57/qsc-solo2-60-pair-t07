"""Tests for versioned flow meter calibration management."""

from __future__ import annotations

import json
import tempfile
import time
import unittest

from waterplant.audit import Auditor
from waterplant.flow import CalibrationError, CalibrationManager, validate_factor
from waterplant.store import Store


class CalibrationManagementCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.manager = CalibrationManager(self.store)
        self.baseline = self.manager.ensure_baseline()
        self.t = int(time.time())
        self.boundary = self.t + 100

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_meter_version_and_effective_time_are_bound(self) -> None:
        change = self.manager.replace_meter(
            "fm-2",
            1.5,
            mode="shift_boundary",
            effective_at=self.boundary,
            model="magflow",
            location="inlet",
            request_id="replace-1",
        )
        version = self.manager.version(change.version_id)
        meter = self.manager.meter("fm-2")

        self.assertIsNotNone(version)
        self.assertIsNotNone(meter)
        self.assertEqual(version.meter_serial, "fm-2")
        self.assertEqual(version.factor, 1.5)
        self.assertEqual(version.effective_at, self.boundary)
        self.assertEqual(change.status, "scheduled")
        self.assertEqual(change.previous_version_id, self.baseline.version_id)
        self.assertEqual(meter.model, "magflow")
        self.assertEqual(self.manager.current_version(at=self.t).version_id, self.baseline.version_id)
        self.assertEqual(self.manager.current(at=self.t), 1.0)

        repeated = self.manager.replace_meter(
            "fm-2", 1.5, mode="shift_boundary", effective_at=self.boundary, request_id="replace-1"
        )
        self.assertEqual(repeated.change_id, change.change_id)
        self.assertEqual(len(self.manager.changes()), 1)

    def test_immediate_and_shift_boundary_settlement_rules_are_fixed(self) -> None:
        scheduled = self.manager.replace_meter(
            "fm-scheduled", 2.0, mode="shift_boundary", effective_at=self.boundary
        )

        before_old = self.manager.record_reading(
            10.0, observed_at=self.boundary - 1, meter_serial="legacy-meter", at=self.t + 50
        )
        before_new = self.manager.record_reading(
            10.0, observed_at=self.boundary - 1, meter_serial="fm-scheduled", at=self.t + 50
        )
        before_untagged = self.manager.record_reading(
            10.0, observed_at=self.boundary - 1, at=self.t + 50, reading_id="untagged-before"
        )

        self.assertEqual(
            {item.version_id for item in before_untagged.calibrated},
            {self.baseline.version_id, scheduled.version_id},
        )
        self.assertEqual(before_untagged.settlement.version_id, self.baseline.version_id)

        self.assertEqual(
            {item.version_id for item in before_old.calibrated},
            {self.baseline.version_id, scheduled.version_id},
        )
        self.assertEqual(before_old.settlement.version_id, self.baseline.version_id)
        self.assertEqual(before_old.settlement.settlement_value, 10.0)
        self.assertEqual(
            {item.version_id for item in before_new.calibrated},
            {scheduled.version_id, self.baseline.version_id},
        )
        self.assertEqual(before_new.settlement.version_id, self.baseline.version_id)

        self.manager.activate_due(at=self.boundary)
        at_boundary = self.manager.record_reading(
            10.0, observed_at=self.boundary, meter_serial="legacy-meter", at=self.boundary
        )
        self.assertEqual(at_boundary.settlement.version_id, scheduled.version_id)
        self.assertEqual(at_boundary.settlement.settlement_value, 20.0)
        self.assertEqual(self.manager.current(at=self.boundary), 2.0)

        # Activation and recompute must never move an already settled historical row.
        self.manager.recompute_reading(before_old.raw.reading_id, at=self.boundary)
        self.manager.recompute_pending(at=self.boundary)
        refreshed_old = self.manager.reading(before_old.raw.reading_id)
        self.assertEqual(refreshed_old.settlement.version_id, self.baseline.version_id)
        self.assertEqual(refreshed_old.settlement.settlement_value, 10.0)

    def test_post_change_coexistence_accepts_old_and_new_but_settles_new(self) -> None:
        change = self.manager.replace_meter("fm-immediate", 3.0, mode="immediate")
        effective = change.effective_at
        reading = self.manager.record_reading(
            10.0,
            observed_at=effective + 10,
            meter_serial="legacy-meter",
            at=effective + 10,
        )

        self.assertEqual(
            {item.version_id for item in reading.calibrated},
            {self.baseline.version_id, change.version_id},
        )
        self.assertEqual(reading.settlement.version_id, change.version_id)
        self.assertEqual(reading.settlement.settlement_value, 30.0)

    def test_partial_failure_is_recoverable_and_recovery_is_idempotent(self) -> None:
        with self.assertRaises(CalibrationError) as failed:
            self.manager.run_calibration_job(
                "fm-recovered",
                samples=[{"raw": 10.0, "reference": 15.0}],
                mode="immediate",
                fail_before="activate",
                job_id="job-1",
                request_id="job-request",
            )
        self.assertEqual(failed.exception.stage, "activate")
        job = self.manager.job("job-1")
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.last_stage, "create_change")
        self.assertIsNotNone(self.manager.meter("fm-recovered"))
        self.assertIsNotNone(self.manager.version(job.version_id))

        recovered = self.manager.recover_job("job-1", at=job.effective_at)
        recovered_again = self.manager.recover_job("job-1", at=job.effective_at)
        self.assertEqual(recovered.status, "complete")
        self.assertEqual(recovered_again.status, "complete")
        self.assertEqual(recovered_again.change_id, recovered.change_id)
        self.assertEqual(self.manager.current(at=job.effective_at), 1.5)
        self.assertEqual(len(self.manager.jobs()), 1)
        self.assertEqual(len(self.manager.changes()), 1)

        restarted = self.manager.run_calibration_job(
            "fm-recovered",
            samples=[{"raw": 10.0, "reference": 15.0}],
            mode="immediate",
            job_id="job-1",
            request_id="job-request",
        )
        self.assertEqual(restarted.status, "complete")
        self.assertEqual(len(self.manager.jobs()), 1)

    def test_reading_and_recompute_are_idempotent_and_consistent(self) -> None:
        first = self.manager.record_reading(
            12.0,
            observed_at=self.t,
            meter_serial="legacy-meter",
            reading_id="reading-fixed",
            request_id="reading-request",
        )
        second = self.manager.record_reading(
            999.0,
            observed_at=self.t,
            meter_serial="legacy-meter",
            reading_id="reading-fixed",
            request_id="reading-request",
        )
        self.assertEqual(first.raw.reading_id, second.raw.reading_id)
        self.assertEqual(second.raw.raw_value, 12.0)
        self.assertEqual(len(self.manager.readings()), 1)

        result_a = self.manager.recompute_reading("reading-fixed", at=self.t)
        result_b = self.manager.recompute_reading("reading-fixed", at=self.t + 1)
        self.assertEqual(result_a.settlement.settlement_value, 12.0)
        self.assertEqual(result_b.settlement.settlement_value, 12.0)
        self.assertEqual(len(self.manager.readings()), 1)
        self.assertTrue(self.manager.consistency_check()["ok"])

    def test_scheduled_job_waits_until_shift_boundary(self) -> None:
        job = self.manager.run_calibration_job(
            "fm-scheduled-job",
            samples=[{"raw": 10.0, "reference": 20.0}],
            mode="shift_boundary",
            effective_at=self.boundary,
            job_id="scheduled-job",
        )
        self.assertEqual(job.status, "scheduled")
        self.assertEqual(self.manager.recover_job("scheduled-job", at=self.boundary - 1).status, "scheduled")
        recovered = self.manager.recover_job("scheduled-job", at=self.boundary)
        self.assertEqual(recovered.status, "complete")
        self.assertEqual(self.manager.current(at=self.boundary), 2.0)

    def test_factor_validation_and_calibration_samples(self) -> None:
        version = self.manager.calibrate(
            "fm-sample",
            samples=[{"raw": 10.0, "reference": 15.0}, {"raw": 20.0, "reference": 25.0}],
        )
        self.assertAlmostEqual(version.factor, 1.375)
        validate_factor(1.0)
        with self.assertRaises(ValueError):
            validate_factor(0.0)

    def test_change_events_are_audited(self) -> None:
        self.manager.replace_meter("fm-audit", 2.5, mode="immediate", request_id="audit")
        try:
            self.manager.run_calibration_job(
                "fm-job-audit",
                samples=[{"raw": 2.0, "reference": 4.0}],
                mode="immediate",
                fail_before="complete",
                job_id="job-audit",
            )
        except CalibrationError:
            pass
        self.manager.recover_job("job-audit")
        auditor = Auditor(self.store)
        kinds = set(auditor.count_by_kind())
        self.assertIn("meter_replace", kinds)
        self.assertIn("calibration", kinds)


class CalibrationConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        from waterplant.console.seed import seed_defaults
        from waterplant.console.server import Server

        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        parsed = json.loads(response.body.decode("utf-8"))
        return response.status, parsed

    def test_calibration_console_archive_and_readings(self) -> None:
        boundary = int(time.time()) + 100
        status, change_body = self.call("POST", "/flow/replace", {
            "serial": "fm-console",
            "factor": 2.0,
            "mode": "shift_boundary",
            "effective_at": boundary,
        })
        self.assertEqual(status, 200)
        self.assertEqual(change_body["change"]["status"], "scheduled")

        status, reading = self.call("POST", "/flow/readings", {
            "raw_value": 10.0,
            "observed_at": boundary - 1,
            "meter_serial": "fm-console",
            "at": boundary - 1,
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(reading["reading"]["calibrated"]), 2)
        self.assertEqual(reading["reading"]["settlement"]["factor"], 1.0)

        status, activated = self.call("POST", "/flow/activate-due", {"at": boundary})
        self.assertEqual(status, 200)
        self.assertEqual(len(activated["activated"]), 1)

        status, listing = self.call("GET", "/flow/readings")
        self.assertEqual(status, 200)
        self.assertTrue(listing["consistency"]["ok"])

        status, archive = self.call("GET", "/flow/archive")
        self.assertEqual(status, 200)
        self.assertTrue(any(meter["serial"] == "fm-console" for meter in archive["meters"]))
        self.assertTrue(any(version["factor"] == 2.0 for version in archive["versions"]))

        status, audit = self.call("GET", "/audit")
        self.assertEqual(status, 200)
        self.assertTrue(any(entry["kind"] == "meter_replace" for entry in audit["entries"]))

        response = self.server.dispatch("GET", "/export/calibration-audit")
        self.assertEqual(response.status, 200)
        self.assertIn("meter_replace", response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
