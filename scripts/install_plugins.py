#!/usr/bin/env python3
"""Install the Agent PTT plugins for Claude Code and Codex from the main checkout.

Always resolves the repository's *main* worktree, even when run from a linked
worktree (e.g. one created by the Copilot app), so neither CLI ends up pointed
at a throwaway checkout that later disappears. Safe to re-run: that is also how
you refresh Claude Code's cached plugin copy after changing a plugin.

    python3 scripts/install_plugins.py              # Claude Code + Codex
    python3 scripts/install_plugins.py --skip-codex

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
CODEX_SCRIPT_MARKER = "codex-announcer/announce.py"
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


def render_codex_hooks(root: Path) -> dict:
    plugin_dir = root / "plugins" / "codex-announcer"
    template = (plugin_dir / "hooks.json.template").read_text()
    return json.loads(template.replace("__AGENT_PTT_PLUGIN_DIR__", str(plugin_dir)))


def _is_announcer_entry(entry: dict) -> bool:
    return any(CODEX_SCRIPT_MARKER in hook.get("command", "") for hook in entry.get("hooks", []))


def merge_codex_hooks(existing: dict, rendered: dict) -> dict:
    """Replace any announcer hook entries (wherever they pointed) and keep all others."""
    merged = json.loads(json.dumps(existing))  # deep copy
    hooks = merged.setdefault("hooks", {})
    for event, entries in hooks.items():
        hooks[event] = [entry for entry in entries if not _is_announcer_entry(entry)]
    for event, entries in rendered["hooks"].items():
        hooks.setdefault(event, []).extend(entries)
    merged["hooks"] = {event: entries for event, entries in hooks.items() if entries}
    return merged


def install_codex(root: Path, codex_home: Path) -> Path:
    target = codex_home / "hooks.json"
    existing = json.loads(target.read_text()) if target.exists() else {"hooks": {}}
    merged = merge_codex_hooks(existing, render_codex_hooks(root))
    codex_home.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, indent=2) + "\n")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-claude", action="store_true", help="don't touch Claude Code")
    parser.add_argument("--skip-codex", action="store_true", help="don't touch Codex")
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
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        target = install_codex(root, codex_home)
        print(f"✅ Codex: announcer hooks in {target} now run from {root}")
        print("   Codex asks you to re-trust changed hooks the next time it starts.")

    print("Restart any running Claude Code / Codex sessions to pick up the plugins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
