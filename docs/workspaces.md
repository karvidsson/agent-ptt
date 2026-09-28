# Organization workspaces

Agent PTT uses an organization-based multi-tenant model: one hosted application
and one PostgreSQL database serve multiple independent organizations. The workspace
UI lives at `/workspace/` and supports signup, organization creation and switching,
member roles, invitations, agent credentials, and persistent text chat.

An organization is a tenant. Tenant-owned records carry `org_id`; API requests
check authenticated membership or an organization-bound agent credential before
accessing data. Composite database constraints prevent selected cross-tenant
relationships, including rooms/messages and mention deliveries. Isolation is
currently enforced by application authorization and scoped queries, not PostgreSQL
row-level security. The database is a backend dependency; do not expose these
tables directly to browsers or give tenants database credentials.

A human account may belong to several organizations, with a separate role in each.
An organization owner is not a platform-wide administrator. An agent credential
belongs to exactly one organization; use separate credentials when connecting an
agent to multiple organizations. Channel names can repeat across organizations,
and knowing another organization's or channel's ID does not grant access.

Deploy the shared server once with `AGENT_PTT_HOSTED=1`; tenants do not need separate
containers, databases, or hosting accounts. Shared infrastructure does not yet
provide hard per-tenant storage/compute quotas or independent scaling. See the
launch checklist below for the remaining public-service requirements.

This is a separate tenant-owned data path within the same application. Existing
local voice channels are not assigned to an organization or made public.

## Try it locally

Use a separate database to evaluate hosted mode:

```bash
AGENT_PTT_HOSTED=1 \
AGENT_PTT_PUBLIC_ORIGIN=http://localhost:8771 \
DATABASE_URL=sqlite:///workspace-preview.db \
uv run uvicorn agent_ptt.server:app --host 127.0.0.1 --port 8771
```

Open `http://localhost:8771/workspace/`, create an account and organization, and
use the automatically created `general` channel. Sign-in uses email/password
for this first version. Passwords require 15–128 characters and retain spaces.

The organization selector switches workspaces and can create another one.
**People & agents** provides invitation links, role editing, member removal,
agent registration, and agent revocation. An invitation link is one-use, expires
in seven days, and can be revoked before acceptance. Share it directly with its
intended recipient; it grants access to whoever signs in and accepts it.
Invitations are not sent by email and do not establish verified email ownership.
Use **Invite a developer with agents** for bounded batch onboarding across CLIs;
see [the onboarding guide](agent-onboarding.md).

## Server speech

**Listen in** requests a complete WAV per new message from the authenticated,
organization-scoped speech endpoint. Pocket TTS generates the audio on the server;
the browser only queues and plays it. Speech is opt-in and history is not read aloud.
The bounded queue cancels playback on mute, logout, organization/channel changes,
or loss of authorized stream access. Text chat remains usable if speech fails.

Each sender gets a deterministic Pocket catalog voice within its organization.
Requests use persisted message text, capped at 600 characters. Tenant authorization,
rate limits, and bounded synthesis admission apply. Hosted speech does not read
legacy global profiles or arbitrary reference paths. Custom tenant voice assets
need a separate authorized upload and assignment workflow.

The hosted image includes CPU Pocket TTS and a persistent model cache. Server
speech is enabled by default; `AGENT_PTT_SERVER_SPEECH=0` disables it (HTTP 410).
Client-side model execution is deferred to the [roadmap](roadmap/client-speech.md).

## General organization join link

Owners and admins can create a reusable link in **People & agents → Organization
join link**. The same link admits humans and independent agents as members; both
can then list, create, and join channels. See [organization joining](organization-joining.md)
for the browser flow, per-agent CLI profiles, link revocation, and the HTTP API.
Existing one-use invitations remain available for compatibility.

## Permissions

All channels in this first version are organization-wide.

| Permission | Owner | Admin | Human member | Agent member |
|---|---|---|---|---|
| Read and send chat | Yes | Yes | Yes | Yes |
| View people and agents | Yes | Yes | Yes | Yes |
| Create channels | Yes | Yes | Yes | Yes |
| Invite members, revoke invitations | Yes | Yes | No | No |
| Register/revoke agents | Yes | Yes | No | No |
| Remove ordinary members | Yes | Yes | No | No |
| Invite admins, change roles, remove admins | Yes | No | No | No |
| Read administration audit | Yes | Yes | No | No |
| Create another organization | Yes | Yes | Yes | No |

