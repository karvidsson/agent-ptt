# Database & Turso Migration

> Organization signup, RBAC, agent credentials, and tenant-scoped text chat are
> documented in [Organization workspaces](workspaces.md). Hosted mode disables
> the legacy local-only endpoints described below.

## Local Development (Default)

By default, Agent PTT uses a local SQLite file:

```
sqlite:///agent_ptt.db
```

The file is created automatically on first server start in the working directory.

## What's Persisted

Every table, by module. All of them are created by `init_db()`; the
`workspace_*` tables additionally have Alembic revisions (see
[Migrations](#migrations-alembic)).

| Table | Defined in | Data | Survives Restart |
|-------|-----------|------|-----------------|
| `channels` | `models.py` | Channels (ID, name) — restored into memory on startup | ✅ |
| `messages` | `models.py` | Message archive (sender, text, kind, timestamp, optional context) | ✅ |
| `participant_keys` | `models.py` | Agent identities (key, handle, voice, channel) | ✅ |
| `voice_profiles` | `models.py` | Voice configurations (ID, name, engine, settings) | ✅ |
| `pinned_voices` | `models.py` | Handle → auto-designed voice, so a handle always sounds the same | ✅ |
| `session_identities` | `models.py` | CLI announcer session `(session_id, agent)` → random handle + voice | ✅ |
| `archived_channels` | `models.py` | IDs of closed channels and when they were archived | ✅ |
| `revoked_participants` | `models.py` | Kicked keys that must not rejoin automatically | ✅ |
| `mention_receivers` | `models.py` | Participation key → stable mention recipient ID | ✅ |
| `message_deliveries` | `models.py` | One inbox receipt per `(message, recipient)` | ✅ |
| `projects`, `project_members` | `models.py` | Collaboration projects (by normalized git remote) and their members, with roles and secret member keys | ✅ |
| `work_items`, `work_item_deliveries`, `work_item_events` | `models.py` | The project inbox: work items, per-member deliveries (the inbox cursor) and the change log | ✅ |
| `workspace_users`, `workspace_organizations`, `workspace_members`, `workspace_credentials` | `workspace_models.py` | Hosted accounts, tenants, roles and hashed session/agent credentials | ✅ |
| `workspace_agents`, `workspace_invitations` | `workspace_models.py` | Registered agents per organization; one-use invitation links (with onboarding `agent_limit`, `channel_id`, `claimed_by`) | ✅ |
| `workspace_rooms`, `workspace_messages`, `workspace_audit` | `workspace_models.py` | Tenant channels, text chat and administrative audit log | ✅ |
| `workspace_deliveries`, `workspace_replies` | `workspace_delivery.py` | Tenant inbox receipts and linked agent replies | ✅ |
| `workspace_session_presence` | `workspace_presence.py` | Per-session presence state (`idle`…`offline`, task, tokens) | ✅ |

The `workspace_*` tables are described in [Organization workspaces](workspaces.md);
the rest of this page covers the local (legacy) tables.

A restart is invisible to participants: the server reloads its channels, their participants and their transcripts on startup, so a key issued before the restart still posts afterwards. Presence is not stored: restored participants show as `offline` until they are seen again, and ones that stay silent for the idle grace period (`AGENT_PTT_IDLE_REAP_SECONDS`, default 600 s) are removed like a leave — except mention receivers (hooked agent sessions), which turn `away` and are removed only after `AGENT_PTT_AWAY_REAP_SECONDS` more (default 7 days, `0` = never).

Only live connections are lost — WebSocket spectators reconnect, and audio already queued for playback is dropped.

## Schema

### `channels`

| Column | Type | Description |
|--------|------|-------------|
| `channel_id` | TEXT (PK) | UUID |
| `name` | TEXT | Channel name |
| `created_at` | DATETIME | Creation time (UTC) |

### `voice_profiles`

| Column | Type | Description |
|--------|------|-------------|
| `voice_id` | TEXT (PK) | UUID |
| `display_name` | TEXT | Human-readable name |
| `engine` | TEXT | TTS engine identifier |
| `settings` | JSON | Engine-specific parameters |
| `created_at` | DATETIME | Creation timestamp |

### `participant_keys`

| Column | Type | Description |
|--------|------|-------------|
| `key_id` | TEXT (PK) | UUID participation key |
| `handle` | TEXT | Display name |
| `voice_id` | TEXT | Associated voice profile (nullable) |
| `channel_id` | TEXT | Current channel (nullable; cleared when the participant leaves, is reaped as idle, is kicked or the channel is archived, so `restore_channels()` skips it) |
| `created_at` | DATETIME | Creation timestamp |

### `pinned_voices`

| Column | Type | Description |
|--------|------|-------------|
| `handle` | TEXT (PK) | Handle, stored lowercase |
| `voice_id` | TEXT | The auto-designed profile (`auto-<handle>`) in `voice_profiles` |
| `source` | TEXT | How the voice was chosen (`hash` by default) |
| `created_at` | DATETIME | When the pin was made |

### `messages`

| Column | Type | Description |
|--------|------|-------------|
| `message_id` | TEXT (PK) | UUID |
| `channel_id` | TEXT (indexed) | Channel the message was sent in |
| `sender_key` | TEXT | Participant key of the sender (secret; the API exposes only its hash as `sender_id`) |
| `handle` | TEXT | Sender's display name |
| `text` | TEXT | Message content |
| `kind` | TEXT | `message`, `action` (`/me`) or `notice` |
| `context` | JSON (nullable) | Optional [message context](api-reference.md#say-post-a-message-via-rest): repo, branch, files, tools, task… as sent, `None` keys dropped. At most 4 KB. Never spoken; served by `GET /channels/{id}/messages/{message_id}` |
| `timestamp` | DATETIME | When the message was sent |
| `fyi` | BOOLEAN | True for an FYI `say`: its mentions were delivered non-waking (default false) |

`init_db()` adds the `kind`, `context` and `fyi` columns to a `messages` table
created before they existed (`create_all` does not alter tables).

## Migrating to Turso

[Turso](https://turso.tech) is distributed SQLite — your data stays in SQLite format but is hosted in the cloud with edge replicas.

### 1. Create a Turso database

```bash
turso db create agent-ptt
turso db tokens create agent-ptt
```

### 2. Set the environment variable

```bash
export DATABASE_URL="libsql://agent-ptt-yourorg.turso.io?authToken=your-token-here"
```

### 3. Start the server

```bash
agent-ptt server start
```

That's it — zero code changes. The `sqlalchemy-libsql` dialect handles the connection seamlessly.

### 4. Verify

```bash
turso db shell agent-ptt
> SELECT COUNT(*) FROM voice_profiles;
> SELECT COUNT(*) FROM messages;
```

## Migrations (Alembic)

Two mechanisms manage the schema, and they cover different tables:

**`init_db()` at startup** (`agent_ptt/db.py`) runs `Base.metadata.create_all`
for every table above, then applies a few idempotent `ALTER TABLE … ADD COLUMN`
statements for databases created before a column existed: `messages.kind`,
`messages.context`, `messages.fyi`, `message_deliveries.wake`, `work_items.fyi`,
`workspace_invitations.agent_limit` / `channel_id` /
`claimed_by`, and `workspace_agents.onboarded_by` / `harness`. It also creates
the `ux_workspace_message_tenant` index on a pre-existing `workspace_messages`
table before the inbox foreign keys that need it. This is what a local or
preview server relies on; re-running it is safe.

**Alembic revisions** in `migrations/versions/` exist only for the hosted
`workspace_*` tables and are what a production deployment runs before starting
a new server version (see [workspaces.md](workspaces.md#hosting-boundary-and-persistence)):

| Revision | Adds |
|----------|------|
| `20260927_workspace` | Baseline: the eleven workspace/delivery tables; adopts compatible pre-migration preview tables, refuses incompatible ones |
| `20260927_presence` | `workspace_session_presence` |
| `20260927_onboarding` | The invitation and agent onboarding columns listed above |
| `20260928_join_links` | `workspace_join_links` (one active reusable link per organization) and `workspace_agent_enrollments` (retry identity and invitation provenance) |

Downgrades are refused by design (they would destroy tenant data); restore a
backup instead. The legacy tables (`channels`, `messages`, `voice_profiles`, …)
have no Alembic revisions — `init_db()` is their only schema management.

```bash
uv run alembic upgrade head     # apply pending workspace revisions
uv run alembic current          # show the applied revision
uv run alembic revision -m "…"  # author a new one (hand-written; keep it additive)
```

The Alembic config is in `alembic.ini` and `migrations/env.py`.


## CLI session identities

`session_identities` stores the composite key `(session_id, agent)`, a unique
random `handle`, its saved `voice_id`, and `created_at`. This pairing is separate
from channel participation keys and survives channel changes and server restarts.
The table is created by `init_db()` alongside the existing tables.

Names come from a pool of 40 (`NAMES` in `session_identity.py`) and are reused.
A handle is *busy* while a `participant_keys` row with that handle sits in an
existing channel (`channel_id` set; leaving, idle reaping and kicks clear it),
including plain-handle participants who joined without a session. A new session
takes the first tier (plain names, then `2`, `3`, … appended with no space, so `@Pearl2` needs no quotes) that has a name that
isn't busy, preferring a name no session holds; otherwise it reclaims the name
of a *dormant* session (one in no channel), deleting that session's row: the one
whose handle was least recently issued a participation key, falling back to the
row's `created_at`. A session keeps its name and voice while its row exists; a
reclaimed session gets a fresh identity on its next join. New voices balance
first over sessions currently in a channel, then over all stored sessions.

## Mention inbox tables

`mention_receivers` binds a channel participation key to a stable agent-session
recipient ID. `message_deliveries` stores one receipt per `(message_id,
recipient_id)`, with channel, display name, creation time, nullable delivery
time, and `wake` (BOOLEAN, default true; false for an FYI message, which a
`wake_only=1` inbox read skips). Both are additive tables created by `init_db()`; message and delivery
rows share one transaction. A composite index covers recipient, channel,
delivery time, and creation time for bounded pending-inbox reads. See
[Agent mentions](mentions.md#storage) for lifecycle and recovery semantics.


## Project inbox tables

Five additive tables back [projects and the project inbox](projects.md). Like
the other legacy tables they have no Alembic revision: `init_db()` creates
them on new and existing databases (`create_all`); the presence columns
`project_members.machine`, `doing`, `doing_at` and `plugin_version` came later and are added to
older databases by idempotent `ALTER TABLE ... ADD COLUMN`s in `init_db()`.
Hosted mode never uses them.

### `projects`

| Column | Type | Description |
|--------|------|-------------|
| `project_id` | TEXT (PK) | UUID |
| `name` | TEXT (indexed) | Display name; defaults to the remote's last path segment |
| `remote` | TEXT (unique, nullable) | Normalized git remote, e.g. `github.com/owner/repo` |
| `channel_id` | TEXT (nullable) | The project's default channel |
| `created_at` | DATETIME | Creation time (UTC) |

### `project_members`

| Column | Type | Description |
|--------|------|-------------|
| `member_id` | TEXT (PK) | Public member ID |
| `project_id` | TEXT (indexed) | Project |
| `member_key` | TEXT (unique) | Bearer secret for member routes; returned only by join, never served elsewhere |
| `handle` | TEXT | Display name |
| `roles` | JSON | Free-form role tags (`list[str]`) |
| `mode` | TEXT | `monitor` or `hooks` |
| `harness` | TEXT (nullable) | `claude`, `codex`, `script`, … |
| `machine` | VARCHAR(100) (nullable) | Host the member runs on, as it reported |
| `doing` | VARCHAR(200) (nullable) | What it reports working on (`PATCH members/me`) |
| `doing_at` | DATETIME (nullable) | When `doing` was last set |
| `plugin_version` | VARCHAR(20) (nullable) | Announcer plugin version it last joined with (stale installs show in `ptt who`) |
| `created_at`, `last_seen` | DATETIME | Join time and last member request |

### `work_items`

| Column | Type | Description |
|--------|------|-------------|
| `item_id` | TEXT (PK) | UUID |
| `project_id` | TEXT (indexed) | Project; `(project_id, updated_at)` is indexed for the board |
| `kind` | TEXT | `instruction`, `handoff`, `question`, `answer`, `claim`, `release`, `note` |
| `to_address` | TEXT | `agent:<member_id>`, `role:<role>` or `project` |
| `from_member_id` | TEXT | Sender |
| `title`, `body` | TEXT | Short line (≤ 120) and text (≤ 8000) |
| `context` | JSON (nullable) | Handoff context, free-form keys allowed |
| `reply_to` | TEXT (indexed, nullable) | Parent item |
| `client_id` | TEXT (nullable) | Idempotency key; unique per `(from_member_id, client_id)` |
| `state` | TEXT | `pending`, `delivered`, `acked`, `claimed`, `waiting`, `review`, `done`, `declined` |
| `assignee_member_id` | TEXT (nullable) | Claim holder |
| `lease_expires_at` | DATETIME (nullable) | When the claim lease runs out |
| `notice_message_id` | TEXT (nullable) | The creation notice in `messages` |
| `fyi` | BOOLEAN | Informational item: delivered, but skipped by `wake_only=1` inbox reads (default false) |
| `created_at`, `updated_at` | DATETIME | Creation and last change |

### `work_item_deliveries`

| Column | Type | Description |
|--------|------|-------------|
| `seq` | INTEGER (PK, autoincrement) | The inbox cursor; a reset delivery gets a new `seq` |
| `item_id`, `member_id` | TEXT (indexed) | Unique together |
| `state` | TEXT | `pending`, `delivered`, `acked` |
| `delivered_at`, `acked_at` | DATETIME (nullable) | First inbox read and ack |

### `work_item_events`

| Column | Type | Description |
|--------|------|-------------|
| `event_id` | INTEGER (PK, autoincrement) | Order of events |
| `item_id` | TEXT (indexed) | Item |
| `at` | DATETIME | When |
| `by_member_id` | TEXT (nullable) | Who; null for the server (lease expiry) |
| `action` | TEXT | `created`, `delivered`, `acked`, `claimed`, `released`, `waiting`, `review`, `resumed`, `answered`, `done`, `declined`, `lease_expired` |
| `from_state`, `to_state` | TEXT (nullable) | The transition |
| `note` | TEXT (nullable) | Optional note (e.g. from done/decline) |

Channel notices about items are ordinary `messages` rows with `kind = notice`
and `sender_key = project-member:<member_id>` (or `project-server`). That tag
is public and not a key.

## Channel moderation

`archived_channels` stores closed channel IDs and their archive timestamps;
channel and message records remain intact. `revoked_participants` records revoked
keys so automatic retries receive 403. Removed participant records have their
channel association cleared. Both new tables are created by `init_db()` without
altering existing tables. Archived channels restore as read-only with no workers.

General join secrets and agent credentials are stored only as hashes. Enrollment
records survive credential revocation so replay cannot mint replacement access.
See [organization joining](organization-joining.md) for lifecycle semantics.
