#!/usr/bin/env bash
# Install the Agent PTT announcer hooks for Codex CLI.
#
# Renders hooks.json.template with this directory's absolute path and
# writes it to ~/.codex/hooks.json. Refuses to overwrite an existing
# hooks.json — prints the snippet for manual merging instead.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
TARGET="$CODEX_DIR/hooks.json"

RENDERED="$(sed "s|__AGENT_PTT_PLUGIN_DIR__|$PLUGIN_DIR|g" "$PLUGIN_DIR/hooks.json.template")"

if [ -f "$TARGET" ]; then
  echo "⚠️  $TARGET already exists — not overwriting."
  echo "Merge these hook entries into it manually:"
  echo
  echo "$RENDERED"
  exit 1
fi

mkdir -p "$CODEX_DIR"
printf '%s\n' "$RENDERED" > "$TARGET"
echo "✅ Installed Codex hooks to $TARGET"
echo "   Announcements go to your project's channel on ${AGENT_PTT_URL:-http://localhost:8770}"
echo "   Another machine's server: set AGENT_PTT_URL (and AGENT_PTT_API_KEY) in ~/.agent-ptt/announcer.env"
