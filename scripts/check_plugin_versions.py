#!/usr/bin/env python3
"""Verify every plugin version matches the release tag.

Usage: python3 scripts/check_plugin_versions.py v0.2.0

Plugins are version-locked to the repo tag (see plugins/README.md):
tagging vX.Y.Z requires every plugins/*/.claude-plugin/plugin.json and
plugins/*/.codex-plugin/plugin.json to declare "version": "X.Y.Z".
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"


def check(tag: str) -> list[str]:
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag)
    if not match:
        return [f"tag '{tag}' is not of the form vX.Y.Z"]
    expected = match.group(1)

    problems = []
    manifest_paths = sorted(PLUGINS_DIR.glob("*/.claude-plugin/plugin.json")) + sorted(
        PLUGINS_DIR.glob("*/.codex-plugin/plugin.json")
    )
    if not manifest_paths:
        problems.append("no plugin manifests found under plugins/")
    for manifest_path in manifest_paths:
        version = json.loads(manifest_path.read_text()).get("version")
        if version != expected:
            problems.append(
                f"{manifest_path.relative_to(REPO_ROOT)}: version {version!r} != tag {expected!r}"
            )
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_plugin_versions.py <tag>", file=sys.stderr)
        return 2

    problems = check(sys.argv[1])
    if problems:
        print("Version check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        print("\nBump the version field in each plugin.json to match the tag.", file=sys.stderr)
        return 1

    print(f"✅ all plugin versions match {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
