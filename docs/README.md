# Documentation index

Every page under `docs/`, one line each. Reference pages describe the code as
it is; `roadmap/` states direction; `plans/` holds dated design plans, each
with a `> Status:` line near the top that is repeated in the table below;
`archive/` keeps finished or superseded plans for the record. The project
[readme](../readme.md) is the front door; `CLAUDE.md` at the repo root is the
contributor brief.

Start the next development session with the [next-phase handoff](next-phase.md).
The [open-plan index](plans/README.md) and [archive index](archive/README.md) separate
future decisions from historical implementation notes.

## Reference

| Page | What it covers |
|------|----------------|
| [overview.md](overview.md) | What Agent PTT is, in a page: voice channels for a team of coding agents |
| [installation.md](installation.md) | Prerequisites, `uv sync`, platform notes, first run |
| [cli-reference.md](cli-reference.md) | Every `agent-ptt` command, option and example (`server`, `channel`, `join`/`say`, IRC commands, `voice`, `model`, `config`) |
| [api-reference.md](api-reference.md) | Local REST + WebSocket API: endpoint index, auth, channels, commands, history and context, voices, health probes |
| [architecture.md](architecture.md) | Module breakdown, message flow, persistence model |
| [database.md](database.md) | Every table, `init_db()` versus the Alembic revisions, Turso setup |
| [voices.md](voices.md) | Voice profiles, the Pocket TTS catalog, cloning, pinned voices |
| [mentions.md](mentions.md) | `@name` addressing: recipients, durable inboxes, acknowledgement and hook handoff |
| [workspaces.md](workspaces.md) | Hosted multi-tenant mode: signup, roles, invitations, agent credentials, `/api/workspace` reference, presence, speech, launch checklist |
| [agent-onboarding.md](agent-onboarding.md) | Current developer/batch flow; retained until universal invitations replace it |
| [deployment.md](deployment.md) | Running the server as one container with PostgreSQL behind an HTTPS proxy |
| [proxmox-testing.md](proxmox-testing.md) | The current Proxmox test VM: what is deployed there and how to reach it |
| [testing.md](testing.md) | Automated gates (`pytest`, `ruff`, `validate_plugins.py`, `node --test`), manual and multi-agent walkthroughs |

## Roadmap (`roadmap/`)

| Page | What it covers |
|------|----------------|
| [roadmap/README.md](roadmap/README.md) | Status table: what shipped, what is deferred, and links into `plans/` and `archive/` |
| [roadmap/client-speech.md](roadmap/client-speech.md) | Client-side Pocket TTS generation in the browser — deferred |

## Plans (`plans/`)

| Page | What it covers | Status |
|------|----------------|--------|
| [plans/irc-commands.md](plans/irc-commands.md) | IRC-style channel commands (`names`, `whois`, `me`, `notice`, `topic`, `away`, `back`) | First slice implemented 2026-09-27; tiers 2–3 open |
| [plans/presence-and-attention.md](plans/presence-and-attention.md) | Session presence, attention bands, naming, and pull-based message context | Section 4 (message context) slices 1–3 done; sections 1–3 proposals |
| [plans/announcer-clarity.md](plans/announcer-clarity.md) | Making the announcer plugins say what agents are actually working on | Slices 1–2 done 2026-09-27; proposals 3–7 open |
| [plans/universal-invitations.md](plans/universal-invitations.md) | One invitation flow for people and agents | Product direction agreed 2026-09-27; planning only |
| [plans/copilot-antigravity.md](plans/copilot-antigravity.md) | Announcer support for GitHub Copilot CLI and Google Antigravity | Proposal 2026-09-27; nothing built |

## Archive (`archive/2026-09/`)

Historical records; each opens with an "Archived" note. Read them for the
reasoning behind what shipped, not as current instructions.

| Page | What it covers | Status when archived |
|------|----------------|----------------------|
| [org-auth.md](archive/2026-09/org-auth.md) | Auth, tenancy and deployment for an organization | Step 0 (shared API key) shipped; later steps superseded by [workspaces.md](workspaces.md) |
| [workspace-presence.md](archive/2026-09/workspace-presence.md) | Tenant session presence table and `/sessions` routes for hosted mode | Server side shipped 2026-09-27; plugin heartbeats and UI per owner |
| [hosted-ui.md](archive/2026-09/hosted-ui.md) | Converging the `/workspace/` UI with the original Agent PTT look | Implemented 2026-09-27; remaining items per owner |
| [hosted-agent-assignments.md](archive/2026-09/hosted-agent-assignments.md) | Handoff contract distributing the hosted workstreams to agents | Coordination handoff 2026-09-27; historical |
| [speech-delivery.md](archive/2026-09/speech-delivery.md) | Optional speech delivery: server route and browser adapter | Server route and client adapter implemented |
| [distributed-channels.md](archive/2026-09/distributed-channels.md) | Supabase-backed distributed channels | Historical proposal; predates the removal of Edge/system TTS |
