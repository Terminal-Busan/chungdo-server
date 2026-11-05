from unittest.mock import AsyncMock

import psycopg
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_root_preserves_existing_response():
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "Hello World!"
    assert response.headers["content-type"].startswith("text/plain")


def test_readiness_without_database_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "message": "Database is unavailable",
        "error": "Service Unavailable",
        "statusCode": 503,
    }


def test_readiness_checks_database_and_closes_connection(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:secret@localhost/test")
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert connect.call_args.kwargs["connect_timeout"] == 5
    connection.execute.assert_awaited_once_with("SELECT 1")
    connection.__aexit__.assert_awaited_once()


def test_readiness_hides_database_failure_details(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:secret@localhost/test")
    connect = AsyncMock(side_effect=psycopg.OperationalError("private detail"))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert "private detail" not in response.text
    assert "secret" not in response.text


def test_openapi_is_available():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/health/ready" in response.json()["paths"]
