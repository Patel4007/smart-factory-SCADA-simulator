from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .config import Settings

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class BaseTelemetryBus:
    def __init__(self, topics: list[str]) -> None:
        self.topics = topics
        self.handlers: list[EventHandler] = []

    def add_handler(self, handler: EventHandler) -> None:
        self.handlers.append(handler)

    async def _dispatch(self, topic: str, payload: dict[str, Any]) -> None:
        for handler in self.handlers:
            await handler(topic, payload)

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError


class InMemoryTelemetryBus(BaseTelemetryBus):
    def __init__(self, topics: list[str]) -> None:
        super().__init__(topics)
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.consumer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        if self.consumer_task is None:
            return
        self.consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.consumer_task

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        await self.queue.put((topic, payload))

    async def _consume_loop(self) -> None:
        while True:
            topic, payload = await self.queue.get()
            await self._dispatch(topic, payload)


class KafkaTelemetryBus(BaseTelemetryBus):
    def __init__(self, settings: Settings, topics: list[str]) -> None:
        super().__init__(topics)
        serializer = lambda value: json.dumps(value).encode("utf-8")
        deserializer = lambda value: json.loads(value.decode("utf-8"))
        self.producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            client_id=settings.kafka_client_id,
            value_serializer=serializer,
        )
        self.consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            client_id=f"{settings.kafka_client_id}-consumer",
            group_id=settings.kafka_group_id,
            auto_offset_reset="latest",
            enable_auto_commit=True,
            value_deserializer=deserializer,
        )
        self.startup_timeout_seconds = settings.kafka_startup_timeout_seconds
        self.consumer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.wait_for(self.producer.start(), timeout=self.startup_timeout_seconds)
        await asyncio.wait_for(self.consumer.start(), timeout=self.startup_timeout_seconds)
        self.consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        if self.consumer_task is not None:
            self.consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.consumer_task
        await self.consumer.stop()
        await self.producer.stop()

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        await self.producer.send_and_wait(topic, payload)

    async def _consume_loop(self) -> None:
        async for message in self.consumer:
            await self._dispatch(message.topic, message.value)


async def create_bus(settings: Settings) -> tuple[BaseTelemetryBus, str, bool, str | None]:
    topics = [settings.kafka_machine_topic, settings.kafka_kpi_topic, settings.kafka_control_topic]
    if settings.use_kafka:
        kafka_bus = KafkaTelemetryBus(settings, topics)
        try:
            await kafka_bus.start()
            return kafka_bus, "kafka", True, None
        except Exception as exc:
            with suppress(Exception):
                await kafka_bus.stop()
            fallback = InMemoryTelemetryBus(topics)
            await fallback.start()
            detail = f"Kafka was requested but unavailable, so the simulator fell back to in-memory streaming: {exc}"
            return fallback, "in-memory", False, detail
    fallback = InMemoryTelemetryBus(topics)
    await fallback.start()
    return fallback, "in-memory", False, None