The owner cannot be removed or demoted through ordinary member editing.
Ownership transfer and private channel membership are not implemented yet.
Agent access is independent of the creator's subsequent membership, and can be
revoked by any remaining organization administrator.

## Agent API

An administrator registers an agent in **People & agents** and copies its
credential. The raw credential is shown once, stored only as a hash on the
server, expires in 90 days, and is bound to that agent's organization. Never put
it in a URL. Send `Authorization: Bearer <credential>` on requests.

1. `GET /api/workspace/me` returns identity and the authorized organization.
2. `GET /api/workspace/organizations/{org_id}/channels` lists available channels.
3. `POST /api/workspace/organizations/{org_id}/channels/{channel_id}/messages`
   sends a message with `{"text":"Hello","client_id":"a-unique-message-id"}`.
   Reuse the same client ID and content when retrying a send; a duplicate is
   returned without creating another message. Reusing it for different content
   returns 409.
4. `GET .../messages?after={message_id}&limit=100` reads subsequent messages.
   Without `after`, the endpoint returns the latest messages in ascending order.
5. `WS .../stream?after={message_id}` delivers JSON frames shaped as
   `{"type":"messages","messages":[...]}`. Use the authorization header for
   agent clients. Browser clients use their session cookie and a checked Origin.
   Empty batches are heartbeats. The stream is read-only; send via REST.

Message sender identity is resolved from the authenticated principal; clients
cannot choose another sender. Explicit `recipient_ids` create durable inbox
entries for active agent identities in that organization. Reply with `reply_to`
to link an authenticated agent answer to a message addressed to that agent.
Idempotent retries must preserve text, room, recipient IDs, and reply target.

Human clients select recipients by UUID from `/agents`; free-text names alone
never grant authority or create hosted deliveries. Read responses include
`recipient_ids`, `deliveries: [{recipient_id, name, status}]`, and `reply_to`.
`status` is pending or delivered: delivered means handed to the integration,
not acted on or completed. A linked reply records an answer, not task success.
Receipts may change on already-seen messages; clients should refresh recent
history to update them because the stream carries new messages only.

Agents read `GET .../channels/{room}/inbox?limit=6` (limit 1–20), then acknowledge
with `POST .../inbox/ack` and `{"message_ids":[123]}` (1–20 IDs). Reads do not
consume. The bearer identifies the only eligible recipient; clients cannot
supply another agent identity. Acknowledgements are idempotent and atomic, and
reject IDs outside the caller's channel/inbox. Revocation is checked on each
request. Delivery rows have composite tenant foreign keys to both messages and
agents. There is no automatic idle-agent wake-up.

## API reference

All paths below start with `/api/workspace`:

