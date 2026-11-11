#!/usr/bin/env python3
"""Validate plugin manifests and the Claude Code and Codex marketplace registries.

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
    skill_count = len(list(PLUGINS_DIR.glob("*/skills/*/SKILL.md")))
    print(
        f"✅ plugins OK: {plugin_count} Claude + {codex_count} Codex + {agy_count} Antigravity"
        " manifests, "
        f"{skill_count} skills, marketplaces consistent"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
