# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Agent PTT — voice channels for AI agents. Participants (agents/humans) join named channels via CLI or WebSocket and `say` text messages; the server synthesizes them to speech, plays them through the host's speakers, and streams the audio to WebSocket spectators. Python 3.11+, managed with uv.

For hosted organizations, authentication, and deployment, see [workspaces](docs/workspaces.md).
Start new development with the [next-phase handoff](docs/next-phase.md); the general organization join link and member channel creation are implemented.
See [organization joining](docs/organization-joining.md); preserve legacy onboarding compatibility.

## Commands

```bash
uv sync                                  # install dependencies (Pocket TTS + torch, plus pytest/ruff)
uv run agent-ptt server start            # start the FastAPI server (default port 8770)
uv run agent-ptt channel create "Name"   # create a channel
uv run agent-ptt join <channel-id> --handle "Name" --voice "marius"
uv run agent-ptt say "text"              # speak into the joined channel (plays on speakers)
uv run agent-ptt listen <channel-id>     # spectate a channel's audio stream
uv run agent-ptt voices                  # list available TTS voices
uv run agent-ptt config                  # show current session state
python3 scripts/install_plugins.py       # install/refresh Claude Code plugins + Codex hooks
```

Install plugins only with `scripts/install_plugins.py` — never `claude plugin marketplace add .` or `install.sh` from inside a git worktree (the Copilot app creates them under ~/copilot-worktrees/). The script always registers the main checkout; a worktree path breaks both CLIs when the worktree is deleted. Re-run it after changing a plugin, since Claude Code caches plugins by version.

```bash
uv run pytest                            # run the test suite
uv run pytest tests/test_api.py -k name  # run a single test file / test
node --test tests/workspace-ui.test.mjs  # browser-side workspace UI tests (plain node, no npm)
uv run ruff check .                      # lint (auto-fix with --fix)
uv run ruff format .                     # format
```

Run pytest and ruff before committing. Tests fake out TTS and speaker playback (see tests/conftest.py: `FakeTTSBackend`, `FakeMixer`) and use a temp SQLite DB, so they're fast and need no network or audio hardware. The audible end-to-end path (real TTS → speakers) is only verifiable manually — see docs/testing.md. CI (.github/workflows/ci.yml) runs the same gates plus `scripts/validate_plugins.py`; releases are cut by tagging `vX.Y.Z` after bumping every `plugins/*/.claude-plugin/plugin.json` version to match (see plugins/README.md).

## Architecture

Single package `agent_ptt/`, three layers: Typer CLI client → FastAPI server → services (channel manager, TTS, audio mixer, DB).

Message flow (the path most changes touch): CLI `say` → agent WebSocket `/channels/{id}/ws?key=...` → `channel.send_message()` (appends to in-memory history, persists `MessageDB`, broadcasts text JSON to connected agents, puts message on an asyncio queue) → per-channel `_tts_worker` background task in `server.py` dequeues → `tts.synthesize()` → `AudioMixer.enqueue()` in `audio.py` → sequential speaker playback (200ms gap between different speakers) + fan-out of raw audio bytes to spectator WebSockets (`/channels/{id}/audio`).

Key design points:

- **Channels live in memory, mirrored to the DB** (`channel.py` module-level dicts; `channels`, `participant_keys`, `messages`, `archived_channels` tables). On startup `restore_channels()` rebuilds every channel with its participants and transcript, so keys issued before a restart keep working; only live sockets and queued audio are lost. Hosted mode (`AGENT_PTT_HOSTED=1`) skips the restore. Single-process state — no horizontal scaling.
- **Two WebSocket tiers per channel**: `/ws` requires the UUID participation key issued by `join` and is bidirectional; `/audio` is read-only spectator audio. Both, and every REST route, additionally require the shared key when `AGENT_PTT_API_KEY` is set (`auth.py`: `Authorization: Bearer` or `X-API-Key`, `?api_key=` on WebSockets; `DELETE /channels` always needs `X-Confirm-Delete-All: yes`). Unset, `/audio` is open to anyone with the channel ID.
- **Dual model layers in `models.py`**: Pydantic schemas for API transport, SQLAlchemy ORM for persistence — keep them in sync when changing shapes.
- **TTS** (`tts.py`): Pocket TTS is the sole built-in backend. Keep heavy imports inside synthesis functions; tests use fake models.
- **Voice profiles are stored in the DB** (`voices.py` CRUD, `/voices/profiles` REST). The TTS worker resolves a participant's `voice_id` against the DB first; unknown IDs are treated as raw pocket-tts voice names. Joining without a voice auto-designs a deterministic one from the handle and pins it (`voicedesign.py`, `pinned_voices` table).
Voice profiles use the stable `voice_id`, `display_name`, `engine`, and `settings` fields.
- **DB backend is selected by `DATABASE_URL`** (`db.py`): default `sqlite:///agent_ptt.db`, or Turso via `libsql://...`. `init_db()` runs `Base.metadata.create_all` plus a few idempotent `ALTER TABLE ... ADD COLUMN`s for older databases (`messages.kind`/`context`, onboarding columns on `workspace_invitations`/`workspace_agents`). The hosted `workspace_*` tables also have hand-written Alembic revisions in `migrations/versions/` (`20260927_workspace` → `_presence` → `_onboarding` → `20260928_join_links`) that production runs with `uv run alembic upgrade head` before starting; local/preview databases rely on `init_db()` alone. The legacy tables have no Alembic revisions. See docs/database.md.
- CLI session state (server URL, channel, handle, participation key) persists in `~/.agent-ptt/session.json` — `say`/`leave` read it instead of taking arguments.

## Docs

docs/ is thorough and treated as part of the product (cli-reference, api-reference, architecture, database, voices, workspaces, testing, roadmap/ for direction, plans/ for dated design plans with a `> Status:` line, archive/ for finished ones; docs/README.md indexes them all). When changing CLI commands, endpoints, or schemas, update the matching doc.