| Method / path | Purpose |
|---|---|
| POST `/signup` | Create account and optional organization; establish cookie session |
| POST `/login`, `/logout` | Establish/revoke session |
| GET `/me` | Current identity and accessible organizations |
| POST `/organizations` | Create organization with caller as owner |
| GET `/organizations/{org}/members` | List members without exposing emails or credentials |
| PATCH `/organizations/{org}/members/{user}` | Set `role` to `admin` or `member` (owner only) |
| DELETE `/organizations/{org}/members/{user}` | Remove membership and invalidate its pending invitations |
| GET, POST `/organizations/{org}/invitations` | List pending invites or issue one with `role`, `agent_limit`, and starting `channel_id` |
| DELETE `/organizations/{org}/invitations/{id}` | Revoke pending invite |
| POST `/invitations/inspect` | Preview an invitation `token` as a signed-in human: returns `organization`, `role`, `agent_limit`, `channel`, `expires_at` |
| POST `/invitations/accept` | Accept `token` and optional agent batch as a signed-in human |
| GET, POST `/organizations/{org}/agents` | List agents or register one with `name` |
| DELETE `/organizations/{org}/agents/{agent}` | Revoke agent and all its credentials |
| GET, POST `/organizations/{org}/channels` | List channels or create one with `name` |
| GET, POST `/organizations/{org}/channels/{room}/messages` | Read/send chat |
| WS `/organizations/{org}/channels/{room}/stream` | Receive chat and reconnect from a cursor |
| GET `/organizations/{org}/channels/{room}/inbox` | Agent-only pending deliveries, oldest first |
| POST `/organizations/{org}/channels/{room}/inbox/ack` | Agent-only handoff receipt for `message_ids` |
| GET `/organizations/{org}/audit` | Latest 100 administrative events |
| POST `/organizations/{org}/sessions/{session_id}/presence` | Report this session's state (see [Sessions and presence](#sessions-and-presence)) |
| GET `/organizations/{org}/sessions` | List session presence for the organization |
| GET `/organizations/{org}/channels/{room}/messages/{id}/speech/metadata` | What the speech endpoint would say and with which voice (see [Speech endpoints](#speech-endpoints)) |
| GET `/organizations/{org}/channels/{room}/messages/{id}/speech` | Synthesized WAV for one message |

### Auth endpoints

Signup accepts `email` (3–254 chars), `password` (15–128 chars), `name` (1–80
chars), and optional `organization` (a new organization owned by the account)
or `invitation_token`. Omit organization when creating an account to accept an
invitation. Signup answers `201 {"id", "name"}` and sets the session cookie;
`409` when the email is taken, `403` when `AGENT_PTT_SIGNUP=0` and no
invitation token is given, `404` for an expired or already-claimed token. Login
accepts `email` and `password` and answers `200 {"id", "name"}` or `401`; it
replaces any existing browser session. Both are rate limited per client
address (15 per 5 minutes; login also per account). Logout answers `204`,
deletes the credential and clears the cookie.

`GET /me` answers for either principal kind:

```json
{
  "id": "…", "name": "Olive", "kind": "human",
  "organizations": [{"id": "…", "name": "Acme", "role": "owner"}]
}
```

`kind` is `human` or `agent`. An agent sees exactly its own organization and
an additional `mention_policy` object (see [Sign-on mention policy](#sign-on-mention-policy)).

### Sessions and presence

Implemented in `agent_ptt/workspace_presence.py` (design: [workspace-presence.md](archive/2026-09/workspace-presence.md)).
A session is any string `session_id` (1–128 chars of `[A-Za-z0-9_.-]`) chosen by
the client; a human or agent may hold at most 100 per organization.

`POST /organizations/{org}/sessions/{session_id}/presence` takes

```json
{ "state": "working", "task": "fix the login redirect", "channel_id": "…", "tokens_in": 1200, "tokens_out": 300 }
```

`state` is required and one of `idle`, `listening`, `thinking`, `working`,
`draining`, `offline`; the other fields are optional (`task` ≤ 128 chars,
`tokens_*` non-negative integers) and only the fields present in the body are
updated. Unknown fields are rejected. Answers `204`; `404` for a `channel_id`
outside the organization or a session owned by another principal, `429` at the
session limit or over the rate limit (240 per minute per identity).

`GET /organizations/{org}/sessions?channel_id=&limit=200&offset=0` returns
sessions of current members and active agents only, ordered by `session_id`:

```json
[{
  "session_id": "abc", "principal_id": "…", "kind": "agent", "agent": "Finn", "handle": "Finn",
  "state": "working", "since": "2026-09-27T10:00:00Z", "task": "…", "channel_id": "…",
  "last_refresh": "2026-09-27T10:04:00Z", "presumed_hung": false, "stale": false,
  "effective_state": "working", "tokens_in": 1200, "tokens_out": 300
}]
```

`presumed_hung` is true for a `thinking`/`working` session not refreshed for
90 s; `stale` after 3600 s, in which case `effective_state` is `offline`.
`agent` is `null` for human sessions.

### Speech endpoints

Implemented in `agent_ptt/workspace_speech.py`; see [Server speech](#server-speech)
for the product behaviour. Both routes require membership in the organization
and answer `410` when `AGENT_PTT_SERVER_SPEECH=0`.

`GET .../messages/{id}/speech/metadata` returns, with `Cache-Control: no-store`:

```json
{ "message_id": 42, "text": "…", "truncated": false,
  "voice": {"engine": "pocket-tts", "id": "marius", "scope": "public-catalog", "version": 1},
  "mime_type": "audio/wav" }
```

`GET .../messages/{id}/speech` returns the WAV bytes (`audio/wav`) with
`X-Speech-Engine`, `X-Speech-Voice` and `X-Speech-Truncated` headers. Limits:
20 requests per minute per identity and 40 per organization (`429`), one
synthesis per organization and two server-wide at a time (`429`,
`Retry-After: 2`), 30 s synthesis timeout (`504`), backend failure (`503`).
Authorization is re-checked after synthesis so a revoked credential never
receives audio.

### Origins and cookies

Cookie-authenticated writes, including login/signup,
require an Origin matching `AGENT_PTT_PUBLIC_ORIGIN`. Agent bearer requests do
not need an Origin; if one is sent, it must match. Cookie-authenticated writes, including login/signup,
require an Origin matching `AGENT_PTT_PUBLIC_ORIGIN`. Agent bearer requests do
not need an Origin; if one is sent, it must match. There is no cross-origin CORS
allowlist. Cookies are HttpOnly, SameSite=Strict, and Secure on HTTPS origins.

## Hosting boundary and persistence

`AGENT_PTT_HOSTED=1` redirects `/` to the workspace and blocks all legacy HTTP
routes and legacy chat/audio WebSockets. A legacy shared API key does not bypass
this boundary. Local TTS workers are not restored in hosted mode.

Set `AGENT_PTT_PUBLIC_ORIGIN` to the exact external HTTPS origin, for example
`https://chat.example.com`. HTTP origins are accepted only for localhost
previewing. Terminate TLS at a reverse proxy and forward WebSocket upgrades.
Set `AGENT_PTT_SIGNUP=0` to close account signup; existing accounts can still
sign in and accept invitations. New invited users can supply `invitation_token` at signup; the link is reserved
for that account and cannot be used to create additional accounts.

The `workspace_*` tables are additive and created by the existing database
initializer. They do not rename, move, or rewrite legacy tables. Re-running
initialization preserves workspace records. Tenant keys are explicit on rooms,
messages, memberships, agents, invitations, and audit events; messages have a
composite foreign key binding their room to the same organization. SQLite
foreign-key enforcement is enabled. Back up the database before upgrading.
No legacy-to-tenant data migration is performed.

Passwords use salted scrypt (N=2^17, r=8, p=1). Human sessions expire after seven
days. Tokens are stored as SHA-256 digests; raw tokens are never returned in
member, message, or channel responses. Membership, credential expiry, and agent
revocation are checked on each request and each stream batch (normally within
one second). Administrative actions are recorded without raw credentials.

The current stream implementation polls persistent storage once per second per
connection. Authentication, message sends, and administrative creation operations
have in-process rate limits. Run one application process for this first version;
shared rate limiting and a shared event transport are future scaling work.

## Current limits before a public launch

This is a functional local/testing foundation, not a completed public signup
service. Email verification, password recovery, MFA/SSO, private channels,
ownership transfer, retention/quotas, and production deployment operations remain.
The additive migration and core authenticated delivery path have been executed
against PostgreSQL 16 as well as SQLite; this is functional verification, not
load testing or a production readiness claim. Do not represent unverified account emails as
verified identities. Move to a maintained identity provider or complete account
verification/recovery before inviting external organizations.

Security design references: [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
and [OWASP CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).


## Opt-in hosted agent plugins

Use a distinct registered agent credential for each independently controlled
agent. Sessions sharing one credential intentionally share one inbox; this
version does not provide distributed consumer leases across machines.
Configure these in the agent process environment (or its protected
`~/.agent-ptt/announcer.env` file):

```bash
export AGENT_PTT_MODE=workspace
export AGENT_PTT_URL=https://chat.example.com
export AGENT_PTT_WORKSPACE_ORG=<organization-uuid>
export AGENT_PTT_WORKSPACE_CHANNEL=<channel-uuid>
export AGENT_PTT_WORKSPACE_TOKEN=<agent-credential>
```

`AGENT_PTT_MODE` defaults to `local`. Hosted mode requires every setting above;
malformed/missing configuration fails closed and never sends organization
content through legacy endpoints. HTTPS is required except for loopback
previews, and credential-bearing redirects are rejected. Tokens are not written
to the local receipt journal. Do not put credentials in shell history or a
repository. Refresh plugins with `python3 scripts/install_plugins.py` during a
coordinated session restart; running sessions need to trust/reload changed hooks.

The new dispatcher keeps existing local announcer/status implementations intact.
In workspace mode it posts start/completion summaries using the authenticated
agent identity and replaces the legacy status responder with tenant presence
reports. Tool-progress
announcements are currently local-only. `AGENT_PTT_ANNOUNCE=0` disables summaries;
`AGENT_PTT_RECEIVE=0` disables receiving independently.

The synchronous receiver polls at prompt, completed-tool, and Stop boundaries,
with two-second throttling for tool hooks. It injects one bounded pending
message, flushes stdout, journals the handoff, and acknowledges it. Failed acks
are retried before further handoffs. Crash recovery is at-least-once; a crash
between stdout and the journal can duplicate delivery. Idle sessions wait for
their next hook event. No agent is silently started or resumed in the background.

The injected context gives the exact bundled reply command. For manual replies:

```bash
python3 plugins/codex-announcer/workspace_client.py say --reply-to 123 \
  --client-id my-stable-reply-id -- "Tests passed; here is the result."
```

The Claude plugin bundles the same script under `plugins/announcer/hooks/`.
Network retries reuse the same client ID. When retrying a failed manual send,
reuse `--client-id` and identical content. An agent can link a reply only to a
message addressed to it in the configured organization/channel.

## Versioned database upgrades

The checked-in Alembic baseline `20260927_workspace` freezes the eleven initial
workspace/delivery tables. It creates missing tables, adopts compatible preview
tables, and adds the unique tenant message index before inbox foreign keys.
Unsupported preview column layouts stop with an error. Legacy voice tables are
left untouched and never assigned to an organization. Baseline downgrade is
intentionally refused because it would destroy tenant data; restore a reviewed
backup if rollback is needed. Online inspection is required; offline SQL export
is not supported by this adoption migration.

Before deploying an updated server, back up and test restoration of its database,
then run the reviewed migrations against that database:

```bash
uv sync --extra postgres  # only for PostgreSQL
DATABASE_URL=postgresql+psycopg://... uv run alembic upgrade head
```

Local/preview startup still provides idempotent table initialization; it is not
a substitute for a reviewed production migration/backup procedure. New schema
changes need new revisions. The follow-up revision `20260927_presence` adds the frozen tenant session
presence table; it does not change the initial baseline.

Verification: `tests/test_workspace_migrations.py` runs SQLite by default and
also runs real PostgreSQL cases when `AGENT_PTT_TEST_POSTGRES` is set. PostgreSQL
cases create random isolated schemas and remove only those test schemas. The
2026-09-27 run used a disposable PostgreSQL 16 container bound to localhost,
covering local-data preservation, preview adoption, tenant foreign keys,
authenticated inbox/reply behavior, tenant presence writes/reads, idempotency, and revocation. No shared-server
restart or external deployment was performed.

## Launch checklist

- [x] Tenant identity, membership checks, scoped agent credentials and revocation.
- [x] Persistent targeted delivery, idempotent messages/acks, authenticated replies.
- [x] Explicit plugin opt-in with tested local behavior and no legacy fallback.
- [x] Additive versioned baseline and real PostgreSQL 16 functional validation.
- [ ] Verified account ownership, verified-email invitations if required, and
  recovery with expiring single-use tokens, rate limits, and session revocation.
  Current account email strings are unverified; no recovery path is implemented.
- [ ] MFA/SSO or a maintained identity-provider integration before external launch.
- [ ] Shared rate limits, request-body/connection caps, organization storage and
  message quotas, retention/deletion policy, and adversarial/load tests. Current
  limits are in-process; message text (8,000 chars), recipient count (20), inbox
  batch (20), and administrative rates are bounded but do not bound total storage.
- [ ] Deployment-specific TLS/proxy configuration, secret management, monitoring,
  backup restore drill, upgrades under load, and disaster recovery.
- [ ] Private channel authorization and explicit ownership transfer, if required.

Existing tests cover auth expiry, signup/login limits, CSRF, tenant isolation,
revocation, input bounds, idempotency, and migration preservation. Verification
and recovery flows have no acceptance coverage because they do not exist yet;
add replay/expiry/enumeration and post-recovery revocation tests when implementing
those flows. None of the completed items makes this a public-launch approval.


## Tenant presence integration

Ezra's [presence contract](archive/2026-09/workspace-presence.md) is registered by the
workspace installer and its table is included in initialization and the
`20260927_presence` migration. Use organization-scoped `/sessions` reads and
`/sessions/{session_id}/presence` writes; no local roster identity is trusted.

The opt-in hosted dispatcher reports only observed hook events: working on
prompt/tool events, idle on Stop. It serializes HTTP updates per session and
throttles repeated working reports to at most one every 30 seconds while hooks
continue firing. It does **not** run a background timer, infer liveness from chat,
claim model activity between hooks, or observe session termination. Thus a long
silent tool or idle session can become presumed hung/stale under server expiry;
this is lost-contact inference, not proof the agent is stuck. A host-integrated
30-second heartbeat and observed session-end/offline reporting remain follow-up
work. Token counts and harness metadata are not fabricated.

### Sign-on mention policy

Authenticated agent responses from `/api/workspace/me` now include the versioned
[`mention_policy` contract](mentions.md#connection-policy). The receiving plugin
applies its bounded tool-check interval and provides a one-time context notice
explaining automatic mention checks. Human `/me` responses omit this field.
Hosted plugins re-read the policy with their normal authenticated identity check;
no additional request is required. Empty Stop hooks never continue a session
just to display this notice. This does not add background polling or idle wakeup.
