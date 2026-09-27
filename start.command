#!/bin/bash
# Double-click in Finder, or run ./start.command [server options].
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Finder's Terminal session may not include uv's default install directory.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

exec uv run agent-ptt server start "$@"
