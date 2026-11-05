#!/usr/bin/env python3
"""Validate plugin manifests and the marketplace registry.

Run locally or in CI: python3 scripts/validate_plugins.py
Exits 1 with a list of problems, 0 with a summary when everything checks out.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"


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

    # All hooks.json files (and templates) parse
    for hooks_path in sorted(PLUGINS_DIR.glob("*/hooks/hooks.json")) + sorted(
        PLUGINS_DIR.glob("*/hooks.json.template")
    ):
        config = _load_json(hooks_path, problems)
        if config is not None and "hooks" not in config:
            problems.append(f"{hooks_path.relative_to(REPO_ROOT)}: missing top-level 'hooks' key")

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
    skill_count = len(list(PLUGINS_DIR.glob("*/skills/*/SKILL.md")))
    print(f"✅ plugins OK: {plugin_count} manifests, {skill_count} skills, marketplace consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
