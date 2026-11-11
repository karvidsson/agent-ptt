#!/usr/bin/env python3
"""Install the Agent PTT plugins (Claude Code, Codex, Antigravity, Cursor) from the main checkout.

Claude Code gets the `.claude-plugin` marketplace; Codex gets the
`.agents/plugins` marketplace and the agent-ptt-announcer plugin, and any loose
announcer entries left in ~/.codex/hooks.json by the old install.sh are removed
so hooks never fire twice. Antigravity (`agy`) gets plugins/antigravity-announcer
copied into ~/.gemini/config/plugins/ (`agy plugin install <dir>`). Cursor
(`agent` / `cursor-agent`) gets plugins/cursor-announcer copied into
~/.cursor/plugins/local/agent-ptt-announcer/, plus two no-op entries in
~/.cursor/hooks.json without which the agent CLI never runs a plugin's
beforeSubmitPrompt and stop hooks.

Always resolves the repository's *main* worktree, even when run from a linked
worktree (e.g. one created by the Copilot app), so neither CLI ends up pointed
at a throwaway checkout that later disappears. Safe to re-run: that is also how
you refresh Claude Code's and Codex's cached plugin copies after changing a plugin.

    python3 scripts/install_plugins.py              # Claude Code + Codex + Antigravity + Cursor
    python3 scripts/install_plugins.py --skip-codex --skip-antigravity --skip-cursor

Stdlib-only, like the plugins themselves.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MARKETPLACE = "agent-ptt"
CODEX_PLUGIN = "agent-ptt-announcer"
# Loose ~/.codex/hooks.json entries from the pre-plugin install.sh all name a
# script under plugins/codex-announcer/.
CODEX_SCRIPT_MARKER = "codex-announcer/"
ANTIGRAVITY_PLUGIN_DIR = "plugins/antigravity-announcer"
CURSOR_PLUGIN_DIR = "plugins/cursor-announcer"
CURSOR_PLUGIN = "agent-ptt-announcer"
# The agent CLI (2026.11.11) runs a plugin's beforeSubmitPrompt and stop hooks only when a
# non-plugin hooks.json also lists the event, so the install adds a no-op for each.
CURSOR_NOOP_MARKER = "agent-ptt: lets plugin hooks run"
CURSOR_NOOP_EVENTS = ("beforeSubmitPrompt", "stop")
HERE = Path(__file__).resolve().parent


def main_checkout(start: Path = HERE) -> Path:
    """The main worktree of the repo containing `start` (first in `git worktree list`)."""
    result = subprocess.run(
        ["git", "-C", str(start), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree "))
    raise RuntimeError(f"not inside a git repository: {start}")


def plugin_names(root: Path) -> list[str]:
    marketplace = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    return [plugin["name"] for plugin in marketplace["plugins"]]


def install_claude(root: Path, run=subprocess.run) -> None:
    """Re-register the marketplace at `root` and reinstall every plugin.

    Claude Code caches plugins by version, so a reinstall is the only way to
    pick up changes without bumping versions.
    """
    names = plugin_names(root)
    quiet = {"capture_output": True, "text": True, "check": False}
    for name in names:
        run(["claude", "plugin", "uninstall", f"{name}@{MARKETPLACE}"], **quiet)
    run(["claude", "plugin", "marketplace", "remove", MARKETPLACE], **quiet)
    run(["claude", "plugin", "marketplace", "add", str(root)], check=True)
    for name in names:
        run(["claude", "plugin", "install", f"{name}@{MARKETPLACE}"], check=True)


def _is_announcer_entry(entry: dict) -> bool:
    return any(CODEX_SCRIPT_MARKER in hook.get("command", "") for hook in entry.get("hooks", []))


def strip_loose_codex_hooks(existing: dict) -> tuple[dict, int]:
    """Drop the announcer entries the old install.sh/template put in hooks.json.

    The plugin now ships those hooks; leaving the loose copies would make every
    hook fire twice. All other entries (herdr's, the user's own) are kept.
    Returns the cleaned config and how many entries were removed.
    """
    cleaned = json.loads(json.dumps(existing))  # deep copy
    hooks = cleaned.get("hooks", {})
    removed = 0
    for event, entries in list(hooks.items()):
        kept = [entry for entry in entries if not _is_announcer_entry(entry)]
        removed += len(entries) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return cleaned, removed


def migrate_codex_hooks(codex_home: Path) -> int:
    """Remove loose announcer entries from CODEX_HOME/hooks.json; returns the count."""
    target = codex_home / "hooks.json"
    if not target.exists():
        return 0
    cleaned, removed = strip_loose_codex_hooks(json.loads(target.read_text()))
    if removed:
        target.write_text(json.dumps(cleaned, indent=2) + "\n")
    return removed


def install_codex(root: Path, codex_home: Path, run=subprocess.run) -> int:
    """Register the repo's Codex marketplace at `root` and (re)install the announcer.

    Removing and re-adding the plugin refreshes Codex's cached copy, the same
    way install_claude does for Claude Code. Returns how many loose announcer
    entries were stripped from hooks.json.
    """
    quiet = {"capture_output": True, "text": True, "check": False}
    run(["codex", "plugin", "remove", f"{CODEX_PLUGIN}@{MARKETPLACE}"], **quiet)
    run(["codex", "plugin", "marketplace", "remove", MARKETPLACE], **quiet)
    run(["codex", "plugin", "marketplace", "add", str(root)], check=True)
    run(["codex", "plugin", "add", f"{CODEX_PLUGIN}@{MARKETPLACE}"], check=True)
    return migrate_codex_hooks(codex_home)


def install_antigravity(root: Path, run=subprocess.run) -> None:
    """Copy the Antigravity plugin into agy's plugin folder (replaces an older copy).

    agy installs a copy, not a link: re-run after every change, then restart agy.
    """
    run(["agy", "plugin", "install", str(root / ANTIGRAVITY_PLUGIN_DIR)], check=True)
    # Grants in agy's settings.json skip its safety reviewer for ptt and the MCP tools,
    # which hook approval alone can't when a session's hooks didn't load.
    installed = Path.home() / ".gemini" / "config" / "plugins" / "agent-ptt-announcer" / "ptt.py"
    if installed.is_file():
        run([sys.executable, str(installed), "agy-rules", "--install"], check=False)


def cursor_noop_hooks(existing: dict) -> tuple[dict, int]:
    """Add the no-op beforeSubmitPrompt/stop entries to a Cursor hooks.json config.

    Keeps every other entry; adds ours only where missing. Returns the config and how
    many entries were added. ``exit 0`` does nothing in sh and PowerShell alike.
    """
    config = json.loads(json.dumps(existing or {}))  # deep copy
    config.setdefault("version", 1)
    hooks = config.setdefault("hooks", {})
    added = 0
    for event in CURSOR_NOOP_EVENTS:
        entries = hooks.setdefault(event, [])
        if not any(CURSOR_NOOP_MARKER in str(e.get("command", "")) for e in entries):
            entries.append({"command": f"exit 0 # {CURSOR_NOOP_MARKER}", "timeout": 5})
            added += 1
    return config, added


def install_cursor(root: Path, cursor_home: Path) -> int:
    """Copy the Cursor plugin into ~/.cursor/plugins/local/ (replacing an older copy)
    and add the no-op hooks.json entries; returns how many entries were added.

    A copy, not a symlink: Cursor only follows links that stay inside that folder.
    Re-run after every change, then restart Cursor (the IDE: Reload Window).
    """
    target = cursor_home / "plugins" / "local" / CURSOR_PLUGIN
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / CURSOR_PLUGIN_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    hooks_path = cursor_home / "hooks.json"
    existing = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
    config, added = cursor_noop_hooks(existing)
    if added:
        hooks_path.write_text(json.dumps(config, indent=2) + "\n")
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-claude", action="store_true", help="don't touch Claude Code")
    parser.add_argument("--skip-codex", action="store_true", help="don't touch Codex")
    parser.add_argument(
        "--skip-antigravity", action="store_true", help="don't touch Antigravity (agy)"
    )
    parser.add_argument("--skip-cursor", action="store_true", help="don't touch Cursor")
    args = parser.parse_args(argv)

    root = main_checkout()
    print(f"Installing Agent PTT plugins from the main checkout: {root}")
    if HERE.parent != root:
        print(f"  (run from linked worktree {HERE.parent} — using the main checkout instead)")

    if not args.skip_claude:
        if shutil.which("claude"):
            install_claude(root)
            print(f"✅ Claude Code: {', '.join(plugin_names(root))} installed from {root}")
        else:
            print("⏭️  Claude Code: `claude` not on PATH, skipped")

    if not args.skip_codex:
        if shutil.which("codex"):
            codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            removed = install_codex(root, codex_home)
            print(f"✅ Codex: {CODEX_PLUGIN}@{MARKETPLACE} installed from {root}")
            if removed:
                print(
                    f"   Removed {removed} loose announcer entries from "
                    f"{codex_home / 'hooks.json'} (the plugin replaces them)"
                )
            print(
                "   Codex asks you to trust the plugin's hooks the next time it starts "
                "(Hooks need review → Trust all), and again after each upgrade."
            )
        else:
            print("⏭️  Codex: `codex` not on PATH, skipped")

    if not args.skip_antigravity:
        if shutil.which("agy"):
            install_antigravity(root)
            print(f"✅ Antigravity: agent-ptt-announcer installed from {root}")
        else:
            print("⏭️  Antigravity: `agy` not on PATH, skipped")

    if not args.skip_cursor:
        if shutil.which("agent") or shutil.which("cursor-agent"):
            cursor_home = Path.home() / ".cursor"
            added = install_cursor(root, cursor_home)
            plugin = cursor_home / "plugins" / "local" / CURSOR_PLUGIN
            print(f"✅ Cursor: {CURSOR_PLUGIN} copied to {plugin}")
            if added:
                print(f"   Added {added} no-op entries to {cursor_home / 'hooks.json'}")
            print(
                "   The agent CLI may not load ~/.cursor/plugins/local: start it with"
                f" `agent --plugin-dir {plugin}`. For no prompt per ptt call, see"
                f" `python3 {plugin / 'ptt.py'} cursor-rules`."
            )
        else:
            print("⏭️  Cursor: `agent` / `cursor-agent` not on PATH, skipped")

    print("Restart any running Claude Code / Codex / agy / Cursor sessions to pick up the plugins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
