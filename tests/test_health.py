import logging

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_db
from app.main import app


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["database"] == "ok"
    assert ready.json()["redis"] == "ok"


def test_ready_returns_503_when_database_is_down(client):
    class DeadSession:
        def execute(self, *args, **kwargs):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_db] = lambda: DeadSession()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_request_id_is_generated_and_reused(client):
    generated = client.get("/health").headers["X-Request-ID"]
    assert len(generated) == 32

    echoed = client.get("/health", headers={"X-Request-ID": "my-trace-123"})
    assert echoed.headers["X-Request-ID"] == "my-trace-123"

    unsafe = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert unsafe.headers["X-Request-ID"] != "bad id\twith spaces"


def test_request_id_appears_in_logs(client, caplog):
    from app.core.logging import RequestIdFilter

    caplog.handler.addFilter(RequestIdFilter())
    with caplog.at_level(logging.INFO, logger="taskpilot.request"):
        client.get("/health", headers={"X-Request-ID": "trace-abc"})

    assert any(getattr(r, "request_id", None) == "trace-abc" for r in caplog.records)


def test_metrics_endpoint_exposes_request_counters(client):
    client.get("/health")

    body = client.get("/metrics").text

    assert 'http_requests_total{method="GET",path="/health",status="200"}' in body
    assert "http_request_duration_seconds_bucket" in body
    assert "ai_plan_generation_total" in body
    assert "ai_plan_generation_failures_total" in body


def test_unexpected_errors_return_500_without_internals(client):
    def explode():
        raise RuntimeError("secret internal detail")

    app.dependency_overrides[get_db] = explode
    with TestClient(app, raise_server_exceptions=False) as quiet_client:
        response = quiet_client.get("/ready")

    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_are_served(client, path):
    assert client.get(path).status_code == 200
