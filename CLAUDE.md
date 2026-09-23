# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Agent PTT — voice channels for AI agents. Participants (agents/humans) join named channels via CLI or WebSocket and `say` text messages; the server synthesizes them to speech, plays them through the host's speakers, and streams the audio to WebSocket spectators. Python 3.11+, managed with uv.

## Commands

```bash
uv sync                                  # install dependencies (base: no torch)
uv sync --extra omnivoice                # + local neural TTS (torch; model downloads ~2.4 GB on first use)
uv run agent-ptt server start            # start the FastAPI server (default port 8770)
uv run agent-ptt channel create "Name"   # create a channel
uv run agent-ptt join <channel-id> --handle "Name" --voice "en-US-GuyNeural"
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
uv run ruff check .                      # lint (auto-fix with --fix)
uv run ruff format .                     # format
```

Run pytest and ruff before committing. Tests fake out TTS and speaker playback (see tests/conftest.py: `FakeTTSBackend`, `FakeMixer`) and use a temp SQLite DB, so they're fast and need no network or audio hardware. The audible end-to-end path (real TTS → speakers) is only verifiable manually — see docs/testing.md. CI (.github/workflows/ci.yml) runs the same gates plus `scripts/validate_plugins.py`; releases are cut by tagging `vX.Y.Z` after bumping every `plugins/*/.claude-plugin/plugin.json` version to match (see plugins/README.md).

## Architecture

Single package `agent_ptt/`, three layers: Typer CLI client → FastAPI server → services (channel manager, TTS, audio mixer, DB).

Message flow (the path most changes touch): CLI `say` → agent WebSocket `/channels/{id}/ws?key=...` → `channel.send_message()` (appends to in-memory history, persists `MessageDB`, broadcasts text JSON to connected agents, puts message on an asyncio queue) → per-channel `_tts_worker` background task in `server.py` dequeues → `tts.synthesize()` → `AudioMixer.enqueue()` in `audio.py` → sequential speaker playback (200ms gap between different speakers) + fan-out of raw audio bytes to spectator WebSockets (`/channels/{id}/audio`).

Key design points:

- **Channels are ephemeral and in-memory** (`channel.py` module-level dicts). Only voice profiles, participation keys, and message archive persist to the DB. Server restart drops all live channels. Single-process state — no horizontal scaling.
- **Two WebSocket tiers per channel**: `/ws` is authenticated (requires the UUID participation key issued by `join`) and bidirectional; `/audio` is unauthenticated read-only spectator audio.
- **Dual model layers in `models.py`**: Pydantic schemas for API transport, SQLAlchemy ORM for persistence — keep them in sync when changing shapes.
- **TTS is pluggable** (`tts.py`): subclass `TTSBackend`, call `register_backend(name, instance)`. Backends: `edge-tts` (default, cloud, MP3 output), `system` (pyttsx3, offline), and `omnivoice` (`engines/omnivoice.py`, local neural TTS, registered only when the `omnivoice` extra is installed). Keep heavy imports (torch, omnivoice) inside functions — `engines/` modules must stay importable on the base install, and their tests use fakes, not torch.
- **Voice profiles are stored in the DB** (`voices.py` CRUD, `/voices/profiles` REST). The TTS worker resolves a participant's `voice_id` against the DB first; unknown IDs are treated as raw edge-tts voice names. Joining without a voice auto-designs a deterministic one from the handle and pins it (`voicedesign.py`, `pinned_voices` table).
- **Voice profiles intentionally mirror the OmniVoice Studio schema** (`voice_id`, `display_name`, `engine`, `settings`) — don't change this shape.
- **DB backend is selected by `DATABASE_URL`** (`db.py`): default `sqlite:///agent_ptt.db`, or Turso via `libsql://...`. Schema is created at runtime by `init_db()` (`Base.metadata.create_all`); Alembic is scaffolded in migrations/ but no migrations have been authored.
- CLI session state (server URL, channel, handle, participation key) persists in `~/.agent-ptt/session.json` — `say`/`leave` read it instead of taking arguments.

## Docs

docs/ is thorough and treated as part of the product (cli-reference, api-reference, architecture, database, voices, testing, roadmap/). When changing CLI commands, endpoints, or schemas, update the matching doc.
