"""Opt-in aggregate metrics. Never export bodies, queries, IDs, headers or errors."""

import asyncio
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

logger = logging.getLogger(__name__)
provider = None
request_duration = None
agent_duration = None
current_stage = ContextVar("telemetry_stage", default=None)


def configure():
    global provider, request_duration, agent_duration
    connection = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection or provider is not None:
        return
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        exporter = AzureMonitorMetricExporter(
            connection_string=connection,
            disable_offline_storage=True,
            connection_timeout=3,
            read_timeout=3,
            retry_total=0,
        )
        reader = PeriodicExportingMetricReader(
            exporter, export_interval_millis=60000, export_timeout_millis=5000
        )
        provider = MeterProvider(
            resource=Resource({"service.name": "welfare-next-backend"}),
            metric_readers=[reader],
            shutdown_on_exit=False,
        )
        meter = provider.get_meter("welfare-next")
        request_duration = meter.create_histogram("welfare.http.duration", unit="s")
        agent_duration = meter.create_histogram("welfare.agent.duration", unit="s")
    except Exception:  # noqa: BLE001 -- Monitoring must not break API startup or expose configuration.
        logger.warning("Monitoring configuration unavailable")


def record(instrument, duration, attributes):
    if instrument is None:
        return
    try:
        instrument.record(max(duration, 0), attributes)
    except Exception:  # noqa: BLE001 -- Exporter/instrument failure must not affect user requests.
        logger.warning("Monitoring metric unavailable")


@contextmanager
def agent_stage(agent):
    started = perf_counter()
    observation = {"outcome": "error"}
    token = current_stage.set(observation)
    try:
        yield observation
    finally:
        current_stage.reset(token)
        record(
            agent_duration,
            perf_counter() - started,
            {
                "agent": agent
                if agent in {"RejectionInterpreter", "PolicyMatcher", "PathPlanner"}
                else "other",
                "outcome": observation["outcome"],
            },
        )


def note_fallback():
    observation = current_stage.get()
    if observation is not None:
        observation["outcome"] = "fallback"


async def shutdown():
    global provider, request_duration, agent_duration
    current = provider
    provider = request_duration = agent_duration = None
    if current is not None:
        try:
            await asyncio.to_thread(current.shutdown, timeout_millis=1500)
        except Exception:  # noqa: BLE001 -- Best-effort metrics flush, never print exporter details.
            logger.warning("Monitoring shutdown unavailable")


class MetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = perf_counter()
        status = 500

        async def measured_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope.get("method")
            record(
                request_duration,
                perf_counter() - started,
                {
                    "http.route": route,
                    "http.method": method
                    if method
                    in {"GET", "POST", "DELETE", "OPTIONS", "HEAD", "PUT", "PATCH"}
                    else "OTHER",
                    "http.status_code": status,
                },
            )
