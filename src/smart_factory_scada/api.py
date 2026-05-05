from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .dashboard import DashboardStore
from .hub import SnapshotHub
from .models import (
    ControlResponse,
    DataUploadResponse,
    DashboardSnapshot,
    LineControlRequest,
    MachineControlRequest,
    ScenarioDefinition,
    ScenarioTriggerRequest,
    StreamChannelDefinition,
)
from .simulation import SmartFactoryEngine, utc_timestamp
from .streaming import BaseTelemetryBus, create_bus


async def simulation_loop(engine: SmartFactoryEngine, bus: BaseTelemetryBus) -> None:
    while True:
        for topic, payload in engine.step():
            await bus.publish(topic, payload)
        await asyncio.sleep(engine.settings.simulation_tick_seconds)


def stream_channel_catalog() -> list[StreamChannelDefinition]:
    return [
        StreamChannelDefinition(
            channel_id="server_sent_events",
            label="Server-Sent Events",
            protocol="sse",
            endpoint="/api/events",
            description="One-way HTTP event stream for dashboard snapshots.",
        ),
        StreamChannelDefinition(
            channel_id="websocket_live",
            label="WebSocket Channel",
            protocol="websocket",
            endpoint="/ws/events",
            description="Bidirectional real-time channel for low-latency SCADA updates.",
        ),
    ]


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_env()
    static_dir = Path(__file__).resolve().parent / "static"
    asset_version = str(
        int(
            max(
                (static_dir / "index.html").stat().st_mtime,
                (static_dir / "styles.css").stat().st_mtime,
                (static_dir / "app.js").stat().st_mtime,
            )
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = SmartFactoryEngine(app_settings)
        hub = SnapshotHub()
        bus, transport, kafka_enabled, startup_notice = await create_bus(app_settings)
        store = DashboardStore(
            site_name=app_settings.site_name,
            line_name=app_settings.line_name,
            data_source=engine.data_source,
            data_source_detail=engine.data_source_detail,
            transport=transport,
            kafka_enabled=kafka_enabled,
            scenarios=engine.scenario_catalog(),
            hub=hub,
            history_limit=app_settings.history_limit,
            event_limit=app_settings.event_limit,
            alarm_limit=app_settings.alarm_limit,
            machine_snapshots=engine.machine_snapshots(),
            metrics_snapshot=engine.line_metrics_snapshot(),
            trace_uploads=engine.uploaded_trace_catalog(),
        )
        bus.add_handler(store.handle_event)

        app.state.settings = app_settings
        app.state.engine = engine
        app.state.hub = hub
        app.state.bus = bus
        app.state.store = store

        for topic, payload in engine.snapshot_events():
            await bus.publish(topic, payload)
        if startup_notice:
            await bus.publish(
                app_settings.kafka_control_topic,
                {
                    "timestamp": utc_timestamp(),
                    "severity": "warning",
                    "event_type": "scenario",
                    "title": "Kafka fallback active",
                    "detail": startup_notice,
                    "source": "stream-router",
                    "code": "KAFKA_FALLBACK",
                },
            )

        app.state.simulation_task = asyncio.create_task(simulation_loop(engine, bus))
        try:
            yield
        finally:
            app.state.simulation_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.simulation_task
            await bus.stop()

    app = FastAPI(
        title="Smart Factory SCADA Simulator",
        version="0.1.0",
        description="Kafka-backed industrial automation simulator with live telemetry and operator controls.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def disable_browser_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        html = html.replace("/static/styles.css", f"/static/styles.css?v={asset_version}")
        html = html.replace("/static/app.js", f"/static/app.js?v={asset_version}")
        return HTMLResponse(html)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, str | bool]:
        store: DashboardStore = request.app.state.store
        return {
            "status": "ok",
            "transport": store.transport,
            "kafka_enabled": store.kafka_enabled,
            "data_source": store.data_source,
        }

    @app.get("/api/dashboard", response_model=DashboardSnapshot)
    async def dashboard(request: Request) -> DashboardSnapshot:
        store: DashboardStore = request.app.state.store
        return store.snapshot()

    @app.get("/api/stream/channels", response_model=list[StreamChannelDefinition])
    async def stream_channels() -> list[StreamChannelDefinition]:
        return stream_channel_catalog()

    @app.get("/api/scenarios", response_model=list[ScenarioDefinition])
    async def scenarios(request: Request) -> list[ScenarioDefinition]:
        store: DashboardStore = request.app.state.store
        return store.scenarios

    @app.get("/api/data/uploads")
    async def data_uploads(request: Request) -> dict[str, object]:
        store: DashboardStore = request.app.state.store
        return {
            "data_source": store.data_source,
            "data_source_detail": store.data_source_detail,
            "trace_uploads": [item.model_dump(mode="json") for item in store.trace_uploads],
        }

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        hub: SnapshotHub = request.app.state.hub
        store: DashboardStore = request.app.state.store
        settings_obj: Settings = request.app.state.settings
        queue = hub.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                initial = store.snapshot().model_dump_json()
                yield f"event: snapshot\ndata: {initial}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=settings_obj.sse_heartbeat_seconds)
                        yield f"event: snapshot\ndata: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        hub: SnapshotHub = websocket.app.state.hub
        store: DashboardStore = websocket.app.state.store
        queue = hub.subscribe()
        try:
            await websocket.send_text(store.snapshot().model_dump_json())
            while True:
                payload = await queue.get()
                await websocket.send_text(payload)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)

    @app.post("/api/control/line", response_model=ControlResponse)
    async def control_line(request: Request, payload: LineControlRequest) -> ControlResponse:
        engine: SmartFactoryEngine = request.app.state.engine
        bus: BaseTelemetryBus = request.app.state.bus
        store: DashboardStore = request.app.state.store
        event = engine.apply_line_action(payload.action)
        if payload.action == "reset":
            store.reset(machine_snapshots=engine.machine_snapshots(), metrics_snapshot=engine.line_metrics_snapshot())
        await bus.publish(app_settings.kafka_control_topic, event)
        for topic, item in engine.snapshot_events():
            await bus.publish(topic, item)
        return ControlResponse(status="ok", detail=event["detail"])

    @app.post("/api/control/machines/{machine_id}", response_model=ControlResponse)
    async def control_machine(request: Request, machine_id: str, payload: MachineControlRequest) -> ControlResponse:
        engine: SmartFactoryEngine = request.app.state.engine
        bus: BaseTelemetryBus = request.app.state.bus
        try:
            event = engine.apply_machine_control(
                machine_id,
                action=payload.action,
                speed=payload.speed,
                enabled=payload.enabled,
                duration_seconds=payload.duration_seconds,
                code=payload.code,
                detail=payload.detail,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await bus.publish(app_settings.kafka_control_topic, event)
        for topic, item in engine.snapshot_events():
            await bus.publish(topic, item)
        return ControlResponse(status="ok", detail=event["detail"])

    @app.post("/api/scenarios/{scenario_id}", response_model=ControlResponse)
    async def trigger_scenario(request: Request, scenario_id: str, payload: ScenarioTriggerRequest) -> ControlResponse:
        engine: SmartFactoryEngine = request.app.state.engine
        bus: BaseTelemetryBus = request.app.state.bus
        try:
            event = engine.inject_scenario(scenario_id, payload.duration_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await bus.publish(app_settings.kafka_control_topic, event)
        for topic, item in engine.snapshot_events():
            await bus.publish(topic, item)
        return ControlResponse(status="ok", detail=event["detail"])

    @app.put("/api/data/upload/{machine_id}/{role}", response_model=DataUploadResponse)
    async def upload_trace(
        request: Request,
        machine_id: str,
        role: str,
        file_name: str,
        sample_rate_hz: int | None = None,
    ) -> DataUploadResponse:
        engine: SmartFactoryEngine = request.app.state.engine
        bus: BaseTelemetryBus = request.app.state.bus
        store: DashboardStore = request.app.state.store
        payload = await request.body()
        try:
            result = engine.upload_trace(
                machine_id,
                role,
                file_name=file_name,
                payload=payload,
                sample_rate_hz=sample_rate_hz,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.update_data_source(
            data_source=engine.data_source,
            data_source_detail=engine.data_source_detail,
            trace_uploads=engine.uploaded_trace_catalog(),
        )
        await bus.publish(
            app_settings.kafka_control_topic,
            {
                "timestamp": utc_timestamp(),
                "severity": "info",
                "event_type": "operator_action",
                "title": "Trace uploaded",
                "detail": result["detail"],
                "source": machine_id,
                "code": f"UPLOAD_{role.upper()}",
            },
        )
        for topic, item in engine.snapshot_events():
            await bus.publish(topic, item)
        return DataUploadResponse(
            status="ok",
            detail=result["detail"],
            binding=result["binding"],
            data_source=engine.data_source,
            data_source_detail=engine.data_source_detail,
        )

    return app


app = create_app()


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run("smart_factory_scada.api:app", host=settings.host, port=settings.port, reload=False)
