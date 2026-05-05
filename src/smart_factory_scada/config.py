from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


def _default_real_data_dir() -> str:
    return str(Path(__file__).resolve().parents[2] / "data" / "bosch_cnc_sample")


def _default_uploaded_data_dir() -> str:
    return str(Path(__file__).resolve().parents[2] / "data" / "uploads")


@dataclass(slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    site_name: str = "North River Plant"
    line_name: str = "Line A - Precision Assembly"
    simulation_tick_seconds: float = 1.0
    history_limit: int = 90
    event_limit: int = 16
    alarm_limit: int = 12
    random_seed: int = 7
    use_kafka: bool = False
    kafka_bootstrap_servers: str = "127.0.0.1:9092"
    kafka_client_id: str = "scada-simulator"
    kafka_group_id: str = "scada-dashboard"
    kafka_machine_topic: str = "scada.machine.telemetry"
    kafka_kpi_topic: str = "scada.line.kpis"
    kafka_control_topic: str = "scada.control.events"
    kafka_startup_timeout_seconds: float = 5.0
    sse_heartbeat_seconds: float = 15.0
    initial_line_running: bool = True
    real_data_dir: str = _default_real_data_dir()
    uploaded_data_dir: str = _default_uploaded_data_dir()
    real_data_window_samples: int = 2000
    upload_max_bytes: int = 64 * 1024 * 1024
    data_source_name: str = "Bosch CNC Machining Dataset"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("SCADA_HOST", "127.0.0.1"),
            port=_read_int("SCADA_PORT", 8000),
            site_name=os.getenv("SCADA_SITE_NAME", "North River Plant"),
            line_name=os.getenv("SCADA_LINE_NAME", "Line A - Precision Assembly"),
            simulation_tick_seconds=_read_float("SCADA_SIMULATION_TICK_SECONDS", 1.0),
            history_limit=_read_int("SCADA_HISTORY_LIMIT", 90),
            event_limit=_read_int("SCADA_EVENT_LIMIT", 16),
            alarm_limit=_read_int("SCADA_ALARM_LIMIT", 12),
            random_seed=_read_int("SCADA_RANDOM_SEED", 7),
            use_kafka=_read_bool("SCADA_USE_KAFKA", False),
            kafka_bootstrap_servers=os.getenv("SCADA_KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092"),
            kafka_client_id=os.getenv("SCADA_KAFKA_CLIENT_ID", "scada-simulator"),
            kafka_group_id=os.getenv("SCADA_KAFKA_GROUP_ID", "scada-dashboard"),
            kafka_machine_topic=os.getenv("SCADA_KAFKA_MACHINE_TOPIC", "scada.machine.telemetry"),
            kafka_kpi_topic=os.getenv("SCADA_KAFKA_KPI_TOPIC", "scada.line.kpis"),
            kafka_control_topic=os.getenv("SCADA_KAFKA_CONTROL_TOPIC", "scada.control.events"),
            kafka_startup_timeout_seconds=_read_float("SCADA_KAFKA_STARTUP_TIMEOUT_SECONDS", 5.0),
            sse_heartbeat_seconds=_read_float("SCADA_SSE_HEARTBEAT_SECONDS", 15.0),
            initial_line_running=_read_bool("SCADA_INITIAL_LINE_RUNNING", True),
            real_data_dir=os.getenv("SCADA_REAL_DATA_DIR", _default_real_data_dir()),
            uploaded_data_dir=os.getenv("SCADA_UPLOADED_DATA_DIR", _default_uploaded_data_dir()),
            real_data_window_samples=_read_int("SCADA_REAL_DATA_WINDOW_SAMPLES", 2000),
            upload_max_bytes=_read_int("SCADA_UPLOAD_MAX_BYTES", 64 * 1024 * 1024),
            data_source_name=os.getenv("SCADA_DATA_SOURCE_NAME", "Bosch CNC Machining Dataset"),
        )
