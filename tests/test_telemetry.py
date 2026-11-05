import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

from app import telemetry


@pytest.fixture
def metrics(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], resource=Resource({}))
    meter = provider.get_meter("test")
    monkeypatch.setattr(
        telemetry, "request_duration", meter.create_histogram("http.duration")
    )
    monkeypatch.setattr(
        telemetry, "agent_duration", meter.create_histogram("agent.duration")
    )
    yield reader
    provider.shutdown()


def points(reader):
    return [
        point
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
    ]


def test_request_metrics_never_include_input_or_actual_identifier(metrics):
    app = FastAPI()
    app.add_middleware(telemetry.MetricsMiddleware)

    @app.get("/sessions/{identifier}")
    def session(identifier: str):
        return {"ok": True}

    client = TestClient(app)
    assert (
        client.get(
            "/sessions/private-id?token=private-token",
            headers={"Authorization": "secret"},
        ).status_code
        == 200
    )
    assert client.get("/unknown-private-path?phone=010-1234-5678").status_code == 404
    recorded = points(metrics)
    assert {p.attributes["http.route"] for p in recorded} == {
        "/sessions/{identifier}",
        "unmatched",
    }
    assert {p.attributes["http.status_code"] for p in recorded} == {200, 404}
    assert all(p.count == 1 for p in recorded)
    assert "private" not in repr(recorded) and "secret" not in repr(recorded)


def test_agent_success_fallback_and_failure_are_distinct(metrics):
    with telemetry.agent_stage("RejectionInterpreter") as observation:
        observation["outcome"] = "success"
    with telemetry.agent_stage("PolicyMatcher") as observation:
        telemetry.note_fallback()
    with pytest.raises(ValueError), telemetry.agent_stage("PathPlanner"):
        raise ValueError("private-notice")
    recorded = points(metrics)
    assert {p.attributes["outcome"] for p in recorded} == {
        "success",
        "fallback",
        "error",
    }
    assert "private-notice" not in repr(recorded)


def test_monitoring_failure_does_not_break_requests(monkeypatch):
    def broken(*args):
        raise RuntimeError("private-key")

    monkeypatch.setattr(telemetry, "request_duration", SimpleNamespace(record=broken))
    app = FastAPI()
    app.add_middleware(telemetry.MetricsMiddleware)

    @app.get("/health")
    def health():
        return {"ok": True}

    assert TestClient(app).get("/health").json() == {"ok": True}


def test_unconfigured_monitoring_is_disabled(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(telemetry, "provider", None)
    telemetry.configure()
    assert telemetry.provider is None


def test_invalid_connection_string_is_not_logged(monkeypatch, caplog):
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", "private-invalid-secret"
    )
    monkeypatch.setattr(telemetry, "provider", None)
    telemetry.configure()
    assert telemetry.provider is None
    assert "private-invalid-secret" not in caplog.text


def test_shutdown_errors_are_safe(monkeypatch, caplog):
    def broken(**kwargs):
        raise RuntimeError("private-key")

    monkeypatch.setattr(telemetry, "provider", SimpleNamespace(shutdown=broken))
    asyncio.run(telemetry.shutdown())
    assert telemetry.provider is None and "private-key" not in caplog.text
