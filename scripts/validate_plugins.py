#!/usr/bin/env python3
"""Validate plugin manifests and the Claude Code, Codex and Cursor marketplace registries.

Run locally or in CI: python3 scripts/validate_plugins.py
Exits 1 with a list of problems, 0 with a summary when everything checks out.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CODEX_MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
CURSOR_MARKETPLACE = REPO_ROOT / ".cursor-plugin" / "marketplace.json"
SHARED_HOOKS = PLUGINS_DIR / "announcer" / "hooks"  # the source of every plugin's shared .py copies
SHARED_COPIES = ("codex-announcer", "antigravity-announcer", "cursor-announcer")
CURSOR_HOOK_SCRIPT = re.compile(r"\$\{CURSOR_PLUGIN_ROOT\}/([^\"\s]+\.py)")
KEBAB = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
# A script a hook command runs: "${PLUGIN_ROOT}/x.py" (Codex) or "${CLAUDE_PLUGIN_ROOT}"/hooks/x.py
HOOK_SCRIPT = re.compile(r'\$\{(?:CLAUDE_)?PLUGIN_ROOT\}"?/([^"\s]+)')


def _load_json(path: Path, problems: list[str]) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        problems.append(f"missing file: {path.relative_to(REPO_ROOT)}")
    except json.JSONDecodeError as e:
        problems.append(f"invalid JSON in {path.relative_to(REPO_ROOT)}: {e}")
    return None


def validate() -> list[str]:
    problems: list[str] = []

    # Every Claude plugin manifest parses and has name + version
    manifests: dict[str, dict] = {}
    for manifest_path in sorted(PLUGINS_DIR.glob("*/.claude-plugin/plugin.json")):
        manifest = _load_json(manifest_path, problems)
        if manifest is None:
            continue
        rel = manifest_path.relative_to(REPO_ROOT)
        if not manifest.get("name"):
            problems.append(f"{rel}: missing required field 'name'")
            continue
        if not manifest.get("version"):
            problems.append(f"{rel}: missing 'version' (required for the release flow)")
        manifests[manifest["name"]] = manifest

    # Marketplace entries parse and point at real plugin dirs with matching names
    marketplace = _load_json(MARKETPLACE, problems)
    if marketplace is not None:
        for entry in marketplace.get("plugins", []):
            name = entry.get("name", "<unnamed>")
            source = entry.get("source", {})
            path = source.get("path") if isinstance(source, dict) else source
            if not path:
                problems.append(f"marketplace entry '{name}': missing source path")
                continue
            plugin_dir = REPO_ROOT / path
            if not plugin_dir.is_dir():
                problems.append(f"marketplace entry '{name}': {path} does not exist")
                continue
            if name not in manifests:
                problems.append(
                    f"marketplace entry '{name}': no plugin.json with that name under plugins/"
                )

        registered = {e.get("name") for e in marketplace.get("plugins", [])}
        for name in manifests:
            if name not in registered:
                problems.append(f"plugin '{name}' is not registered in marketplace.json")

    # Codex marketplace: entries point at plugin dirs with a matching .codex-plugin manifest,
    # versioned in lockstep with the Claude plugins
    claude_versions = {m.get("version") for m in manifests.values() if m.get("version")}
    codex_marketplace = _load_json(CODEX_MARKETPLACE, problems)
    if codex_marketplace is not None:
        if not codex_marketplace.get("plugins"):
            problems.append("codex marketplace.json: no plugins listed")
        for entry in codex_marketplace.get("plugins", []):
            name = entry.get("name", "<unnamed>")
            source = entry.get("source", {})
            path = source.get("path") if isinstance(source, dict) else source
            if not path:
                problems.append(f"codex marketplace entry '{name}': missing source path")
                continue
            plugin_dir = REPO_ROOT / path
            if not plugin_dir.is_dir():
                problems.append(f"codex marketplace entry '{name}': {path} does not exist")
                continue
            manifest_path = plugin_dir / ".codex-plugin" / "plugin.json"
            manifest = _load_json(manifest_path, problems)
            if manifest is None:
                continue
            rel = manifest_path.relative_to(REPO_ROOT)
            if manifest.get("name") != name:
                problems.append(
                    f"{rel}: name {manifest.get('name')!r} != marketplace entry {name!r}"
                )
            version = manifest.get("version")
            if not version:
                problems.append(f"{rel}: missing 'version'")
            elif claude_versions and claude_versions != {version}:
                problems.append(
                    f"{rel}: version {version!r} must match every .claude-plugin version "
                    f"({', '.join(sorted(claude_versions))})"
                )
    if len(claude_versions) > 1:
        problems.append(
            f"plugin versions differ ({', '.join(sorted(claude_versions))}); bump them together"
        )

    # All hooks.json files parse, and every script a hook runs ships in the plugin dir
    for hooks_path in sorted(PLUGINS_DIR.glob("*/hooks/hooks.json")):
        rel = hooks_path.relative_to(REPO_ROOT)
        config = _load_json(hooks_path, problems)
        if config is None:
            continue
        if "hooks" not in config:
            problems.append(f"{rel}: missing top-level 'hooks' key")
            continue
        plugin_dir = hooks_path.parent.parent
        for groups in config["hooks"].values():
            for group in groups:
                for hook in group.get("hooks", []):
                    for script in HOOK_SCRIPT.findall(hook.get("command", "")):
                        target = (plugin_dir / script).resolve()
                        if not target.is_relative_to(plugin_dir.resolve()):
                            problems.append(f"{rel}: {script} points outside the plugin dir")
                        elif not target.is_file():
                            problems.append(f"{rel}: hook script {script} does not exist")

    # Antigravity plugins: a root plugin.json + a root hooks.json of named hook groups whose
    # commands run from the plugin folder ("python3 ./x.py"), versioned with the others
    for manifest_path in sorted(PLUGINS_DIR.glob("*/plugin.json")):
        plugin_dir = manifest_path.parent
        rel = manifest_path.relative_to(REPO_ROOT)
        manifest = _load_json(manifest_path, problems)
        if manifest is None:
            continue
        if not manifest.get("name"):
            problems.append(f"{rel}: missing 'name'")
        version = manifest.get("version")
        if claude_versions and claude_versions != {version}:
            problems.append(
                f"{rel}: version {version!r} must match every .claude-plugin version "
                f"({', '.join(sorted(claude_versions))})"
            )
        hooks_path = plugin_dir / "hooks.json"
        hooks = _load_json(hooks_path, problems) if hooks_path.exists() else {}
        for group in (hooks or {}).values():
            if not isinstance(group, dict):
                problems.append(f"{hooks_path.relative_to(REPO_ROOT)}: hook groups are objects")
                continue
            for event, handlers in group.items():
                if event == "enabled":
                    continue
                flat = []
                for handler in handlers:
                    flat += handler.get("hooks", [handler]) if "matcher" in handler else [handler]
                for handler in flat:
                    for script in re.findall(r"\./([^\s\"]+\.py)", handler.get("command", "")):
                        if not (plugin_dir / script).is_file():
                            where = hooks_path.relative_to(REPO_ROOT)
                            problems.append(f"{where}: {event} runs missing {script}")

    problems += validate_cursor(claude_versions)
    problems += validate_shared_copies()
    problems += validate_mcp()

    client = REPO_ROOT / "agent_ptt/skills/agent-ptt-organization/scripts/client.py"
    bundled = PLUGINS_DIR / "voice/scripts/organization_client.py"
    if not bundled.exists() or bundled.read_bytes() != client.read_bytes():
        problems.append("voice organization_client.py must match the published organization client")

    # Skills have parseable frontmatter with a description
    for skill_path in sorted(PLUGINS_DIR.glob("*/skills/*/SKILL.md")):
        rel = skill_path.relative_to(REPO_ROOT)
        text = skill_path.read_text()
        if not text.startswith("---\n") or text.count("---\n") < 2:
            problems.append(f"{rel}: missing frontmatter block")
        elif "description:" not in text.split("---\n")[1]:
            problems.append(f"{rel}: frontmatter missing 'description'")

    return problems


def validate_cursor(claude_versions: set) -> list[str]:
    """Cursor plugins: .cursor-plugin/plugin.json (kebab-case name, versioned with the
    others), hooks/hooks.json ({"version": 1, "hooks": {event: [{command}]}}) whose
    scripts exist, rules/*.mdc with frontmatter, and the repo's Cursor marketplace."""
    problems: list[str] = []
    names: dict[str, Path] = {}
    for manifest_path in sorted(PLUGINS_DIR.glob("*/.cursor-plugin/plugin.json")):
        plugin_dir = manifest_path.parent.parent
        rel = manifest_path.relative_to(REPO_ROOT)
        manifest = _load_json(manifest_path, problems)
        if manifest is None:
            continue
        name = manifest.get("name") or ""
        if not KEBAB.match(name):
            problems.append(f"{rel}: 'name' must be kebab-case, got {name!r}")
        names[name] = plugin_dir
        version = manifest.get("version")
        if claude_versions and claude_versions != {version}:
            problems.append(
                f"{rel}: version {version!r} must match every .claude-plugin version "
                f"({', '.join(sorted(claude_versions))})"
            )
        hooks_path = plugin_dir / "hooks" / "hooks.json"
        hooks_rel = hooks_path.relative_to(REPO_ROOT)
        config = _load_json(hooks_path, problems) if hooks_path.exists() else None
        if config is not None:
            if config.get("version") != 1 or not isinstance(config.get("hooks"), dict):
                problems.append(f'{hooks_rel}: needs "version": 1 and a "hooks" object')
                config = {"hooks": {}}
            for event, handlers in config["hooks"].items():
                for handler in handlers if isinstance(handlers, list) else []:
                    command = str(handler.get("command", ""))
                    for script in CURSOR_HOOK_SCRIPT.findall(command):
                        if not (plugin_dir / script).is_file():
                            problems.append(f"{hooks_rel}: {event} runs missing {script}")
        for rule in sorted((plugin_dir / "rules").glob("*.mdc")):
            text = rule.read_text()
            head = text.split("---\n")[1] if text.startswith("---\n") else ""
            if "description:" not in head and "alwaysApply:" not in head:
                problems.append(f"{rule.relative_to(REPO_ROOT)}: missing rule frontmatter")
    if names:
        marketplace = _load_json(CURSOR_MARKETPLACE, problems)
        listed = set()
        for entry in (marketplace or {}).get("plugins", []):
            name, source = entry.get("name"), entry.get("source")
            listed.add(name)
            path = source if isinstance(source, str) else None
            if not path or not (REPO_ROOT / path / ".cursor-plugin" / "plugin.json").is_file():
                problems.append(f"cursor marketplace entry {name!r}: no Cursor plugin at {path}")
            elif name not in names or (REPO_ROOT / path).resolve() != names[name].resolve():
                problems.append(f"cursor marketplace entry {name!r}: name differs from {path}")
        for name in names:
            if name not in listed:
                problems.append(f"Cursor plugin {name!r} is not in .cursor-plugin/marketplace.json")
    return problems


