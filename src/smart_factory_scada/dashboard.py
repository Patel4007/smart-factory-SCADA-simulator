from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .hub import SnapshotHub
from .models import (
    AlarmRecord,
    DashboardSnapshot,
    EventFeedItem,
    HistoryPoint,
    LineMetrics,
    MachineSnapshot,
    ScenarioDefinition,
    TraceUploadBinding,
)


class DashboardStore:
    def __init__(
        self,
        *,
        site_name: str,
        line_name: str,
        data_source: str,
        data_source_detail: str,
        transport: str,
        kafka_enabled: bool,
        scenarios: list[dict[str, Any]],
        hub: SnapshotHub,
        history_limit: int,
        event_limit: int,
        alarm_limit: int,
        machine_snapshots: list[dict[str, Any]],
        metrics_snapshot: dict[str, Any],
        trace_uploads: list[dict[str, Any]],
    ) -> None:
        self.site_name = site_name
        self.line_name = line_name
        self.data_source = data_source
        self.data_source_detail = data_source_detail
        self.transport = transport
        self.kafka_enabled = kafka_enabled
        self.history_limit = history_limit
        self.event_limit = event_limit
        self.alarm_limit = alarm_limit
        self.hub = hub
        self.scenarios = [ScenarioDefinition.model_validate(item) for item in scenarios]
        self.machine_order = [item["machine_id"] for item in machine_snapshots]
        self.trace_uploads = [TraceUploadBinding.model_validate(item) for item in trace_uploads]
        self.reset(machine_snapshots=machine_snapshots, metrics_snapshot=metrics_snapshot)

    def reset(self, *, machine_snapshots: list[dict[str, Any]], metrics_snapshot: dict[str, Any]) -> None:
        self.generated_at = metrics_snapshot["timestamp"]
        self.metrics = self._coerce_metrics(metrics_snapshot)
        self.machines: OrderedDict[str, MachineSnapshot] = OrderedDict(
            (item["machine_id"], MachineSnapshot.model_validate(item)) for item in machine_snapshots
        )
        self.alarms: list[AlarmRecord] = []
        self.events: list[EventFeedItem] = []
        self.history: list[HistoryPoint] = []

    async def handle_event(self, topic: str, payload: dict[str, Any]) -> None:
        self.generated_at = payload.get("timestamp", self.generated_at)
        if payload.get("event_type") is not None:
            self._apply_control(payload)
        elif payload.get("machine_id") is not None:
            self._apply_machine(payload)
        else:
            self._apply_metrics(payload)
        await self.hub.publish(self.snapshot().model_dump(mode="json"))

    def snapshot(self) -> DashboardSnapshot:
        machines = [self.machines[machine_id] for machine_id in self.machine_order if machine_id in self.machines]
        return DashboardSnapshot(
            generated_at=self.generated_at,
            site_name=self.site_name,
            line_name=self.line_name,
            data_source=self.data_source,
            data_source_detail=self.data_source_detail,
            transport=self.transport,
            kafka_enabled=self.kafka_enabled,
            metrics=self.metrics,
            machines=machines,
            alarms=self.alarms,
            events=self.events,
            history=self.history,
            scenarios=self.scenarios,
            trace_uploads=self.trace_uploads,
        )

    def update_data_source(
        self,
        *,
        data_source: str,
        data_source_detail: str,
        trace_uploads: list[dict[str, Any]],
    ) -> None:
        self.data_source = data_source
        self.data_source_detail = data_source_detail
        self.trace_uploads = [TraceUploadBinding.model_validate(item) for item in trace_uploads]

    def _apply_machine(self, payload: dict[str, Any]) -> None:
        machine = MachineSnapshot.model_validate(payload)
        self.machines[machine.machine_id] = machine

    def _apply_metrics(self, payload: dict[str, Any]) -> None:
        self.metrics = self._coerce_metrics(payload)
        point = HistoryPoint.model_validate(
            {
                "timestamp": payload["timestamp"],
                "throughput_uph": self.metrics.throughput_uph,
                "downtime_pct": self.metrics.downtime_pct,
                "health_score": self.metrics.overall_health,
                "power_kw": self.metrics.energy_kw,
                "active_alarm_count": self.metrics.active_alarm_count,
            }
        )
        self.history.insert(0, point)
        self.history = self.history[: self.history_limit]

    def _apply_control(self, payload: dict[str, Any]) -> None:
        event = EventFeedItem.model_validate(payload)
        self.events.insert(0, event)
        self.events = self.events[: self.event_limit]

        if event.event_type == "alarm":
            self._upsert_alarm(payload)
        elif event.event_type in {"recovery", "clear_fault", "reset"}:
            self._clear_alarm(payload.get("source", ""), payload.get("code"))

    def _upsert_alarm(self, payload: dict[str, Any]) -> None:
        machine_id = payload.get("source", "")
        code = payload.get("code", "ALARM")
        message = payload.get("detail", payload.get("title", "Alarm detected"))
        severity = payload.get("severity", "warning")
        record = AlarmRecord.model_validate(
            {
                "timestamp": payload["timestamp"],
                "machine_id": machine_id,
                "severity": severity,
                "code": code,
                "message": message,
                "acknowledged": False,
            }
        )
        for index, current in enumerate(self.alarms):
            if current.machine_id == machine_id and current.code == code:
                self.alarms[index] = record
                break
        else:
            self.alarms.insert(0, record)
        self.alarms = self.alarms[: self.alarm_limit]

    def _clear_alarm(self, machine_id: str, code: str | None) -> None:
        if not machine_id:
            self.alarms = []
            return
        self.alarms = [
            alarm
            for alarm in self.alarms
            if not (alarm.machine_id == machine_id and (code is None or alarm.code == code))
        ]

    @staticmethod
    def _coerce_metrics(payload: dict[str, Any]) -> LineMetrics:
        return LineMetrics.model_validate({key: value for key, value in payload.items() if key != "timestamp"})
