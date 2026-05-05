from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MachineSnapshot(BaseModel):
    machine_id: str
    name: str
    kind: str
    state: str
    speed_setpoint: float
    temperature_c: float
    vibration_mm_s: float
    power_kw: float
    throughput_uph: float
    total_units: float
    good_units: float
    scrap_units: float
    downtime_seconds: float
    health_score: float
    buffer_fill_pct: float
    signal_rms: float = 0.0
    signal_peak: float = 0.0
    trace_id: str = ""
    trace_quality: str = ""
    alarm_code: str | None = None
    detail: str = ""


class AlarmRecord(BaseModel):
    timestamp: str
    machine_id: str
    severity: Literal["info", "warning", "critical"]
    code: str
    message: str
    acknowledged: bool = False


class EventFeedItem(BaseModel):
    timestamp: str
    severity: Literal["info", "warning", "critical"]
    event_type: str
    title: str
    detail: str
    source: str


class HistoryPoint(BaseModel):
    timestamp: str
    throughput_uph: float
    downtime_pct: float
    health_score: float
    power_kw: float
    active_alarm_count: int


class LineMetrics(BaseModel):
    line_state: str
    mode: str
    throughput_uph: float
    target_throughput_uph: float
    downtime_pct: float
    overall_health: float
    oee: float
    energy_kw: float
    good_units: float
    scrap_units: float
    active_alarm_count: int
    availability_pct: float
    quality_pct: float
    performance_pct: float
    buffer_utilization_pct: float


class ScenarioDefinition(BaseModel):
    scenario_id: str
    name: str
    description: str
    impact: str


class StreamChannelDefinition(BaseModel):
    channel_id: str
    label: str
    protocol: Literal["sse", "websocket"]
    endpoint: str
    description: str


class TraceUploadBinding(BaseModel):
    machine_id: str
    machine_name: str
    role: Literal["normal", "fault"]
    trace_id: str
    file_name: str
    source_format: Literal["hdf5", "csv"]
    quality_label: Literal["good", "bad"]
    sample_rate_hz: int
    sample_count: int
    window_count: int


class DashboardSnapshot(BaseModel):
    generated_at: str
    site_name: str
    line_name: str
    data_source: str
    data_source_detail: str
    transport: str
    kafka_enabled: bool
    metrics: LineMetrics
    machines: list[MachineSnapshot]
    alarms: list[AlarmRecord]
    events: list[EventFeedItem]
    history: list[HistoryPoint]
    scenarios: list[ScenarioDefinition]
    trace_uploads: list[TraceUploadBinding]


class LineControlRequest(BaseModel):
    action: Literal["start", "stop", "reset"]


class MachineControlRequest(BaseModel):
    action: Literal["set_speed", "maintenance", "clear_fault", "fault"]
    speed: float | None = Field(default=None, ge=0.5, le=1.3)
    enabled: bool | None = None
    duration_seconds: int = Field(default=20, ge=5, le=300)
    code: str | None = None
    detail: str | None = None


class ScenarioTriggerRequest(BaseModel):
    duration_seconds: int = Field(default=25, ge=5, le=300)


class ControlResponse(BaseModel):
    status: str
    detail: str


class DataUploadResponse(BaseModel):
    status: str
    detail: str
    binding: TraceUploadBinding
    data_source: str
    data_source_detail: str
