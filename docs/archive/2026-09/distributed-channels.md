# Roadmap: Distributed Channels (Supabase / Global Storage)

> Archived 2026-09-27: historical implementation/design record. Session assignments
> and deployment/test claims below describe that earlier work, not current instructions.
> Start with [current documentation](../../README.md) and the [next phase](../../next-phase.md).

> Historical design notes: Pocket TTS is now the default; Edge TTS and system TTS have been removed. See [current voice documentation](../../voices.md).

## Problem

Today one server process owns everything live: channels, message queues, TTS,
and the speakers. Agents must reach that exact process over HTTP/WS, and audio
plays only on the machine where it runs.

We want the pieces to live anywhere:

- **Agents push from anywhere** — a laptop, a CI runner, a cloud sandbox —
  via the plugins/skills, which are already pure REST clients.
- **Listen from anywhere** — your office Mac speaks what agents elsewhere are
  doing; a colleague listens to the same channel from another city.
- **Durable, global state** — voice profiles, pinned voices, and message
  history in one shared database (e.g. Supabase Postgres or Turso) instead of
  a local SQLite file per machine.

## What already works / what doesn't

| Piece | Today | Distributed-ready? |
|-------|-------|--------------------|
| Plugins & skills (announcer, /say, Codex hooks) | REST clients, `AGENT_PTT_URL` | ✅ point them at any host |
| Spectator audio (`agent-ptt listen`) | WS stream from the server | ✅ works over the network |
| Durable state (profiles, pins, archive) | SQLite via `DATABASE_URL` | ✅ Turso today; Postgres needs verification |
| Live channels + TTS queues | **In-memory dicts in one process** | ❌ single process owns everything |
| Speaker playback | Server's own speakers | ❌ tied to the hub machine |
| Auth | Opaque UUID participation keys | ❌ fine on LAN, not for the public internet |

The core insight: **text is the payload, not audio**. Voice profiles and pins
live in the shared DB, so any node holding a profile renders the same voice.
Ship text through global infrastructure and synthesize at the edge — audio
never needs to cross the network except for casual spectators.

## Target architecture

```
  Agents (plugins/skills, anywhere)
        │  REST: POST /channels/{id}/say
        ▼
  Channel API  ──────────────►  Supabase / Turso
  (stateless-ish hub)           ├─ messages (archive + bus)
        │                       ├─ voice_profiles / pinned_voices
        │ pub/sub (Realtime      └─ channels + participants
        │  or LISTEN/NOTIFY)
        ▼
  Audio nodes (subscribe to channels, N per world)
  ├─ office-mac    → synthesizes + plays on speakers
  ├─ home-studio   → same channel, its own speakers
  └─ any laptop    → `agent-ptt listen` still works via a node
```

## Phases

### Phase 1: Headless hub (works almost today) — ~0.5 day
The playback loop already degrades gracefully when `sounddevice` is missing.
Make it official:
- `agent-ptt server start --no-audio` — skip speaker playback, keep the
  spectator WS stream.
- Deploy the hub on a VPS/Fly.io with `DATABASE_URL` pointing at Turso.
- Everyone sets `AGENT_PTT_URL` to the hub; listeners run `agent-ptt listen`.
- Limitation: audio is synthesized on the hub and streamed — fine for a few
  listeners, no per-room speakers yet.

### Phase 2: Supabase Postgres storage — ~1 day
- Verify the SQLAlchemy layer against Postgres (JSON columns, `db.merge`
  upserts); `DATABASE_URL=postgresql+psycopg://...supabase.co/postgres`.
- Author the initial Alembic migration (scaffolding exists, `migrations/` is
  empty) so schema changes stop relying on `create_all`.

### Phase 3: Message bus + audio nodes — the real distribution — ~2-3 days
- Replace the in-process `asyncio.Queue` with pub/sub: Supabase Realtime
  (channel per PTT channel) or plain Postgres `LISTEN/NOTIFY`.
- New `agent-ptt node start [--channels a,b]` — subscribes to channels,
  resolves voice profiles from the shared DB, synthesizes locally with Pocket TTS, plays through local speakers. The
  AudioMixer + TTS worker code moves nearly unchanged; only the queue source
  changes.
- The hub shrinks to a channel API: create/join/say/history, all backed by
  DB rows instead of in-memory dicts (participants are already persisted;
  channels gain a table + heartbeat/TTL for liveness).

### Phase 4: Auth for the public internet — ~1 day

See the expanded [organization auth plan](org-auth.md) for identity,
tenancy, permissions, and deployment. It supersedes this auth outline and its
estimate. Auth must precede public organization deployment and can ship before
the message bus or audio nodes.

- UUID keys stay as the participant identity, but API access needs a bearer
  token (Supabase auth JWT or static API keys) once the hub leaves the LAN.
- Spectator audio/WS endpoints get read tokens per channel.

## Gotchas

- **Double audio**: two audio nodes in the same room both play every message.
  That's a feature across rooms and a bug within one — nodes should be
  explicitly assigned to channels (`--channels`), not auto-subscribe to all.
- **Ordering**: pub/sub delivery order per channel must be preserved; sequence
  numbers on messages (already have timestamps + message_id) let nodes reorder
  or at least detect gaps.
- **Synth capability drift**: nodes must share compatible Pocket model and voice asset versions;
  report unavailable speech without blocking text.
- **Realtime payload limits**: text messages are tiny; never ship audio bytes
  through Realtime — spectators connect to a node, not the bus.
- **Voice assignment location**: pin design should happen in exactly one place
  (the hub) so two nodes don't race to design the same handle.

## Relation to existing docs

- [Database & Turso](../../database.md) — Phase 2 extends this to Supabase.
- [Architecture](../../architecture.md) — Phases 3 splits "server" into
  channel API + audio nodes; update when implemented.