def validate_shared_copies() -> list[str]:
    """The Codex, Antigravity and Cursor plugins carry identical copies of the shared
    hook scripts in plugins/announcer/hooks/ (sync them with cp after a change)."""
    problems = []
    for name in SHARED_COPIES:
        plugin_dir = PLUGINS_DIR / name
        if not plugin_dir.is_dir():
            continue
        for script in sorted(SHARED_HOOKS.glob("*.py")):
            copy = plugin_dir / script.name
            if not copy.is_file() or copy.read_bytes() != script.read_bytes():
                problems.append(
                    f"plugins/{name}/{script.name} differs from {script.name} in hooks/"
                )
    return problems


# Plugin MCP configs: Claude Code's root .mcp.json, Antigravity's mcp_config.json, Cursor's
# mcp.json and the file a Codex manifest's "mcpServers" names. Each runs ptt_mcp.py.
MCP_ROOT_VARS = re.compile(r"^\$\{(?:CLAUDE_|CURSOR_)?PLUGIN_ROOT\}/")
MCP_SERVER = "agent-ptt"
# Cursor's own server has its own name: its Claude-plugin import would shadow "agent-ptt".
MCP_SERVER_NAMES = {"cursor-announcer": "agent-ptt-cursor"}
MCP_SIBLINGS = ("ptt.py", "project_link.py", "session_start.py", "session_routing.py")


