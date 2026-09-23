"""Tests for scripts/install_plugins.py — always installs from the main checkout."""

import importlib.util
import json
import subprocess
from pathlib import Path

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


def test_install_codex_writes_fresh_hooks_json(tmp_path):
    target = installer.install_codex(REPO_ROOT, tmp_path / ".codex")

    config = json.loads(target.read_text())
    command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert str(REPO_ROOT / "plugins" / "codex-announcer" / "announce.py") in command
    assert set(config["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}


def test_install_codex_replaces_stale_paths_and_keeps_other_hooks(tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    stale = "python3 /gone/copilot-worktree/plugins/codex-announcer/announce.py"
    other = {"hooks": [{"type": "command", "command": "bash ~/.codex/herdr.sh session"}]}
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [other],
                    "Stop": [{"hooks": [{"type": "command", "command": stale}]}],
                }
            }
        )
    )

    config = json.loads(installer.install_codex(REPO_ROOT, codex_home).read_text())

    assert config["hooks"]["SessionStart"] == [other]
    assert "/gone/" not in json.dumps(config)
    assert len(config["hooks"]["Stop"]) == 1  # replaced, not duplicated

    # Idempotent: a second run changes nothing
    again = json.loads(installer.install_codex(REPO_ROOT, codex_home).read_text())
    assert again == config
