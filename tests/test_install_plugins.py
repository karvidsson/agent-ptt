"""Tests for scripts/install_plugins.py — always installs from the main checkout."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install_plugins.py"
spec = importlib.util.spec_from_file_location("install_plugins", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_main_checkout_from_linked_worktree_is_the_main_one(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
        cwd=main,
    )
    linked = tmp_path / "copilot-worktree"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=main)

    assert installer.main_checkout(linked).resolve() == main.resolve()
    assert installer.main_checkout(main).resolve() == main.resolve()


def test_plugin_names_come_from_the_marketplace():
    assert installer.plugin_names(REPO_ROOT) == ["agent-ptt-announcer", "agent-ptt-voice"]


def test_install_claude_reregisters_marketplace_and_reinstalls(tmp_path):
    calls = []
    installer.install_claude(REPO_ROOT, run=lambda cmd, **kw: calls.append(cmd))

    assert calls == [
        ["claude", "plugin", "uninstall", "agent-ptt-announcer@agent-ptt"],
        ["claude", "plugin", "uninstall", "agent-ptt-voice@agent-ptt"],
        ["claude", "plugin", "marketplace", "remove", "agent-ptt"],
        ["claude", "plugin", "marketplace", "add", str(REPO_ROOT)],
        ["claude", "plugin", "install", "agent-ptt-announcer@agent-ptt"],
        ["claude", "plugin", "install", "agent-ptt-voice@agent-ptt"],
    ]


def test_install_codex_registers_marketplace_and_reinstalls(tmp_path):
    calls = []
    removed = installer.install_codex(
        REPO_ROOT, tmp_path / ".codex", run=lambda cmd, **kw: calls.append(cmd)
    )

    assert calls == [
        ["codex", "plugin", "remove", "agent-ptt-announcer@agent-ptt"],
        ["codex", "plugin", "marketplace", "remove", "agent-ptt"],
        ["codex", "plugin", "marketplace", "add", str(REPO_ROOT)],
        ["codex", "plugin", "add", "agent-ptt-announcer@agent-ptt"],
    ]
    assert removed == 0
    assert not (tmp_path / ".codex" / "hooks.json").exists()  # never created


def test_install_codex_strips_loose_announcer_hooks_and_keeps_others(tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    loose = (
        "AGENT_PTT_AGENT=Codex python3 /gone/copilot-worktree/plugins/codex-announcer/announce.py"
    )
    herdr = {"hooks": [{"type": "command", "command": "bash ~/.codex/herdr.sh session"}]}
    announcer = {"matcher": "", "hooks": [{"type": "command", "command": loose}]}
    original = {
        "hooks": {
            "SessionStart": [herdr, announcer],
            "Stop": [announcer],
            "SessionEnd": [announcer, announcer],
        },
        "other": True,
    }
    (codex_home / "hooks.json").write_text(json.dumps(original))

    removed = installer.install_codex(REPO_ROOT, codex_home, run=lambda cmd, **kw: None)

    config = json.loads((codex_home / "hooks.json").read_text())
    assert removed == 4
    assert config == {"hooks": {"SessionStart": [herdr]}, "other": True}

    # Idempotent: a second run removes nothing and leaves the file alone
    assert installer.install_codex(REPO_ROOT, codex_home, run=lambda cmd, **kw: None) == 0
    assert json.loads((codex_home / "hooks.json").read_text()) == config


def test_codex_marketplace_exposes_the_announcer_plugin():
    marketplace = json.loads((REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
    assert marketplace["name"] == installer.MARKETPLACE
    [entry] = marketplace["plugins"]
    assert entry["name"] == installer.CODEX_PLUGIN
    plugin_dir = REPO_ROOT / entry["source"]["path"]
    manifest = json.loads((plugin_dir / ".codex-plugin" / "plugin.json").read_text())
    assert manifest["name"] == installer.CODEX_PLUGIN


def test_main_skips_codex_when_not_on_path(monkeypatch, capsys):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer, "main_checkout", lambda: REPO_ROOT)
    monkeypatch.setattr(
        installer, "install_codex", lambda *a, **k: pytest.fail("must not run without codex")
    )

    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "Codex: `codex` not on PATH, skipped" in out


def test_install_antigravity_installs_the_plugin_folder(tmp_path):
    calls = []
    installer.install_antigravity(tmp_path, run=lambda cmd, **kw: calls.append(cmd))
    assert calls == [["agy", "plugin", "install", str(tmp_path / "plugins/antigravity-announcer")]]
    assert (REPO_ROOT / installer.ANTIGRAVITY_PLUGIN_DIR / "plugin.json").is_file()


def test_main_skips_antigravity_when_not_on_path(monkeypatch, capsys):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer, "main_checkout", lambda: REPO_ROOT)
    monkeypatch.setattr(
        installer, "install_antigravity", lambda *a, **k: pytest.fail("must not run without agy")
    )
    assert installer.main([]) == 0
    assert "Antigravity: `agy` not on PATH, skipped" in capsys.readouterr().out


def test_install_cursor_copies_the_plugin_and_adds_the_noop_hooks_once(tmp_path):
    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    mine = {"command": "./hooks/mine.sh"}
    (cursor_home / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [mine]}}))
    stale = cursor_home / "plugins" / "local" / "agent-ptt-announcer"
    stale.mkdir(parents=True)
    (stale / "old.py").write_text("x")
    assert installer.install_cursor(REPO_ROOT, cursor_home) == 2
    assert (stale / "cursor_hook.py").is_file() and not (stale / "old.py").exists()
    assert (stale / ".cursor-plugin" / "plugin.json").is_file()
    hooks = json.loads((cursor_home / "hooks.json").read_text())["hooks"]
    assert hooks["stop"][0] == mine  # the user's own entries stay
    assert set(hooks) == {"stop", "beforeSubmitPrompt"}
    assert all(installer.CURSOR_NOOP_MARKER in e["command"] for e in hooks["beforeSubmitPrompt"])
    assert installer.install_cursor(REPO_ROOT, cursor_home) == 0  # idempotent
    assert len(json.loads((cursor_home / "hooks.json").read_text())["hooks"]["stop"]) == 2


def test_main_skips_cursor_when_not_on_path(monkeypatch, capsys):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer, "main_checkout", lambda: REPO_ROOT)
    monkeypatch.setattr(
        installer, "install_cursor", lambda *a, **k: pytest.fail("must not run without agent")
    )
    assert installer.main([]) == 0
    assert "Cursor: `agent` / `cursor-agent` not on PATH, skipped" in capsys.readouterr().out
