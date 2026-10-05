"""HTTP handlers for every console endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.chlor import validate_demand
from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import (
    DEFAULT_BOUNDARY,
    CalibrationError,
    calibrate_meter,
    factor_from_samples,
    validate_factor,
    validate_samples,
    validate_serial,
)
from waterplant.intake import DEFAULT_WINDOW, Sensor, validate_flow
from waterplant.inventory import validate_quantity
from waterplant.ns import Stage, treatment_line
from waterplant.ph import validate_ph
from waterplant.quota import Quota, check_quota, validate_amount
from waterplant.reporting import audit_export, calibration_audit_export, telemetry_export, to_csv
from waterplant.scheduler import validate_threshold
from waterplant.store import export_state

from . import checks, history, ops
from . import describe as describe_module
from . import simulate as simulate_module
from . import snapshot as snapshot_module
from . import telemetry as telemetry_module
from .cycle import run_cycle
from .http import Request, Response, csv_response, json_response, text_response
from .report import text_report
from .routes import route_table
from .system import system_payload
from .version import version_payload

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def health(server: "Server", request: Request) -> Response:
    store = server.runtime.store
    return json_response(
        {"status": "ok", "keys": store.keys(), "size": store.count(), "data": export_state(store)}
    )


def health_checks(server: "Server", request: Request) -> Response:
    return json_response({"checks": checks.run_checks(server.runtime)})


def snapshot(server: "Server", request: Request) -> Response:
    return json_response(snapshot_module.collect(server.runtime))


def describe(server: "Server", request: Request) -> Response:
    return json_response(describe_module.collect(server.runtime))


def report(server: "Server", request: Request) -> Response:
    return text_response(text_report(server.runtime))


def pipeline(server: "Server", request: Request) -> Response:
    line = treatment_line()
    last = line.last()
    return json_response(
        {
            "name": line.name,
            "steps": [step.as_dict() for step in line.steps()],
            "ordered": line.before(Stage.INTAKE, Stage.COAG),
            "last_stage": "" if last is None else last.value,
        }
    )


def catalog(server: "Server", request: Request) -> Response:
    return json_response({"routes": [route.as_dict() for route in route_table()]})


def ops_counters(server: "Server", request: Request) -> Response:
    return json_response(ops.collect(server.runtime).as_dict())


def ops_reset_store(server: "Server", request: Request) -> Response:
    return json_response(ops.reset_store(server.runtime))


def telemetry(server: "Server", request: Request) -> Response:
    return json_response(telemetry_module.collect(server.runtime).as_dict())


def version(server: "Server", request: Request) -> Response:
    return json_response(version_payload())


def system(server: "Server", request: Request) -> Response:
    return json_response(system_payload())


def history_append(server: "Server", request: Request) -> Response:
    return history.append(server.runtime.store, request)


def history_list(server: "Server", request: Request) -> Response:
    return history.listing(server.runtime.store)


def history_clear(server: "Server", request: Request) -> Response:
    return history.clear(server.runtime.store)


def intake_flow(server: "Server", request: Request) -> Response:
    sensor = Sensor.from_payload(request.payload)
    validate_flow(sensor.flow)
    server.runtime.flow_repository.record(sensor)
    server.runtime.trend.record(sensor.flow)
    return json_response({"flow": sensor.flow, "turbidity": sensor.turbidity})


def intake_flow_get(server: "Server", request: Request) -> Response:
    value, present = server.runtime.flow_repository.load_flow()
    return json_response({"flow": value, "ok": present})


def coag_dose(server: "Server", request: Request) -> Response:
    flow = request.float_field("flow")
    validate_flow(flow)
    dose = server.runtime.coag_doser.update_flow_and_dose(flow)
    return json_response({"dose": dose})


def coag_turbidity(server: "Server", request: Request) -> Response:
    dose = server.runtime.sampler.judge(request.float_list("samples"))
    return json_response({"dose": dose})


def coag_ratio(server: "Server", request: Request) -> Response:
    return json_response({"ratio": server.runtime.coag_doser.current_ratio()})


def flow_replace(server: "Server", request: Request) -> Response:
    factor = request.float_field("factor")
    validate_factor(factor)
    serial = request.str_field("serial", "legacy-meter")
    mode = request.str_field("mode", "immediate")
    shift_boundary = request.str_field("shift_boundary", DEFAULT_BOUNDARY)
    effective_at = _optional_float(request, "effective_at")
    compatibility_until = _optional_float(request, "compatibility_until")
    request_id = request.str_field("request_id")
    meter = calibrate_meter(serial, factor)
    change = server.runtime.calibration.replace_meter(
        serial,
        factor,
        mode=mode,
        effective_at=None if effective_at is None else int(effective_at),
        shift_boundary=shift_boundary,
        compatibility_until=None if compatibility_until is None else int(compatibility_until),
        request_id=request_id,
    )
    version = server.runtime.calibration.version(change.version_id)
    return json_response({
        "meter": meter.as_dict(),
        "change": change.as_dict(),
        "version": None if version is None else version.as_dict(),
    })


def flow_archive(server: "Server", request: Request) -> Response:
    manager = server.runtime.calibration
    meters = manager.meters()
    versions = manager.versions()
    changes = manager.changes()
    return json_response({
        "meters": [meter.as_dict() for meter in meters],
        "versions": [version.as_dict() for version in versions],
        "changes": [change.as_dict() for change in changes],
        "current_version": (
            None
            if manager.current_version() is None
            else manager.current_version().as_dict()
        ),
        "pending_changes": [change.as_dict() for change in manager.pending_changes()],
    })


def flow_meters_register(server: "Server", request: Request) -> Response:
    serial = request.str_field("serial")
    validate_serial(serial)
    commissioned_at = _optional_float(request, "commissioned_at")
    attributes = request.payload.get("attributes", {})
    if attributes is not None and not isinstance(attributes, dict):
        raise ValueError("attributes must be an object")
    meter = server.runtime.calibration.register_meter(
        serial,
        model=request.str_field("model"),
        manufacturer=request.str_field("manufacturer"),
        location=request.str_field("location"),
        commissioned_at=None if commissioned_at is None else int(commissioned_at),
        attributes=attributes or {},
        request_id=request.str_field("request_id"),
    )
    return json_response({"meter": meter.as_dict()})


def flow_versions_create(server: "Server", request: Request) -> Response:
    samples = _calibration_samples(request)
    factor_value = _optional_float(request, "factor")
    factor = factor_from_samples(samples) if factor_value is None else factor_value
    validate_factor(factor)
    version = server.runtime.calibration.calibrate(
        request.str_field("serial"),
        samples=samples,
        basis=request.str_field("basis", "samples"),
        factor=factor,
        version_id=request.str_field("version_id"),
        request_id=request.str_field("request_id"),
    )
    return json_response({"version": version.as_dict()})


def flow_calibrations_start(server: "Server", request: Request) -> Response:
    samples = _calibration_samples(request)
    factor_value = _optional_float(request, "factor")
    effective_at_value = _optional_float(request, "effective_at")
    try:
        job = server.runtime.calibration.run_calibration_job(
            serial=request.str_field("serial"),
            samples=samples,
            mode=request.str_field("mode", "immediate"),
            shift_boundary=request.str_field("shift_boundary", DEFAULT_BOUNDARY),
            model=request.str_field("model"),
            manufacturer=request.str_field("manufacturer"),
            location=request.str_field("location"),
            factor=factor_value,
            fail_before=request.str_field("fail_before"),
            job_id=request.str_field("job_id"),
            request_id=request.str_field("request_id"),
            effective_at=None if effective_at_value is None else int(effective_at_value),
        )
    except CalibrationError as exc:
        job = server.runtime.calibration.job(exc.job_id)
        if job is None:
            raise
        raise RequestError(409, f"calibration failed before {exc.stage}; job_id={exc.job_id}") from exc
    return json_response({"job": job.as_dict()})


def flow_calibrations_recover(server: "Server", request: Request) -> Response:
    job = server.runtime.calibration.recover_job(
        request.str_field("job_id"),
        None if _optional_float(request, "at") is None else int(_optional_float(request, "at")),
    )
    return json_response({"job": job.as_dict()})


def flow_activate_due(server: "Server", request: Request) -> Response:
    at = _optional_float(request, "at")
    changes = server.runtime.calibration.activate_due(None if at is None else int(at))
    result = server.runtime.calibration.recompute_pending(
        None if at is None else int(at)
    )
    return json_response({
        "activated": [change.as_dict() for change in changes],
        "recompute": result,
    })


def flow_readings(server: "Server", request: Request) -> Response:
    manager = server.runtime.calibration
    return json_response({
        "readings": [reading.as_dict() for reading in manager.readings()],
        "pending": [reading.raw.reading_id for reading in manager.pending_readings()],
        "consistency": manager.consistency_check(),
    })


def flow_readings_record(server: "Server", request: Request) -> Response:
    observed_at = _optional_float(request, "observed_at")
    at = _optional_float(request, "at")
    reading = server.runtime.calibration.record_reading(
        raw_value=request.float_field("raw_value"),
        observed_at=None if observed_at is None else int(observed_at),
        meter_serial=request.str_field("meter_serial"),
        source_version_id=request.str_field("source_version_id"),
        settlement_version_id=request.str_field("settlement_version_id"),
        reading_id=request.str_field("reading_id"),
        quality=request.str_field("quality", "good"),
        request_id=request.str_field("request_id"),
        at=None if at is None else int(at),
    )
    return json_response({"reading": reading.as_dict()})


def flow_readings_recompute(server: "Server", request: Request) -> Response:
    at = _optional_float(request, "at")
    reading_id = request.str_field("reading_id")
    if reading_id:
        reading = server.runtime.calibration.recompute_reading(
            reading_id, None if at is None else int(at)
        )
        return json_response({"reading": reading.as_dict()})
    return json_response(server.runtime.calibration.recompute_pending(None if at is None else int(at)))


def flow_consistency(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.calibration.consistency_check())


def _calibration_samples(request: Request) -> list[dict[str, float]]:
    raw_samples = request.payload.get("samples", [])
    if not isinstance(raw_samples, list):
        raise ValueError("samples must be a list of {raw, reference} objects")
    samples: list[dict[str, float]] = []
    for sample in raw_samples:
        if not isinstance(sample, dict):
            raise ValueError("each sample must be an object")
        try:
            samples.append({"raw": float(sample["raw"]), "reference": float(sample["reference"])})
        except KeyError as exc:
            raise ValueError("each sample requires raw and reference") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError("raw and reference sample values must be numbers") from exc
    validate_samples(samples)
    return samples


def _optional_float(request: Request, name: str) -> float | None:
    if name not in request.payload or request.payload[name] is None:
        return None
    return request.float_field(name)


def chlor_target(server: "Server", request: Request) -> Response:
    demand = request.float_field("demand")
    validate_demand(demand)
    target = server.runtime.well.update_residual_demand(demand)
    return json_response({"target": target})


def chlor_dose(server: "Server", request: Request) -> Response:
    return json_response({"dose": server.runtime.chlor_doser.apply_residual()})


def clearwell_level(server: "Server", request: Request) -> Response:
    target = request.float_field("target")
    validate_level(target)
    runtime = server.runtime
    minimum = runtime.well.adjust_level(target, runtime.inlet, runtime.outlet)
    return json_response({"min_level": minimum, "level": runtime.well.level()})


def filter_add(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    bed = server.runtime.bank.add_bed(
        request.str_field("id"), zone, request.float_field("load")
    )
    return json_response({"id": bed.id, "zone": bed.zone, "load": bed.load})


def filter_close(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.close(bed_id)
    return json_response({"closed": bed_id})


def filter_open(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.open(bed_id)
    return json_response({"opened": bed_id})


def filter_renumber(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    bed_id = request.str_field("id")
    server.runtime.bank.renumber(bed_id, zone)
    return json_response({"renumbered": bed_id, "zone": zone})


def filter_load(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    load = request.float_field("load")
    if load < 0:
        raise ValueError("load must be non-negative")
    server.runtime.bank.set_load(bed_id, load)
    return json_response({"id": bed_id, "load": load})


def filter_reset(server: "Server", request: Request) -> Response:
    server.runtime.bank.reset_closed()
    return json_response({"reset": True})


def filter_remove(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.remove_bed(bed_id)
    return json_response({"removed": bed_id})


def filter_zone(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    zone, present = server.runtime.bank.bed_zone(bed_id)
    return json_response({"id": bed_id, "zone": zone, "ok": present})


def backwash_start(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.backwash.start(bed_id)
    return json_response({"started": bed_id})


def backwash_order(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    order = runtime.backwash.order_rotation()
    return json_response({"order": order, "on_duty": runtime.bank.on_duty() or ""})


def backwash_select(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    bed_id = server.runtime.backwash.select(zone)
    return json_response({"bed": bed_id, "zone": zone})


def backwash_enqueue(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.backwash.enqueue(bed_id)
    return json_response({"enqueued": bed_id})


def backwash_recover(server: "Server", request: Request) -> Response:
    server.runtime.backwash.recover()
    return json_response({"recovered": True})


def backwash_replay(server: "Server", request: Request) -> Response:
    return json_response({"replayed": server.runtime.backwash.replay()})


def quota_add(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_amount(amount)
    return json_response({"value": server.runtime.accumulator.add(amount)})


def quota_get(server: "Server", request: Request) -> Response:
    return json_response({"value": server.runtime.accumulator.value()})


def quota_check(server: "Server", request: Request) -> Response:
    quota = Quota(chemical=request.str_field("chemical"), limit=request.float_field("limit"))
    remaining, ok = check_quota(quota.chemical, request.float_field("used"), quota.limit)
    return json_response({"remaining": remaining, "ok": ok})


def audit_list(server: "Server", request: Request) -> Response:
    return json_response({"entries": [entry.as_dict() for entry in server.runtime.auditor.entries()]})


def audit_summary(server: "Server", request: Request) -> Response:
    auditor = server.runtime.auditor
    return json_response({"count": auditor.count(), "by_kind": auditor.count_by_kind()})


def audit_filter(server: "Server", request: Request) -> Response:
    entries = server.runtime.auditor.filter(request.str_field("kind"))
    return json_response({"entries": [entry.as_dict() for entry in entries]})


def cycle(server: "Server", request: Request) -> Response:
    return run_cycle(server.runtime, request)


def simulate(server: "Server", request: Request) -> Response:
    return simulate_module.run_simulation(server.runtime, request)


def ph_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.stabilizer.state().as_dict())


def ph_read(server: "Server", request: Request) -> Response:
    value = request.float_field("value")
    validate_ph(value)
    return json_response(server.runtime.stabilizer.read(value).as_dict())


def ph_verdict(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.stabilizer.stabilize().as_dict())


def schedule_state(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    return json_response(runtime.scheduler.state(runtime.bank).as_dict())


def schedule_threshold(server: "Server", request: Request) -> Response:
    value = request.float_field("threshold")
    validate_threshold(value)
    return json_response({"threshold": server.runtime.scheduler.set_threshold(value)})


def schedule_plan(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    entries = runtime.scheduler.plan(runtime.bank)
    return json_response(
        {"entries": [entry.as_dict() for entry in entries], "due": runtime.scheduler.due(runtime.bank)}
    )


def intake_trend(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.trend.stats().as_dict())


def intake_trend_window(server: "Server", request: Request) -> Response:
    value = request.int_field("window", DEFAULT_WINDOW)
    server.runtime.trend.set_window(value)
    return json_response({"window": server.runtime.trend.window()})


def intake_trend_reset(server: "Server", request: Request) -> Response:
    server.runtime.trend.reset()
    return json_response({"reset": True})


def audit_totals(server: "Server", request: Request) -> Response:
    return json_response({"totals": server.runtime.auditor.totals()})


def inventory_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.inventory.state().as_dict())


def inventory_receive(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_quantity(amount)
    lot = server.runtime.inventory.receive(
        request.str_field("chemical"), request.str_field("lot_id"), amount
    )
    return json_response({"lot": lot.as_dict()})


def inventory_consume(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_quantity(amount)
    chemical = request.str_field("chemical")
    consumed = server.runtime.inventory.consume(chemical, amount)
    return json_response(
        {"consumed": consumed, "balance": server.runtime.inventory.balance(chemical)}
    )


def inventory_reorder_level(server: "Server", request: Request) -> Response:
    level = request.float_field("level")
    validate_quantity(level, "level")
    return json_response({"reorder_level": server.runtime.inventory.set_reorder_level(level)})


def metrics(server: "Server", request: Request) -> Response:
    return json_response(server.metrics.snapshot().as_dict())


def metrics_reset(server: "Server", request: Request) -> Response:
    server.metrics.reset()
    return json_response({"reset": True})


def export_audit(server: "Server", request: Request) -> Response:
    columns, rows = audit_export(server.runtime.auditor)
    return csv_response(to_csv(columns, rows))


def export_calibration_audit(server: "Server", request: Request) -> Response:
    columns, rows = calibration_audit_export(server.runtime.auditor)
    return csv_response(to_csv(columns, rows))


def export_telemetry(server: "Server", request: Request) -> Response:
    columns, rows = telemetry_export(telemetry_module.collect(server.runtime))
    return csv_response(to_csv(columns, rows))
