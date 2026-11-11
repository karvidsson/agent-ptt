"""The plugin/marketplace validator used by CI."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
VALIDATE_PY = REPO_ROOT / "scripts" / "validate_plugins.py"

spec = importlib.util.spec_from_file_location("validate_plugins", VALIDATE_PY)
validate_plugins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate_plugins)


def test_repo_plugins_are_valid():
    """The shipped plugins must always validate — this is the CI gate."""
    assert validate_plugins.validate() == []


def test_cli_exit_code_and_summary():
    proc = subprocess.run(
        [sys.executable, str(VALIDATE_PY)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert "plugins OK" in proc.stdout


def test_detects_broken_manifest(tmp_path, monkeypatch):
    plugin = tmp_path / "plugins" / "broken" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"version": "1.0.0"}')  # no name
    marketplace = tmp_path / ".claude-plugin"
    marketplace.mkdir()
    (marketplace / "marketplace.json").write_text('{"plugins": []}')

    monkeypatch.setattr(validate_plugins, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(validate_plugins, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(validate_plugins, "MARKETPLACE", marketplace / "marketplace.json")

    problems = validate_plugins.validate()
    assert any("missing required field 'name'" in p for p in problems)


def test_detects_unregistered_plugin(tmp_path, monkeypatch):
    plugin = tmp_path / "plugins" / "orphan" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"name": "orphan", "version": "1.0.0"}')
    marketplace = tmp_path / ".claude-plugin"
    marketplace.mkdir()
    (marketplace / "marketplace.json").write_text('{"plugins": []}')

    monkeypatch.setattr(validate_plugins, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(validate_plugins, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(validate_plugins, "MARKETPLACE", marketplace / "marketplace.json")

    problems = validate_plugins.validate()
    assert any("not registered in marketplace.json" in p for p in problems)


def test_detects_dangling_marketplace_source(tmp_path, monkeypatch):
    (tmp_path / "plugins").mkdir()
    marketplace = tmp_path / ".claude-plugin"
    marketplace.mkdir()
    (marketplace / "marketplace.json").write_text(
        '{"plugins": [{"name": "ghost", "source": {"type": "path", "path": "./plugins/ghost"}}]}'
    )

    monkeypatch.setattr(validate_plugins, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(validate_plugins, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(validate_plugins, "MARKETPLACE", marketplace / "marketplace.json")

    problems = validate_plugins.validate()
    assert any("does not exist" in p for p in problems)


def _codex_tree(tmp_path, monkeypatch, *, codex_name="codex-thing", codex_version="1.0.0"):
    """A minimal repo: one Claude plugin and one Codex plugin with a hooks.json."""
    claude = tmp_path / "plugins" / "thing" / ".claude-plugin"
    claude.mkdir(parents=True)
    (claude / "plugin.json").write_text('{"name": "thing", "version": "1.0.0"}')
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text(
        '{"plugins": [{"name": "thing", "source": "./plugins/thing"}]}'
    )
    codex = tmp_path / "plugins" / "codex-thing"
    (codex / ".codex-plugin").mkdir(parents=True)
    (codex / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": codex_name, "version": codex_version})
    )
    (codex / "hooks").mkdir()
    (codex / "run.py").write_text("")
    (tmp_path / ".agents" / "plugins").mkdir(parents=True)
    (tmp_path / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "m",
                "plugins": [
                    {
                        "name": "codex-thing",
                        "source": {"source": "local", "path": "./plugins/codex-thing"},
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(validate_plugins, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(validate_plugins, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(
        validate_plugins, "MARKETPLACE", tmp_path / ".claude-plugin" / "marketplace.json"
    )
    monkeypatch.setattr(
        validate_plugins, "CODEX_MARKETPLACE", tmp_path / ".agents" / "plugins" / "marketplace.json"
    )
    return codex


def _codex_problems():
    return [p for p in validate_plugins.validate() if "organization_client" not in p]


def _write_hooks(codex, command):
    hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
    (codex / "hooks" / "hooks.json").write_text(json.dumps(hooks))


def test_valid_codex_plugin_passes(tmp_path, monkeypatch):
    codex = _codex_tree(tmp_path, monkeypatch)
    _write_hooks(codex, 'python3 "${PLUGIN_ROOT}/run.py"')
    assert _codex_problems() == []


def test_detects_codex_manifest_name_mismatch(tmp_path, monkeypatch):
    _codex_tree(tmp_path, monkeypatch, codex_name="other")
    assert any("!= marketplace entry 'codex-thing'" in p for p in _codex_problems())


def test_detects_codex_version_drift(tmp_path, monkeypatch):
    _codex_tree(tmp_path, monkeypatch, codex_version="0.9.0")
    assert any("must match every .claude-plugin version" in p for p in _codex_problems())


def test_detects_missing_codex_manifest(tmp_path, monkeypatch):
    codex = _codex_tree(tmp_path, monkeypatch)
    (codex / ".codex-plugin" / "plugin.json").unlink()
    assert any("missing file" in p and ".codex-plugin" in p for p in _codex_problems())


def test_detects_missing_and_escaping_hook_scripts(tmp_path, monkeypatch):
    codex = _codex_tree(tmp_path, monkeypatch)
    _write_hooks(codex, 'python3 "${PLUGIN_ROOT}/gone.py" "${PLUGIN_ROOT}/../outside.py"')
    problems = _codex_problems()
    assert any("hook script gone.py does not exist" in p for p in problems)
    assert any("../outside.py points outside the plugin dir" in p for p in problems)


def test_claude_style_hook_paths_are_checked_too(tmp_path, monkeypatch):
    _codex_tree(tmp_path, monkeypatch)
    hooks = tmp_path / "plugins" / "thing" / "hooks"
    hooks.mkdir()
    command = 'python3 "${CLAUDE_PLUGIN_ROOT}"/hooks/missing.py'
    config = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
    (hooks / "hooks.json").write_text(json.dumps(config))
    assert any("hooks/missing.py does not exist" in p for p in _codex_problems())


def test_mcp_configs_are_checked(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    hooks = plugins / "announcer" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("ptt_mcp.py", *validate_plugins.MCP_SIBLINGS):
        (hooks / name).write_text("# same\n")
    monkeypatch.setattr(validate_plugins, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(validate_plugins, "PLUGINS_DIR", plugins)

    def server(*args, **extra):
        return {"mcpServers": {"agent-ptt": {"command": "python3", "args": list(args), **extra}}}

    (plugins / "announcer" / ".mcp.json").write_text(
        json.dumps(server("${CLAUDE_PLUGIN_ROOT}/hooks/ptt_mcp.py"))
    )
    assert validate_plugins.validate_mcp() == []

    agy = plugins / "agy"
    agy.mkdir()
    (agy / "ptt_mcp.py").write_text("# drifted\n")
    (agy / "mcp_config.json").write_text(json.dumps(server("${PLUGIN_ROOT}/ptt_mcp.py")))
    codex = plugins / "codex"
    (codex / ".codex-plugin").mkdir(parents=True)
    (codex / ".codex-plugin" / "plugin.json").write_text('{"mcpServers": "./servers.json"}')
    (codex / "servers.json").write_text(json.dumps(server("./ptt_mcp.py")))
    (plugins / "bad").mkdir()
    (plugins / "bad" / ".mcp.json").write_text('{"mcpServers": {"other": {}}}')
    problems = validate_plugins.validate_mcp()
    assert any("agy/mcp_config.json: ptt_mcp.py differs" in p for p in problems)
    assert any('codex/servers.json: a relative script needs "cwd": "."' in p for p in problems)
    assert any("codex/servers.json: MCP script ptt_mcp.py does not exist" in p for p in problems)
    assert any("bad/.mcp.json: no mcpServers.agent-ptt" in p for p in problems)
