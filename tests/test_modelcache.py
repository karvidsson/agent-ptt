"""Model cache helpers and the `model` CLI group.

huggingface_hub is faked in sys.modules so tests run on the base install.
"""

import importlib.machinery
import importlib.util
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agent_ptt.cli import app
from agent_ptt.modelcache import (
    DEFAULT_CHECKPOINT,
    download_model,
    format_size,
    get_cached_model,
    hub_available,
    list_cached_models,
)

runner = CliRunner()


def _flat(output: str) -> str:
    """Collapse whitespace so Rich's 80-col line wrapping can't split phrases."""
    return " ".join(output.split())


def _repo(repo_id, size=1024, nb_files=3, repo_type="model"):
    return SimpleNamespace(
        repo_id=repo_id,
        size_on_disk=size,
        nb_files=nb_files,
        repo_path=f"/hf-cache/{repo_id}",
        repo_type=repo_type,
    )


@pytest.fixture
def fake_hub(monkeypatch):
    """Install a fake huggingface_hub with a controllable cache."""
    hub = types.ModuleType("huggingface_hub")
    # find_spec raises for sys.modules entries without a __spec__
    hub.__spec__ = importlib.machinery.ModuleSpec("huggingface_hub", loader=None)
    hub.repos = []
    hub.downloads = []
    hub.scan_cache_dir = lambda: SimpleNamespace(repos=hub.repos)

    def snapshot_download(repo_id):
        hub.downloads.append(repo_id)
        return f"/hf-cache/{repo_id}/snapshots/abc"

    hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return hub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_format_size():
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(5 * 1024**2) == "5.0 MB"
    assert format_size(int(2.4 * 1024**3)) == "2.4 GB"


def test_list_cached_models_sorted_and_filtered(fake_hub):
    fake_hub.repos = [
        _repo("small/model", size=10),
        _repo("big/model", size=1000),
        _repo("some/dataset", size=5000, repo_type="dataset"),
    ]
    models = list_cached_models()
    assert [m.repo_id for m in models] == ["big/model", "small/model"]


def test_list_cached_models_no_cache_dir(fake_hub):
    def boom():
        raise FileNotFoundError("no cache")

    fake_hub.scan_cache_dir = boom
    assert list_cached_models() == []


def test_get_cached_model(fake_hub):
    fake_hub.repos = [_repo(DEFAULT_CHECKPOINT, size=2_400_000_000, nb_files=13)]
    cached = get_cached_model(DEFAULT_CHECKPOINT)
    assert cached is not None
    assert cached.nb_files == 13
    assert get_cached_model("missing/model") is None


def test_download_model(fake_hub):
    path = download_model()
    assert fake_hub.downloads == [DEFAULT_CHECKPOINT]
    assert path.startswith(f"/hf-cache/{DEFAULT_CHECKPOINT}")


def test_hub_available_false(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert hub_available() is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_model_status_cached(fake_hub):
    fake_hub.repos = [_repo(DEFAULT_CHECKPOINT, size=2_400_000_000, nb_files=13)]
    result = runner.invoke(app, ["model", "status"])
    assert result.exit_code == 0
    assert "cached" in result.output
    assert "2.2 GB" in result.output


def test_cli_model_status_not_downloaded(fake_hub):
    result = runner.invoke(app, ["model", "status"])
    assert result.exit_code == 0
    assert "not downloaded" in _flat(result.output)
    assert "model download" in _flat(result.output)


def test_cli_model_download(fake_hub):
    result = runner.invoke(app, ["model", "download"])
    assert result.exit_code == 0
    assert fake_hub.downloads == [DEFAULT_CHECKPOINT]
    assert "Model ready" in result.output


def test_cli_model_download_custom_checkpoint(fake_hub):
    result = runner.invoke(app, ["model", "download", "--checkpoint", "other/model"])
    assert result.exit_code == 0
    assert fake_hub.downloads == ["other/model"]


def test_cli_model_list(fake_hub):
    fake_hub.repos = [_repo("a/model", size=1024**3, nb_files=5)]
    result = runner.invoke(app, ["model", "list"])
    assert result.exit_code == 0
    assert "a/model" in result.output
    assert "1.0 GB" in result.output


def test_cli_model_list_empty(fake_hub):
    result = runner.invoke(app, ["model", "list"])
    assert result.exit_code == 0
    assert "No models" in result.output


def test_modelcache_imports_first_in_fresh_interpreter():
    """Regression: importing modelcache before
    anything else must not blow up on a circular import."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import agent_ptt.modelcache; "
            "from agent_ptt.tts import has_backend; "
            "print(has_backend('pocket-tts'))",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_cli_commands_fail_cleanly_without_extra(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    for cmd in (["model", "download"], ["model", "list"], ["model", "status"]):
        result = runner.invoke(app, cmd)
        assert result.exit_code == 1
        assert "uv sync" in _flat(result.output)
