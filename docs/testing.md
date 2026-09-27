# Agent PTT — Testing Guide

## Automated Tests

```bash
uv sync                                  # installs dev dependencies (pytest, ruff)
uv run pytest                            # run the full Python suite
uv run pytest tests/test_api.py -k name  # run a single file or test
uv run ruff check .                      # lint
uv run ruff format --check .             # format check (CI); drop --check to reformat
python3 scripts/validate_plugins.py      # plugin manifests and the marketplace registry
node --test tests/workspace-ui.test.mjs tests/workspace-speech.test.mjs   # browser-side workspace JS
```

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`,
`pytest -q` and `validate_plugins.py` on every push and pull request. The
`node --test` files are plain Node (no npm install, no `package.json`); run
them locally when touching `agent_ptt/static/workspace*.js`.

The suite in `tests/` covers the channel manager, data models, TTS registry, audio mixer bookkeeping, all REST + WebSocket endpoints, the workspace API and migrations, and the plugin hook scripts (run as subprocesses). TTS synthesis and speaker playback are faked (`tests/conftest.py`), and a temporary SQLite database is used — no network, audio hardware, or running server required. Set `AGENT_PTT_TEST_POSTGRES` to also run the migration tests against a real PostgreSQL (see [workspaces.md](workspaces.md#hosting-boundary-and-persistence)).

What automated tests **cannot** cover is the audible path: real pocket-tts synthesis and actual speaker output. Use the manual walkthrough below for that.

## Manual Testing

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) installed

### Setup

```bash
cd /path/to/agent-ptt
uv sync
```

## Start the Server

```bash
uv run agent-ptt server start --port 8770
```

Leave this running in its own terminal.

## Test Commands (in a new terminal)

```bash
cd /path/to/agent-ptt

# Create a channel
uv run agent-ptt channel create "Test Room"

# List channels (grab the channel ID from here)
uv run agent-ptt channel list

# Join a channel (replace <channel-id> with the actual ID)
uv run agent-ptt join <channel-id> --handle "Krille" --voice "marius"

# Send a message — plays through speakers as speech 🔊
uv run agent-ptt say "Hello world, this is Agent PTT"

# List available voices
uv run agent-ptt voices

# Listen as a spectator (in yet another terminal)
uv run agent-ptt listen <channel-id>

# Leave the channel
uv run agent-ptt leave

# Check your current config/session
uv run agent-ptt config
```

## Multi-Agent Test

Open 3 terminals:

**Terminal 1 — Server:**
```bash
cd /path/to/agent-ptt
uv run agent-ptt server start --port 8770
```

**Terminal 2 — Agent 1:**
```bash
cd /path/to/agent-ptt
uv run agent-ptt channel create "War Room"
# copy the channel ID
uv run agent-ptt join <channel-id> --handle "Claude" --voice "alba"
uv run agent-ptt say "Hello, I'm Claude. Ready to discuss."
```

**Terminal 3 — Agent 2:**
```bash
cd /path/to/agent-ptt
uv run agent-ptt join <channel-id> --handle "GPT" --voice "marius"
uv run agent-ptt say "Hey Claude, GPT here. Let's go."
```

Both messages will be synthesized with different voices and played through speakers.

## Popular Voices

| Voice ID | Engine |
|----------|--------|
| `alba` | Pocket TTS |
| `marius` | Pocket TTS |
| `javert` | Pocket TTS |
| `jean` | Pocket TTS |
| `fantine` | Pocket TTS |
| `cosette` | Pocket TTS |
| `eponine` | Pocket TTS |
| `azelma` | Pocket TTS |


Run `uv run agent-ptt voices` for the full list of English voices.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///agent_ptt.db` | Database connection (swap to Turso: `libsql://...`) |
| `AGENT_PTT_API_KEY` | unset | When set, every REST route and WebSocket needs it (see [Authentication](api-reference.md#authentication)) |
| `AGENT_PTT_HOSTED` | `0` | `1` enables the multi-tenant workspace and disables the local channel routes |

## Targeted agent mentions

Run `uv run pytest tests/test_mentions.py tests/test_receive.py` for routing,
restart recovery, receipt isolation, and hook handoff/retry coverage. For a
live check, refresh the plugins and start an agent session, select it from
the browser composer’s `@` menu, and send a request while it works. The receipt
should change from pending to delivered after a tool or turn boundary. Repeat
while idle: it stays pending until another hook event runs. Delivered records
a hook handoff, not task completion. See [Agent mentions](mentions.md).
