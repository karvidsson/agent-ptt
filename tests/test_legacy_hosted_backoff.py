"""Legacy hooks must not hammer an organization server after a mode mismatch."""

import importlib.util
import io
import json
import urllib.error
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def routing(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "plugins/codex-announcer/session_routing.py"
    spec = importlib.util.spec_from_file_location("backoff_routing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROUTES_DIR", tmp_path)
    return module


def error(detail, code=404):
    return urllib.error.HTTPError(
        "http://localhost:8770/channels",
        code,
        "error",
        {},
        io.BytesIO(json.dumps({"detail": detail}).encode()),
    )


def test_hosted_response_pauses_other_hook_processes_then_retries(routing, monkeypatch, capsys):
    now = [1000]
    monkeypatch.setattr(routing.time, "time", lambda: now[0])
    calls = []

    def unavailable(request, **kwargs):
        calls.append(request.full_url)
        raise error("Local-only endpoint is disabled in hosted mode")

    monkeypatch.setattr(routing.urllib.request, "urlopen", unavailable)
    request = urllib.request.Request("http://localhost:8770/channels")
    with pytest.raises(routing.WorkspaceRequired, match="enrollment"):
        routing.local_urlopen(request, timeout=1)
    assert "/workspace/SKILL.md" in capsys.readouterr().err
    for method in ("GET", "POST"):
        with pytest.raises(routing.WorkspaceRequired):
            routing.local_urlopen(
                urllib.request.Request(request.full_url, method=method), timeout=1
            )
    assert len(calls) == 1
    assert capsys.readouterr().err == ""
    # A different server is independent of the cached mismatch.
    with pytest.raises(routing.WorkspaceRequired):
        routing.local_urlopen(urllib.request.Request("http://localhost:9000/channels"), timeout=1)
    assert len(calls) == 2
    now[0] += 301
    result = object()
    probes = []

    def probe(url, **kwargs):
        if isinstance(url, str):
            probes.append(url)
            return nullcontext(SimpleNamespace(headers={"X-Agent-PTT-Mode": "local"}))
        return result

    monkeypatch.setattr(routing.urllib.request, "urlopen", probe)
    assert routing.local_urlopen(request, timeout=1) is result
    assert probes == ["http://localhost:8770/health/live"]


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_other_errors_keep_status_and_body_without_backoff(routing, monkeypatch, code):
    def unavailable(*args, **kwargs):
        raise error("Channel not found", code)

    monkeypatch.setattr(routing.urllib.request, "urlopen", unavailable)
    for _ in range(2):
        with pytest.raises(urllib.error.HTTPError) as caught:
            routing.local_urlopen(
                urllib.request.Request("http://localhost:8770/channels/nope"), timeout=1
            )
        assert caught.value.code == code
        assert json.loads(caught.value.read()) == {"detail": "Channel not found"}
    assert list(routing.ROUTES_DIR.iterdir()) == []


@pytest.mark.parametrize("mode", ["workspace", None, "offline"])
def test_expired_backoff_never_retries_channel_on_hosted_or_unknown_server(
    routing, monkeypatch, mode
):
    now = [1000]
    monkeypatch.setattr(routing.time, "time", lambda: now[0])

    def disabled(*args, **kwargs):
        raise error("Local-only endpoint is disabled in hosted mode")

    monkeypatch.setattr(routing.urllib.request, "urlopen", disabled)
    request = urllib.request.Request("http://localhost:8770/channels", method="POST")
    with pytest.raises(routing.WorkspaceRequired):
        routing.local_urlopen(request, timeout=1)
    calls = []

    def probe(url, **kwargs):
        calls.append(url)
        assert url == "http://localhost:8770/health/live"
        if mode == "offline":
            raise OSError("offline")
        return nullcontext(SimpleNamespace(headers={"X-Agent-PTT-Mode": mode}))

    monkeypatch.setattr(routing.urllib.request, "urlopen", probe)
    for _ in range(3):
        now[0] += 301
        with pytest.raises(routing.WorkspaceRequired):
            routing.local_urlopen(request, timeout=1)
        with pytest.raises(routing.WorkspaceRequired):
            routing.local_urlopen(request, timeout=1)
    assert len(calls) == 3