def _mcp_configs() -> list[Path]:
    configs = (
        sorted(PLUGINS_DIR.glob("*/.mcp.json"))
        + sorted(PLUGINS_DIR.glob("*/mcp_config.json"))
        + sorted(PLUGINS_DIR.glob("*/mcp.json"))
    )
    for manifest in sorted(PLUGINS_DIR.glob("*/.codex-plugin/plugin.json")):
        try:
            named = json.loads(manifest.read_text()).get("mcpServers")
        except (OSError, ValueError):
            continue  # reported by the manifest checks
        if isinstance(named, str):
            configs.append(manifest.parent.parent / named)
    return list(dict.fromkeys(p.resolve() for p in configs))


def validate_mcp() -> list[str]:
    problems: list[str] = []
    reference = PLUGINS_DIR / "announcer" / "hooks" / "ptt_mcp.py"
    for path in _mcp_configs():
        rel = path.relative_to(REPO_ROOT)
        config = _load_json(path, problems)
        if config is None:
            continue
        servers = config.get("mcpServers")
        plugin_dir = path.parent
        name = MCP_SERVER_NAMES.get(plugin_dir.name, MCP_SERVER)
        if not isinstance(servers, dict) or name not in servers:
            problems.append(f"{rel}: no mcpServers.{name}")
            continue
        server = servers[name]
        scripts = [a for a in server.get("args", []) if str(a).endswith(".py")]
        if len(scripts) != 1:
            problems.append(f"{rel}: {name} must run one .py script")
            continue
        script = MCP_ROOT_VARS.sub("", scripts[0])
        if script == scripts[0]:  # relative: Codex joins a relative cwd to the plugin root
            if server.get("cwd") != ".":
                problems.append(f'{rel}: a relative script needs "cwd": "."')
            script = script.removeprefix("./")
        target = (plugin_dir / script).resolve()
        if not target.is_relative_to(plugin_dir.resolve()):
            problems.append(f"{rel}: {script} points outside the plugin dir")
        elif not target.is_file():
            problems.append(f"{rel}: MCP script {script} does not exist")
        elif target.read_bytes() != reference.read_bytes():
            problems.append(f"{rel}: {script} differs from {reference.relative_to(REPO_ROOT)}")
        else:
            for sibling in MCP_SIBLINGS:
                if not (target.parent / sibling).is_file():
                    problems.append(f"{rel}: {script} needs {sibling} next to it")
    return problems


def main() -> int:
    problems = validate()
    if problems:
        print("Plugin validation failed:", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return 1

    plugin_count = len(list(PLUGINS_DIR.glob("*/.claude-plugin/plugin.json")))
    codex_count = len(list(PLUGINS_DIR.glob("*/.codex-plugin/plugin.json")))
    agy_count = len(list(PLUGINS_DIR.glob("*/plugin.json")))
    cursor_count = len(list(PLUGINS_DIR.glob("*/.cursor-plugin/plugin.json")))
    skill_count = len(list(PLUGINS_DIR.glob("*/skills/*/SKILL.md")))
    print(
        f"✅ plugins OK: {plugin_count} Claude + {codex_count} Codex + {agy_count} Antigravity"
        f" + {cursor_count} Cursor manifests, "
        f"{skill_count} skills, marketplaces consistent"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
