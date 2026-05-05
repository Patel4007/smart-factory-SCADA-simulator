from __future__ import annotations

import random
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .real_data import (
    DATASET_DETAIL,
    DATASET_NAME,
    DEFAULT_SAMPLE_RATE_HZ,
    RealTrace,
    TraceWindow,
    load_curated_bosch_sample,
    load_trace_file,
)


def utc_timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class MachineSpec:
    machine_id: str
    name: str
    kind: str
    base_rate_uph: float
    buffer_capacity: float
    temperature_baseline: float
    vibration_baseline: float
    power_baseline: float


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    scenario_id: str
    name: str
    description: str
    impact: str


@dataclass(frozen=True, slots=True)
class TraceAssignment:
    normal_trace_id: str
    offset: int
    load_scale: float
    fault_trace_id: str


@dataclass(frozen=True, slots=True)
class UploadedTraceBinding:
    machine_id: str
    machine_name: str
    role: str
    trace_id: str
    file_name: str
    source_format: str
    quality_label: str
    sample_rate_hz: int
    sample_count: int
    window_count: int


@dataclass(slots=True)
class MachineRuntime:
    spec: MachineSpec
    speed_setpoint: float = 1.0
    state: str = "idle"
    temperature_c: float = 0.0
    vibration_mm_s: float = 0.0
    power_kw: float = 0.0
    throughput_uph: float = 0.0
    total_units: float = 0.0
    good_units: float = 0.0
    scrap_units: float = 0.0
    downtime_seconds: float = 0.0
    health_score: float = 100.0
    buffer_fill_pct: float = 0.0
    signal_rms: float = 0.0
    signal_peak: float = 0.0
    signal_anomaly: float = 1.0
    trace_id: str = ""
    trace_quality: str = "good"
    trace_cursor: int = 0
    alarm_code: str | None = None
    detail: str = ""
    fault_until: datetime | None = None
    fault_code: str | None = None
    maintenance_override: bool = False
    maintenance_until: datetime | None = None


class SmartFactoryEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.random = random.Random(settings.random_seed)
        self.base_data_source = settings.data_source_name or DATASET_NAME
        self.base_data_source_detail = DATASET_DETAIL
        self.data_source = self.base_data_source
        self.data_source_detail = self.base_data_source_detail
        self.real_traces = load_curated_bosch_sample(
            settings.real_data_dir,
            window_samples=settings.real_data_window_samples,
        )
        self.uploaded_trace_bindings: dict[tuple[str, str], UploadedTraceBinding] = {}
        self.machine_specs = [
            MachineSpec("FEED-01", "Raw Coil Feeder", "feeder", 240.0, 80.0, 38.0, 1.2, 12.0),
            MachineSpec("CUT-02", "Laser Cutter", "cutter", 228.0, 62.0, 44.0, 1.9, 18.0),
            MachineSpec("ASM-03", "Robot Assembly Cell", "robot", 220.0, 54.0, 46.0, 2.1, 22.0),
            MachineSpec("QCI-04", "Vision Quality Gate", "vision-qc", 215.0, 44.0, 40.0, 1.4, 14.0),
            MachineSpec("PAC-05", "Packaging Conveyor", "packaging", 210.0, 0.0, 37.0, 1.1, 10.0),
        ]
        self.trace_assignments = {
            "FEED-01": TraceAssignment("m01_op01_good", offset=0, load_scale=1.02, fault_trace_id="m01_op01_bad"),
            "CUT-02": TraceAssignment("m02_op03_good", offset=5, load_scale=0.98, fault_trace_id="m01_op01_bad"),
            "ASM-03": TraceAssignment("m03_op05_good", offset=9, load_scale=1.00, fault_trace_id="m01_op01_bad"),
            "QCI-04": TraceAssignment("m01_op01_good", offset=14, load_scale=0.88, fault_trace_id="m01_op01_bad"),
            "PAC-05": TraceAssignment("m02_op03_good", offset=21, load_scale=0.82, fault_trace_id="m01_op01_bad"),
        }
        self.scenario_specs = {
            "conveyor-jam": ScenarioSpec(
                "conveyor-jam",
                "Conveyor Jam",
                "Jams the assembly conveyor and collapses downstream throughput.",
                "Robot cell faults, active alarms spike, and downstream stations starve.",
            ),
            "power-sag": ScenarioSpec(
                "power-sag",
                "Utility Power Sag",
                "Introduces a short voltage dip across the entire line.",
                "Cycle times slow, power stability degrades, and temperatures climb.",
            ),
            "quality-drift": ScenarioSpec(
                "quality-drift",
                "Quality Drift",
                "Switches the quality station onto a real anomalous Bosch trace and elevates scrap.",
                "Vision scrap rises and the quality gate becomes degraded instead of fully stopped.",
            ),
            "maintenance-window": ScenarioSpec(
                "maintenance-window",
                "Maintenance Window",
                "Forces a controlled maintenance intervention on packaging.",
                "Packaging stops cleanly while the rest of the line drains through its buffers.",
            ),
        }
        self.reset()

    def reset(self) -> None:
        self.running = self.settings.initial_line_running
        self.mode = "AUTO"
        self.elapsed_seconds = 0.0
        self.operating_seconds = 0.0
        self.good_units_total = 0.0
        self.scrap_units_total = 0.0
        self.buffers = [18.0, 12.0, 9.0, 6.0]
        self.power_sag_until: datetime | None = None
        self.quality_drift_until: datetime | None = None
        self.runtimes = [MachineRuntime(spec=spec) for spec in self.machine_specs]
        for runtime in self.runtimes:
            assignment = self.trace_assignments[runtime.spec.machine_id]
            runtime.trace_cursor = assignment.offset
            self._seed_runtime_from_trace(runtime)
        self._refresh_state_descriptions()
        self._refresh_data_source_metadata()

    def scenario_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "scenario_id": scenario.scenario_id,
                "name": scenario.name,
                "description": scenario.description,
                "impact": scenario.impact,
            }
            for scenario in self.scenario_specs.values()
        ]

    def snapshot_events(self) -> list[tuple[str, dict[str, Any]]]:
        now = datetime.now(timezone.utc)
        timestamp = utc_timestamp(now)
        events = [(self.settings.kafka_machine_topic, self._machine_payload(runtime, timestamp)) for runtime in self.runtimes]
        events.append((self.settings.kafka_kpi_topic, self._line_metrics_payload(timestamp)))
        return events

    def uploaded_trace_catalog(self) -> list[dict[str, Any]]:
        machine_positions = {spec.machine_id: index for index, spec in enumerate(self.machine_specs)}
        ordered = sorted(
            self.uploaded_trace_bindings.values(),
            key=lambda binding: (machine_positions.get(binding.machine_id, 999), 0 if binding.role == "normal" else 1),
        )
        return [
            {
                "machine_id": binding.machine_id,
                "machine_name": binding.machine_name,
                "role": binding.role,
                "trace_id": binding.trace_id,
                "file_name": binding.file_name,
                "source_format": binding.source_format,
                "quality_label": binding.quality_label,
                "sample_rate_hz": binding.sample_rate_hz,
                "sample_count": binding.sample_count,
                "window_count": binding.window_count,
            }
            for binding in ordered
        ]

    def uploaded_trace_catalog_item(self, machine_id: str, role: str) -> dict[str, Any]:
        binding = self.uploaded_trace_bindings[(machine_id, role)]
        return {
            "machine_id": binding.machine_id,
            "machine_name": binding.machine_name,
            "role": binding.role,
            "trace_id": binding.trace_id,
            "file_name": binding.file_name,
            "source_format": binding.source_format,
            "quality_label": binding.quality_label,
            "sample_rate_hz": binding.sample_rate_hz,
            "sample_count": binding.sample_count,
            "window_count": binding.window_count,
        }

    def apply_line_action(self, action: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        timestamp = utc_timestamp(now)
        if action == "start":
            self.running = True
            detail = "Operator issued a controlled line start against the real-trace replay."
            title = "Line started"
        elif action == "stop":
            self.running = False
            detail = "Operator issued a controlled line stop. Real trace windows remain visible for review."
            title = "Line stopped"
        elif action == "reset":
            self.reset()
            detail = "Controller state, buffers, alarms, counters, and real-trace cursors were reset."
            title = "Simulation reset"
        else:
            raise ValueError(f"Unsupported line action: {action}")
        self._refresh_state_descriptions()
        return {
            "timestamp": timestamp,
            "severity": "info",
            "event_type": "reset" if action == "reset" else "operator_action",
            "title": title,
            "detail": detail,
            "source": "line-controller",
            "code": action.upper(),
        }

    def apply_machine_control(
        self,
        machine_id: str,
        *,
        action: str,
        speed: float | None = None,
        enabled: bool | None = None,
        duration_seconds: int = 20,
        code: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        runtime = self._runtime(machine_id)
        now = datetime.now(timezone.utc)
        timestamp = utc_timestamp(now)
        if action == "set_speed":
            if speed is None:
                raise ValueError("Speed value is required for set_speed.")
            runtime.speed_setpoint = max(0.5, min(1.3, speed))
            title = f"{runtime.spec.name} speed updated"
            message = f"Speed setpoint moved to {runtime.speed_setpoint:.2f}x nominal on the real-data replay."
            severity = "info"
            event_type = "operator_action"
        elif action == "maintenance":
            runtime.maintenance_override = bool(enabled)
            title = f"{runtime.spec.name} maintenance {'enabled' if runtime.maintenance_override else 'cleared'}"
            message = "Manual maintenance hold is active." if runtime.maintenance_override else "Machine returned to automatic service."
            severity = "warning" if runtime.maintenance_override else "info"
            event_type = "operator_action"
        elif action == "clear_fault":
            cleared_code = runtime.alarm_code or runtime.fault_code
            runtime.fault_until = None
            runtime.fault_code = None
            runtime.alarm_code = None
            title = f"{runtime.spec.name} fault cleared"
            message = "Operator acknowledged the machine fault and returned it to standby."
            severity = "info"
            event_type = "clear_fault"
        elif action == "fault":
            cleared_code = None
            runtime.fault_until = now + timedelta(seconds=duration_seconds)
            runtime.fault_code = code or "MANUAL_FAULT"
            title = f"{runtime.spec.name} fault injected"
            message = detail or "A manual SCADA fault was injected, switching the machine onto an anomalous trace replay."
            severity = "critical"
            event_type = "alarm"
        else:
            raise ValueError(f"Unsupported machine action: {action}")
        self._refresh_state_descriptions()
        return {
            "timestamp": timestamp,
            "severity": severity,
            "event_type": event_type,
            "title": title,
            "detail": message,
            "source": runtime.spec.machine_id,
            "code": runtime.fault_code if action == "fault" else cleared_code if action == "clear_fault" else action.upper(),
        }

    def inject_scenario(self, scenario_id: str, duration_seconds: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        until = now + timedelta(seconds=duration_seconds)
        if scenario_id == "conveyor-jam":
            runtime = self._runtime("ASM-03")
            runtime.fault_until = until
            runtime.fault_code = "JAM"
        elif scenario_id == "power-sag":
            self.power_sag_until = until
        elif scenario_id == "quality-drift":
            self.quality_drift_until = until
        elif scenario_id == "maintenance-window":
            runtime = self._runtime("PAC-05")
            runtime.maintenance_until = until
        else:
            raise ValueError(f"Unsupported scenario: {scenario_id}")
        self._refresh_state_descriptions()
        scenario = self.scenario_specs[scenario_id]
        return {
            "timestamp": utc_timestamp(now),
            "severity": "warning" if scenario_id != "conveyor-jam" else "critical",
            "event_type": "scenario",
            "title": scenario.name,
            "detail": f"{scenario.description} Duration: {duration_seconds} seconds.",
            "source": scenario_id,
            "code": scenario_id.upper().replace("-", "_"),
        }

    def upload_trace(
        self,
        machine_id: str,
        role: str,
        *,
        file_name: str,
        payload: bytes,
        sample_rate_hz: int | None = None,
    ) -> dict[str, Any]:
        if role not in {"normal", "fault"}:
            raise ValueError(f"Unsupported upload role: {role}")
        if not payload:
            raise ValueError("Uploaded file is empty.")
        if len(payload) > self.settings.upload_max_bytes:
            limit_mb = self.settings.upload_max_bytes / (1024 * 1024)
            raise ValueError(f"Uploaded file exceeds the {limit_mb:.0f} MB limit.")
        if sample_rate_hz is not None and sample_rate_hz <= 0:
            raise ValueError("Sample rate must be greater than zero.")

        runtime = self._runtime(machine_id)
        assignment = self.trace_assignments[machine_id]
        safe_name = self._sanitize_upload_name(file_name)
        upload_dir = Path(self.settings.uploaded_data_dir) / machine_id / role
        upload_dir.mkdir(parents=True, exist_ok=True)
        timestamp_token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stored_name = f"{timestamp_token}_{safe_name}"
        target_path = upload_dir / stored_name
        target_path.write_bytes(payload)

        trace_id = f"upload_{machine_id.lower().replace('-', '_')}_{role}_{timestamp_token.lower()}"
        quality_label = "bad" if role == "fault" else "good"
        trace = load_trace_file(
            trace_id,
            target_path,
            window_samples=self.settings.real_data_window_samples,
            sample_rate_hz=sample_rate_hz or DEFAULT_SAMPLE_RATE_HZ,
            machine_code=machine_id,
            operation_code=runtime.spec.kind.upper(),
            quality_label=quality_label,
        )
        self.real_traces[trace_id] = trace

        if role == "normal":
            self.trace_assignments[machine_id] = replace(assignment, normal_trace_id=trace_id, offset=0)
        else:
            self.trace_assignments[machine_id] = replace(assignment, fault_trace_id=trace_id)

        self.uploaded_trace_bindings[(machine_id, role)] = UploadedTraceBinding(
            machine_id=machine_id,
            machine_name=runtime.spec.name,
            role=role,
            trace_id=trace.trace_id,
            file_name=trace.file_name,
            source_format=trace.source_format,
            quality_label=trace.quality_label,
            sample_rate_hz=trace.sample_rate_hz,
            sample_count=trace.sample_count,
            window_count=len(trace.windows),
        )
        runtime.trace_cursor = 0
        self._refresh_runtime_trace_preview(runtime)
        self._refresh_state_descriptions()
        self._refresh_data_source_metadata()
        return {
            "detail": (
                f"Loaded {trace.file_name} as the {role} replay for {runtime.spec.name}. "
                f"{len(trace.windows)} windows are ready for live playback."
            ),
            "binding": self.uploaded_trace_catalog_item(machine_id, role),
        }

    def step(self) -> list[tuple[str, dict[str, Any]]]:
        now = datetime.now(timezone.utc)
        timestamp = utc_timestamp(now)
        dt = self.settings.simulation_tick_seconds
        self.elapsed_seconds += dt
        if self.running:
            self.operating_seconds += dt

        control_events: list[tuple[str, dict[str, Any]]] = []
        power_sag_active = self.power_sag_until is not None and self.power_sag_until > now
        quality_drift_active = self.quality_drift_until is not None and self.quality_drift_until > now

        for index, runtime in enumerate(self.runtimes):
            spec = runtime.spec
            assignment = self.trace_assignments[spec.machine_id]
            fault_active = runtime.fault_until is not None and runtime.fault_until > now
            maintenance_active = runtime.maintenance_override or (
                runtime.maintenance_until is not None and runtime.maintenance_until > now
            )
            trace = self._active_trace(runtime, fault_active=fault_active, quality_drift_active=quality_drift_active)
            window = self._next_trace_window(runtime, trace, advance=self.running or fault_active or maintenance_active)

            signal_ratio = self._signal_ratio(window, trace, assignment)
            anomaly_ratio = self._signal_anomaly(window, trace, assignment)
            if quality_drift_active and spec.kind == "vision-qc":
                anomaly_ratio = max(anomaly_ratio, 1.24)

            desired_units = spec.base_rate_uph / 3600.0 * dt * runtime.speed_setpoint
            desired_units *= 0.84 + 0.24 * signal_ratio
            desired_units *= 1.0 - 0.16 * max(anomaly_ratio - 1.0, 0.0)
            desired_units *= 0.76 if power_sag_active else 1.0

            if fault_active:
                state = "fault"
                processed_units = 0.0
            elif maintenance_active:
                state = "maintenance"
                processed_units = 0.0
            elif not self.running:
                state = "idle"
                processed_units = 0.0
            else:
                input_available = float("inf") if index == 0 else self.buffers[index - 1]
                output_capacity = float("inf") if index == len(self.runtimes) - 1 else spec.buffer_capacity - self.buffers[index]
                processed_units = max(0.0, min(desired_units, input_available, output_capacity))
                if index > 0 and input_available < desired_units * 0.72:
                    state = "starved"
                    runtime.downtime_seconds += dt * 0.45
                elif index < len(self.runtimes) - 1 and output_capacity < desired_units * 0.72:
                    state = "blocked"
                    runtime.downtime_seconds += dt * 0.45
                else:
                    state = "running"

            if state in {"fault", "maintenance"}:
                runtime.downtime_seconds += dt

            active_processing = state in {"running", "starved", "blocked"} and processed_units > 0.0
            if active_processing:
                if index > 0:
                    self.buffers[index - 1] = max(0.0, self.buffers[index - 1] - processed_units)

                effective_units = processed_units
                if spec.kind == "vision-qc":
                    scrap_factor = 0.015 + 0.045 * max(anomaly_ratio - 1.0, 0.0) + (0.085 if quality_drift_active else 0.0)
                    rejected_units = processed_units * min(scrap_factor, 0.20)
                    effective_units = max(0.0, processed_units - rejected_units)
                    runtime.scrap_units += rejected_units
                    self.scrap_units_total += rejected_units
                elif spec.kind == "packaging":
                    rejected_units = processed_units * min(0.004 + 0.012 * max(anomaly_ratio - 1.0, 0.0), 0.03)
                    effective_units = max(0.0, processed_units - rejected_units)
                    runtime.scrap_units += rejected_units
                    self.scrap_units_total += rejected_units

                if index < len(self.runtimes) - 1:
                    self.buffers[index] = min(spec.buffer_capacity, self.buffers[index] + effective_units)
                else:
                    runtime.good_units += effective_units
                    self.good_units_total += effective_units

                runtime.total_units += processed_units
                runtime.throughput_uph = effective_units * 3600.0 / dt
            else:
                runtime.throughput_uph = 0.0

            load_ratio = processed_units / max(spec.base_rate_uph / 3600.0 * dt, 0.001) if active_processing else 0.0
            runtime.temperature_c = self._temperature(spec, signal_ratio, anomaly_ratio, power_sag_active, state)
            runtime.vibration_mm_s = self._vibration(spec, signal_ratio, anomaly_ratio, state)
            runtime.power_kw = self._power_draw(spec, signal_ratio, anomaly_ratio, power_sag_active, state)
            runtime.health_score = self._health(spec, runtime, state, anomaly_ratio)
            runtime.signal_rms = round(window.rms_magnitude, 1)
            runtime.signal_peak = round(window.peak_magnitude, 1)
            runtime.signal_anomaly = anomaly_ratio
            runtime.trace_id = trace.trace_id
            runtime.trace_quality = trace.quality_label
            runtime.state = state
            runtime.buffer_fill_pct = self._buffer_fill(index)
            runtime.detail = self._detail_for_state(runtime, power_sag_active, quality_drift_active)

            new_alarm_code = self._alarm_code(spec, runtime, quality_drift_active)
            if new_alarm_code != runtime.alarm_code:
                if runtime.alarm_code:
                    control_events.append(
                        (
                            self.settings.kafka_control_topic,
                            {
                                "timestamp": timestamp,
                                "severity": "info",
                                "event_type": "recovery",
                                "title": f"{spec.name} recovered",
                                "detail": f"{spec.name} cleared alarm {runtime.alarm_code} and returned toward stable operation.",
                                "source": spec.machine_id,
                                "code": runtime.alarm_code,
                            },
                        )
                    )
                if new_alarm_code:
                    control_events.append(
                        (
                            self.settings.kafka_control_topic,
                            {
                                "timestamp": timestamp,
                                "severity": self._alarm_severity(runtime, new_alarm_code),
                                "event_type": "alarm",
                                "title": f"{spec.name} alarm",
                                "detail": runtime.detail,
                                "source": spec.machine_id,
                                "code": new_alarm_code,
                            },
                        )
                    )
            runtime.alarm_code = new_alarm_code

        machine_events = [(self.settings.kafka_machine_topic, self._machine_payload(runtime, timestamp)) for runtime in self.runtimes]
        kpi_event = (self.settings.kafka_kpi_topic, self._line_metrics_payload(timestamp))
        return machine_events + [kpi_event] + control_events

    def machine_snapshots(self) -> list[dict[str, Any]]:
        timestamp = utc_timestamp()
        return [self._machine_payload(runtime, timestamp) for runtime in self.runtimes]

    def line_metrics_snapshot(self) -> dict[str, Any]:
        return self._line_metrics_payload(utc_timestamp())

    def _runtime(self, machine_id: str) -> MachineRuntime:
        for runtime in self.runtimes:
            if runtime.spec.machine_id == machine_id:
                return runtime
        raise ValueError(f"Unknown machine: {machine_id}")

    def _seed_runtime_from_trace(self, runtime: MachineRuntime) -> None:
        self._refresh_runtime_trace_preview(runtime)

    def _refresh_runtime_trace_preview(self, runtime: MachineRuntime) -> None:
        now = datetime.now(timezone.utc)
        assignment = self.trace_assignments[runtime.spec.machine_id]
        fault_active = runtime.fault_until is not None and runtime.fault_until > now
        quality_drift_active = self.quality_drift_until is not None and self.quality_drift_until > now
        power_sag_active = self.power_sag_until is not None and self.power_sag_until > now
        trace = self._active_trace(runtime, fault_active=fault_active, quality_drift_active=quality_drift_active)
        window = trace.window_at(runtime.trace_cursor)
        signal_ratio = self._signal_ratio(window, trace, assignment)
        anomaly_ratio = self._signal_anomaly(window, trace, assignment)
        if runtime.maintenance_override or (runtime.maintenance_until is not None and runtime.maintenance_until > now):
            runtime.state = "maintenance"
        elif fault_active:
            runtime.state = "fault"
        elif not self.running:
            runtime.state = "idle"
        else:
            runtime.state = "running"
        runtime.signal_rms = round(window.rms_magnitude, 1)
        runtime.signal_peak = round(window.peak_magnitude, 1)
        runtime.signal_anomaly = anomaly_ratio
        runtime.trace_id = trace.trace_id
        runtime.trace_quality = trace.quality_label
        runtime.temperature_c = self._temperature(runtime.spec, signal_ratio, anomaly_ratio, power_sag_active, runtime.state)
        runtime.vibration_mm_s = self._vibration(runtime.spec, signal_ratio, anomaly_ratio, runtime.state)
        runtime.power_kw = self._power_draw(runtime.spec, signal_ratio, anomaly_ratio, power_sag_active, runtime.state)
        runtime.health_score = self._health(runtime.spec, runtime, runtime.state, anomaly_ratio)
        runtime.detail = self._detail_for_state(runtime, power_sag_active, quality_drift_active)

    def _refresh_state_descriptions(self) -> None:
        for runtime in self.runtimes:
            if runtime.maintenance_override:
                runtime.state = "maintenance"
                runtime.detail = "Manual maintenance lockout is active on the real-trace replay."
            elif runtime.fault_until is not None and runtime.fault_until > datetime.now(timezone.utc):
                runtime.state = "fault"
                runtime.detail = "An anomalous trace replay is active because the machine is faulted."
            elif not self.running:
                runtime.state = "idle"
                runtime.detail = "Line is stopped by operator command; last real trace window is held."
            else:
                runtime.state = "running"
                runtime.detail = f"Replaying trace {runtime.trace_id or 'sample'} in automatic mode."

    def _active_trace(self, runtime: MachineRuntime, *, fault_active: bool, quality_drift_active: bool) -> RealTrace:
        assignment = self.trace_assignments[runtime.spec.machine_id]
        use_fault_trace = fault_active or (quality_drift_active and runtime.spec.kind == "vision-qc")
        trace_id = assignment.fault_trace_id if use_fault_trace else assignment.normal_trace_id
        return self.real_traces[trace_id]

    def _next_trace_window(self, runtime: MachineRuntime, trace: RealTrace, *, advance: bool) -> TraceWindow:
        window = trace.window_at(runtime.trace_cursor)
        if advance:
            runtime.trace_cursor += 1
        return window

    def _signal_ratio(self, window: TraceWindow, trace: RealTrace, assignment: TraceAssignment) -> float:
        ratio = 0.93 + (window.intensity_ratio - 1.0) * 0.40
        ratio *= assignment.load_scale
        if trace.quality_label == "bad":
            ratio *= 0.82
        return max(0.68, min(ratio, 1.18))

    def _signal_anomaly(self, window: TraceWindow, trace: RealTrace, assignment: TraceAssignment) -> float:
        baseline_rms = self.real_traces[assignment.normal_trace_id].mean_rms
        anomaly = 1.0 + max(0.0, window.rms_magnitude / max(baseline_rms, 1e-9) - 1.0) * 0.55
        if trace.quality_label == "bad":
            anomaly += 0.18
        if window.crest_factor > 4.5:
            anomaly += 0.03
        return max(0.82, min(anomaly, 1.65))

    def _machine_payload(self, runtime: MachineRuntime, timestamp: str) -> dict[str, Any]:
        return {
            "timestamp": timestamp,
            "machine_id": runtime.spec.machine_id,
            "name": runtime.spec.name,
            "kind": runtime.spec.kind,
            "state": runtime.state,
            "speed_setpoint": round(runtime.speed_setpoint, 2),
            "temperature_c": round(runtime.temperature_c, 1),
            "vibration_mm_s": round(runtime.vibration_mm_s, 2),
            "power_kw": round(runtime.power_kw, 1),
            "throughput_uph": round(runtime.throughput_uph, 1),
            "total_units": round(runtime.total_units, 1),
            "good_units": round(runtime.good_units, 1),
            "scrap_units": round(runtime.scrap_units, 1),
            "downtime_seconds": round(runtime.downtime_seconds, 1),
            "health_score": round(runtime.health_score, 1),
            "buffer_fill_pct": round(runtime.buffer_fill_pct, 1),
            "signal_rms": round(runtime.signal_rms, 1),
            "signal_peak": round(runtime.signal_peak, 1),
            "trace_id": runtime.trace_id,
            "trace_quality": runtime.trace_quality,
            "alarm_code": runtime.alarm_code,
            "detail": runtime.detail,
        }

    def _line_metrics_payload(self, timestamp: str) -> dict[str, Any]:
        active_alarm_count = sum(1 for runtime in self.runtimes if runtime.alarm_code)
        energy_kw = sum(runtime.power_kw for runtime in self.runtimes)
        overall_health = sum(runtime.health_score for runtime in self.runtimes) / len(self.runtimes)
        downtime_pct = (
            sum(runtime.downtime_seconds for runtime in self.runtimes)
            / max(self.operating_seconds * len(self.runtimes), 1.0)
            * 100.0
        )
        target_throughput = self.machine_specs[-1].base_rate_uph * (
            0.82 if self.power_sag_until and self.power_sag_until > datetime.now(timezone.utc) else 1.0
        )
        throughput_uph = self.runtimes[-1].throughput_uph
        quality_pct = self.good_units_total / max(self.good_units_total + self.scrap_units_total, 0.001) * 100.0
        availability_pct = max(0.0, 100.0 - downtime_pct)
        performance_pct = (throughput_uph / max(target_throughput, 1.0)) * 100.0 if self.running else 0.0
        performance_pct = max(0.0, min(performance_pct, 115.0))
        oee = availability_pct * quality_pct * performance_pct / 10000.0
        buffer_utilization = sum(self._buffer_fill(index) for index in range(len(self.runtimes))) / len(self.runtimes)
        if not self.running:
            line_state = "STOPPED"
        elif any(runtime.state == "fault" for runtime in self.runtimes):
            line_state = "ALARM"
        elif active_alarm_count:
            line_state = "DEGRADED"
        else:
            line_state = "RUNNING"
        return {
            "timestamp": timestamp,
            "line_state": line_state,
            "mode": self.mode,
            "throughput_uph": round(throughput_uph, 1),
            "target_throughput_uph": round(target_throughput, 1),
            "downtime_pct": round(min(downtime_pct, 100.0), 1),
            "overall_health": round(overall_health, 1),
            "oee": round(min(max(oee, 0.0), 100.0), 1),
            "energy_kw": round(energy_kw, 1),
            "good_units": round(self.good_units_total, 1),
            "scrap_units": round(self.scrap_units_total, 1),
            "active_alarm_count": active_alarm_count,
            "availability_pct": round(min(max(availability_pct, 0.0), 100.0), 1),
            "quality_pct": round(min(max(quality_pct, 0.0), 100.0), 1),
            "performance_pct": round(performance_pct, 1),
            "buffer_utilization_pct": round(buffer_utilization, 1),
        }

    def _refresh_data_source_metadata(self) -> None:
        if not self.uploaded_trace_bindings:
            self.data_source = self.base_data_source
            self.data_source_detail = self.base_data_source_detail
            return
        self.data_source = f"{self.base_data_source} + Operator Uploads"
        assignment_summary = ", ".join(
            f"{binding.machine_id} {binding.role}: {binding.file_name}" for binding in self.uploaded_trace_bindings.values()
        )
        self.data_source_detail = f"{self.base_data_source_detail} Active uploads: {assignment_summary}."

    @staticmethod
    def _sanitize_upload_name(file_name: str) -> str:
        sanitized = Path(file_name or "").name.strip()
        if not sanitized:
            raise ValueError("Upload is missing a file name.")
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", sanitized)
        if "." not in sanitized:
            raise ValueError("Upload must keep a supported file extension.")
        return sanitized

    def _buffer_fill(self, index: int) -> float:
        if index < len(self.buffers):
            capacity = self.machine_specs[index].buffer_capacity
            return self.buffers[index] / max(capacity, 1.0) * 100.0
        capacity = self.machine_specs[-2].buffer_capacity
        return self.buffers[-1] / max(capacity, 1.0) * 100.0

    def _temperature(
        self,
        spec: MachineSpec,
        signal_ratio: float,
        anomaly_ratio: float,
        power_sag_active: bool,
        state: str,
    ) -> float:
        if state == "idle":
            base = spec.temperature_baseline - 4.0
        elif state == "maintenance":
            base = spec.temperature_baseline - 1.8
        else:
            base = spec.temperature_baseline + 4.2 * signal_ratio + 6.0 * max(anomaly_ratio - 1.0, 0.0)
        base += 3.0 if power_sag_active else 0.0
        base += 11.0 if state == "fault" else 0.0
        return base + self.random.uniform(-0.6, 0.8)

    def _vibration(
        self,
        spec: MachineSpec,
        signal_ratio: float,
        anomaly_ratio: float,
        state: str,
    ) -> float:
        if state == "idle":
            base = spec.vibration_baseline * 0.72
        elif state == "maintenance":
            base = spec.vibration_baseline * 0.58
        else:
            base = spec.vibration_baseline * (0.82 + 0.34 * signal_ratio + 0.85 * max(anomaly_ratio - 1.0, 0.0))
        if state == "fault":
            base += 0.8
        return base + self.random.uniform(-0.08, 0.12)

    def _power_draw(
        self,
        spec: MachineSpec,
        signal_ratio: float,
        anomaly_ratio: float,
        power_sag_active: bool,
        state: str,
    ) -> float:
        if state == "idle":
            multiplier = 0.20
        elif state == "maintenance":
            multiplier = 0.14
        elif state == "fault":
            multiplier = 0.36 + 0.08 * anomaly_ratio
        else:
            multiplier = 0.72 + 0.22 * signal_ratio + 0.18 * max(anomaly_ratio - 1.0, 0.0)
        multiplier *= 0.94 if power_sag_active else 1.0
        return spec.power_baseline * multiplier

    def _health(self, spec: MachineSpec, runtime: MachineRuntime, state: str, anomaly_ratio: float) -> float:
        signal_penalty = max(0.0, anomaly_ratio - 1.0) * 58.0
        temp_penalty = max(0.0, runtime.temperature_c - (spec.temperature_baseline + 8.0)) * 2.8
        vibration_penalty = max(0.0, runtime.vibration_mm_s - (spec.vibration_baseline + 1.1)) * 14.0
        state_penalty = {"fault": 34.0, "maintenance": 12.0, "blocked": 10.0, "starved": 7.0}.get(state, 0.0)
        return max(8.0, min(100.0, 100.0 - signal_penalty - temp_penalty - vibration_penalty - state_penalty))

    def _detail_for_state(self, runtime: MachineRuntime, power_sag_active: bool, quality_drift_active: bool) -> str:
        state = runtime.state
        if state == "fault":
            return (
                f"Real anomaly trace {runtime.trace_id} is active and {runtime.spec.name} is faulted "
                f"with code {runtime.fault_code or 'FAULT'}."
            )
        if state == "maintenance":
            return f"{runtime.spec.name} is in a scheduled or manual maintenance window. Real trace replay is paused."
        if state == "blocked":
            return f"{runtime.spec.name} is blocked by downstream buffer saturation while replaying {runtime.trace_id}."
        if state == "starved":
            return f"{runtime.spec.name} is starved by upstream material constraints while replaying {runtime.trace_id}."
        if not self.running:
            return f"{runtime.spec.name} is waiting on an operator restart. Last real trace window: {runtime.trace_id}."
        if quality_drift_active and runtime.spec.kind == "vision-qc":
            return f"Real anomalous trace {runtime.trace_id} is driving elevated inspection scrap."
        if power_sag_active:
            return f"{runtime.spec.name} continues replaying trace {runtime.trace_id} under a plant-wide power sag."
        return f"Replaying trace {runtime.trace_id} ({runtime.trace_quality}) within the expected vibration band."

    def _alarm_code(self, spec: MachineSpec, runtime: MachineRuntime, quality_drift_active: bool) -> str | None:
        if runtime.state == "fault":
            return runtime.fault_code or "FAULT"
        if quality_drift_active and spec.kind == "vision-qc":
            return "QC_DRIFT"
        if runtime.signal_anomaly >= 1.22:
            return "HIGH_VIBRATION"
        if runtime.temperature_c > spec.temperature_baseline + 10.5:
            return "HIGH_TEMP"
        if runtime.state == "blocked":
            return "BUFFER_BLOCKED"
        if runtime.state == "starved":
            return "MATERIAL_STARVED"
        return None

    @staticmethod
    def _alarm_severity(runtime: MachineRuntime, alarm_code: str) -> str:
        if runtime.state == "fault" or alarm_code in {"HIGH_TEMP", "HIGH_VIBRATION", "JAM"}:
            return "critical"
        if alarm_code in {"BUFFER_BLOCKED", "MATERIAL_STARVED", "QC_DRIFT"}:
            return "warning"
        return "info"
