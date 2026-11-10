"""The release version-lock checker used by the release workflow."""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CHECK_PY = REPO_ROOT / "scripts" / "check_plugin_versions.py"

spec = importlib.util.spec_from_file_location("check_plugin_versions", CHECK_PY)
check_versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_versions)


def _plugin(tmp_path, name, version, kind=".claude-plugin"):
    manifest_dir = tmp_path / "plugins" / name / kind
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(json.dumps({"name": name, "version": version}))


def test_current_repo_versions_are_release_ready():
    """Every shipped plugin.json must agree on one version so a matching
    tag can always be cut."""
    versions = {
        json.loads(p.read_text())["version"]
        for pattern in ("*/.claude-plugin/plugin.json", "*/.codex-plugin/plugin.json")
        for p in (REPO_ROOT / "plugins").glob(pattern)
    }
    assert len(versions) == 1, f"plugin versions diverge: {versions}"
    assert check_versions.check(f"v{versions.pop()}") == []


def test_matching_versions_pass(tmp_path, monkeypatch):
    _plugin(tmp_path, "a", "1.2.3")
    _plugin(tmp_path, "b", "1.2.3")
    monkeypatch.setattr(check_versions, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_versions, "PLUGINS_DIR", tmp_path / "plugins")
    assert check_versions.check("v1.2.3") == []


def test_mismatched_version_fails(tmp_path, monkeypatch):
    _plugin(tmp_path, "a", "1.2.3")
    _plugin(tmp_path, "b", "1.0.0")
    monkeypatch.setattr(check_versions, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_versions, "PLUGINS_DIR", tmp_path / "plugins")
    problems = check_versions.check("v1.2.3")
    assert len(problems) == 1
    assert "'1.0.0' != tag '1.2.3'" in problems[0]


def test_codex_manifest_must_match_the_tag(tmp_path, monkeypatch):
    _plugin(tmp_path, "a", "1.2.3")
    _plugin(tmp_path, "codex-a", "1.0.0", kind=".codex-plugin")
    monkeypatch.setattr(check_versions, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_versions, "PLUGINS_DIR", tmp_path / "plugins")
    problems = check_versions.check("v1.2.3")
    assert len(problems) == 1
    assert ".codex-plugin" in problems[0]


def test_bad_tag_format_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(check_versions, "PLUGINS_DIR", tmp_path / "plugins")
    assert check_versions.check("1.2.3") == ["tag '1.2.3' is not of the form vX.Y.Z"]
    assert check_versions.check("va.b.c") == ["tag 'va.b.c' is not of the form vX.Y.Z"]


def test_no_plugins_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(check_versions, "PLUGINS_DIR", tmp_path / "plugins")
    assert check_versions.check("v1.0.0") == ["no plugin manifests found under plugins/"]
