# Tenant session presence

> Archived 2026-09-27: historical implementation/design record. Session assignments
> and deployment/test claims below describe that earlier work, not current instructions.
> Start with [current documentation](../../README.md) and the [next phase](../../next-phase.md).

> Status: server side shipped 2026-09-27 — `agent_ptt/workspace_presence.py`, `workspace_session_presence` table, `20260927_presence` revision, routes documented in [workspaces.md](../../workspaces.md#sessions-and-presence). Plugin heartbeats and UI rendering per owner, unverified.

Ezra implementation: `agent_ptt/workspace_presence.py`. This is the hosted
counterpart of the presence slice in [presence-and-attention.md](../../plans/presence-and-attention.md).
It does not implement attention bands, plugin heartbeats, or UI rendering.

## Integration (Olive)

Import `agent_ptt.workspace_presence` before `Base.metadata.create_all()` or
migration metadata discovery. Its single additive table is
`workspace_session_presence`; declarations live in that module, using the
existing Base. Call `workspace_presence.install(app)` from workspace installation
once. It installs `router`; the common workspace middleware must remain active
for cookie-origin checks and response security headers. The module does not
import workspace.py. No legacy table or data is changed.

For the baseline migration, use the module's `SessionPresence.__table__`:

- Composite primary key: `org_id` (String), `session_id` (String(128)).
- Nullable `user_id` and `agent_id` (String), with an exactly-one check.
- Composite foreign keys `(org_id,user_id)` to workspace_members and
  `(org_id,agent_id)` to workspace_agents, both ON DELETE CASCADE.
- Nullable `channel_id` (String), composite foreign key `(org_id,channel_id)`
  to workspace_rooms; no cross-tenant channel reference is permitted.
- Required `state` (String(16)), constrained to the six states below.
- Required `since` and `last_refresh` (DateTime), stored as naive UTC following
  the existing workspace convention; all writes provide server timestamps.
- Nullable `task` (String(128)), `tokens_in` and `tokens_out` (Integer), with
  nonnegative checks on token counts. API counts are limited to signed 32-bit.

All table creation/migration and route registration changes belong to Olive.
The module and focused tests can be reviewed independently before registration.

## Heartbeat client contract (Olive / Hazel)

Use the existing opt-in hosted client and its agent bearer token:

`POST /api/workspace/organizations/{org_id}/sessions/{session_id}/presence`

```json
{"state":"working","task":"Implement presence","channel_id":"room-uuid","tokens_in":140}
```

Returns 204, no body. Session IDs are 1–128 word characters, dots, or hyphens.
Use a stable ID per real session and serialize its updates. Identity and handle
come from authentication, never from request fields. A session ID within an
organization is reserved to its first authenticated identity; even an owner
cannot update someone else's row. Separate organizations may reuse IDs.

`state` is required: `idle`, `listening`, `thinking`, `working`, `draining`, or
`offline`. Send every 30 seconds while the session is known to be alive; retain
the current state for heartbeats. Send `offline` on an observed session end.
A responder must not refresh a session merely because it remembers its ID.
Chat sends and inbox acknowledgements do not update presence.

`task`, `channel_id`, `tokens_in`, and `tokens_out` are optional. Omission preserves
the existing value; explicit null clears it. Token counts are client-reported
cumulative snapshots, not billing data. Resetting a count is allowed. `task` has
a 128-character limit. Unknown fields, including client timestamps and identity
fields, are rejected. The server sets `last_refresh` on every accepted update;
`since` changes only on the first update or a reported state transition.

An identical retry safely refreshes the heartbeat without changing `since`.
Concurrent first registration is resolved against the authenticated owner.
Clients must serialize state transitions: this API has no client sequence counter,
so it cannot distinguish a delayed old state update from a new transition.

Cookie-authenticated humans may report their own sessions through the same API,
subject to the existing Origin check. Revoked agents, expired credentials, and
removed members cannot write. Invalid credentials return 401, inaccessible orgs,
channels, or sessions return 404, invalid payloads return 422. Each identity has
an in-process limit of 240 updates/minute and 100 stored sessions per org (429
on overflow). Reuse an existing ID after hitting the cap; a retention/deletion
policy is future work. These bounds are not distributed quota enforcement.

## UI contract (Ada)

`GET /api/workspace/organizations/{org_id}/sessions`

Returns an array with this fixed row shape:

```json
{
  "session_id": "session-uuid",
  "principal_id": "authenticated-agent-uuid",
  "kind": "agent",
  "agent": "Ezra",
  "handle": "Ezra",
  "state": "working",
  "since": "2030-01-01T12:00:00Z",
  "task": "Implement presence",
  "channel_id": "room-uuid",
  "last_refresh": "2030-01-01T12:00:30Z",
  "presumed_hung": false,
  "stale": false,
  "effective_state": "working",
  "tokens_in": 140,
  "tokens_out": null
}
```

`agent` is the authenticated agent's current display name, or null for humans;
`kind` is `agent` or `human`. Use `principal_id` to match roster identities and
`session_id` to distinguish concurrent sessions, never the display name.
Unreported identities have no session row; this absence does not prove offline.

Optional `channel_id` filters sessions by their reported channel; the channel
must belong to the requesting org. Pagination is `limit` (default 200, max 1000)
and `offset` (default 0), ordered by session ID. Clear cached rows on org switch.
All authorized members can read their org's roster; there is no cross-org view.
Revoked agents and removed members are excluded on every read.

Keep the reported `state` visible and distinct from server inference:

- `presumed_hung`: reported `thinking`/`working` and heartbeat age **over** 90 s.
  This is suspicion of lost contact, not evidence a model is stuck.
- `stale`: any reported state and heartbeat age **over** 3600 s.
- `effective_state`: `offline` when stale; otherwise the reported state.
  Explicit reported `offline` is distinguishable from inferred offline via
  `state` and `stale`. An offline report does not become stale immediately.

The derived fields are computed at read time and never overwrite the persisted
state. Fresh heartbeat receipt clears inferred expiry. There is no background
sweeper or inference of model activity from chat. The long idle expiry preserves
the source plan's semantics; it is not a 90-second offline timeout.

## Validation and remaining work

`pytest tests/test_workspace_presence.py` tests server-clock boundaries without
sleeping; heartbeat recovery/state transitions; authenticated identity ownership;
cross-tenant reads/writes/channel references; expiry/revocation; membership
removal; cookie-origin enforcement; fixed response fields; pagination and bounds;
and chat leaving presence untouched. Tests use an isolated SQLite database and
real auth helpers with seeded credentials. No shared server restart is needed.

Plugin clients and UI remain separate owner integrations. This slice does not
claim live heartbeats in production, model-call detection, browser checks, or
PostgreSQL validation. Olive owns baseline migration and Postgres checks.
