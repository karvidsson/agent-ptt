"""Production entrypoint validation and public, non-sensitive health checks."""

import pytest
from sqlalchemy.exc import OperationalError

from agent_ptt import runtime
from agent_ptt.db import get_db
from agent_ptt.server import app


@pytest.mark.parametrize("hosted", ["0", "1"])
def test_health_endpoints_work_without_auth(client, monkeypatch, hosted):
    monkeypatch.setenv("AGENT_PTT_HOSTED", hosted)
    monkeypatch.setenv("AGENT_PTT_API_KEY", "private")
    assert client.get("/health/live").json() == {"status": "ok"}
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_database_outage_fails_readiness_without_leaking_details(client):
    class BrokenDB:
        def execute(self, statement):
            raise OperationalError("SELECT 1", {}, Exception("secret database password"))

    app.dependency_overrides[get_db] = lambda: BrokenDB()
    try:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert client.get("/health/live").status_code == 200
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    monkeypatch.setenv("AGENT_PTT_PUBLIC_ORIGIN", "https://chat.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:password@db/ptt")
    monkeypatch.setenv("PORT", "8081")


def test_runtime_execs_single_worker_and_accepts_cloud_port(production, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.os, "execvp", lambda *args: calls.append(args))
    runtime.serve()
    executable, args = calls[0]
    assert executable == "uvicorn"
    assert args[args.index("--port") + 1] == "8081"
    assert args[args.index("--host") + 1] == "0.0.0.0"
    assert args[args.index("--workers") + 1] == "1"


@pytest.mark.parametrize(
    "key,value",
    [
        ("AGENT_PTT_HOSTED", "0"),
        ("AGENT_PTT_PUBLIC_ORIGIN", ""),
        ("AGENT_PTT_PUBLIC_ORIGIN", "http://public.example"),
        ("DATABASE_URL", ""),
        ("DATABASE_URL", "sqlite:///ephemeral.db"),
        ("PORT", "0"),
        ("PORT", "65536"),
    ],
)
def test_invalid_production_configuration_fails_before_start(production, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(runtime.os, "execvp", lambda *args: pytest.fail("server must not start"))
    with pytest.raises((ValueError, RuntimeError)):
        runtime.serve()
