# Plan: Running Agent PTT for an organization (auth, tenancy, deployment)

> Archived 2026-09-27: historical implementation/design record. Session assignments
> and deployment/test claims below describe that earlier work, not current instructions.
> Start with [current documentation](../../README.md) and the [next phase](../../next-phase.md).

> Implementation update: the self-service multi-organization workspace foundation
> now exists in the same app. See [implemented behavior and remaining work](../../workspaces.md).
> The older single-organization-first recommendation below is superseded by the
> decision to support organization signup.

> Status: historical proposal, 2026-09-27. **Step 0 shipped** (`agent_ptt/auth.py`); the
> later steps are superseded by the workspace implementation above.
>
> Step 0 = perimeter auth: one shared `AGENT_PTT_API_KEY` gating every REST
> route and both WebSockets (`agent_ptt/auth.py`, `tests/test_auth.py`),
> plus an `X-Confirm-Delete-All: yes` header on `DELETE /channels`. The CLI,
> the example scripts, `examples/crew/hook.py`, the web UI and the announcer /
> voice plugins all send the key from the same env var as a Bearer header (the
> UI prompts and keeps it in localStorage).

## Current hosted launch status (2026-09-27)

Tenant-owned text chat now has explicit agent recipients, durable inboxes,
recipient-only acknowledgements, authenticated linked replies, and an opt-in
hosted plugin transport. Delivered records hook handoff, not acted-on/completed
work; idle agents still wait for a hook event. An additive Alembic baseline has
been exercised on SQLite and real PostgreSQL 16 without assigning legacy data
to tenants. See [the launch checklist](../../workspaces.md#launch-checklist).

Email ownership verification, account recovery, MFA/SSO, shared resource limits,
retention, and production backup/deployment operations remain open. The older
single-organization/perimeter steps below are historical design notes, not the
current implemented behavior or a claim of public-launch readiness.

## Current architecture decision

The product is multi-tenant: one hosted server and shared PostgreSQL database
serve many organizations. Each organization owns its channels, agents, messages,
mention inboxes, and presence records. Human accounts may join multiple
organizations, with independent membership roles. Agent credentials are bound
to one organization. The existing workspace implementation supplies these
boundaries; see [the current tenancy contract](../../workspaces.md).

The deployment is shared across organizations. It does not require one server,
database, or cloud account per customer. The earlier single-organization-first
recommendation is retired. Public-service hardening remains tracked above.

## Historical assessment before workspace implementation

The sections below describe the original local-only application and the plan
that led to the workspace implementation. Statements about missing authentication
or ownership below are historical, not a description of current hosted mode.

### Original local-only gaps

From `agent_ptt/server.py`:

- Channel create, list, delete, and delete-all have no auth at all.
  `DELETE /channels` wipes everything.
- The agent WebSocket (`/channels/{id}/ws`) accepts `key=None` and joins the
  caller as `anonymous`. The key travels as a query string, so proxies and
  access logs record it.
- The spectator audio stream (`/channels/{id}/audio`) and all voice-profile
  CRUD (`/voices/profiles*`, `/voices/pinned*`) are unauthenticated by design.
- Channel responses expose participant keys. These must become identifiers,
  not credentials that grant authority to anyone who reads a response.
- Channels are persisted and restored, but live state, queues, and connections
  belong to one process. Neither storage nor live state has organization ownership.

## Target design

### 1. Two kinds of principals

- **Humans** authenticate via the organization's identity provider using
  OpenID Connect (Entra ID, Google, Okta). Validate the login flow and establish
  a server-side browser session. API access tokens must be validated for their
  issuer, audience, signature, and expiry; ID tokens are not API bearer tokens.
- **Agents** get API tokens minted by an authorized human. Tokens are stored
  hashed in the DB, scoped to an org and optionally to specific channels, with
  expiry and revocation.
- A FastAPI dependency resolves a bearer into
  `Principal(org_id, user_id, role, scopes, acting_for)` and every route takes
  it. WebSockets resolve the same principal from a header or the first frame.

Decide before implementing: does an agent act as its own identity or on behalf
of the human who launched it? On-behalf-of is simpler for audit and matches how
the Claude Code and Codex hooks run on a developer's machine. Standalone
service identities matter once agents run in CI or sandboxes. The token model
should allow both via an optional `acting_for` user id.

### 2. CLI login

- `agent-ptt login` uses browser-assisted login, with device authorization where
  the provider supports it. Store credentials in the OS credential store and
  keep non-secret preferences such as the current org in the session file.
- Agents and hooks read `AGENT_PTT_TOKEN` from the environment, matching how
  the announcer plugin already reads `AGENT_PTT_URL`.
- `agent-ptt token create|list|revoke` for admins/members to manage agent
  tokens without the web UI.

### 3. Participation identities and protected streams

- `join` still issues a participation key, but only after a valid bearer, and
  the key is bound to the principal.
- Authenticate requests with a separate credential; participation keys alone
  must not authorize sending, even if exposed in channel responses.
- Protect both WebSocket endpoints, including read-only audio. Browser clients
  use secure HttpOnly session cookies with origin checks; non-browser clients
  can use an authorization header. If first-frame authentication is used, enforce
  a short timeout and send no channel data before authentication succeeds.
- Use CSRF protection for cookie-authenticated writes. Recheck permissions and
  close active streams when sessions expire or access is revoked. Avoid
  long-lived credentials in URLs.

### 4. Tenancy and roles

- Add `org_id` to channels, messages, voice profiles, pinned voices, and
  tokens. Every query filters by the principal's org.
- Roles:
  - **admin**: manage tokens and members, delete channels, edit org-wide
    voice profiles.
  - **member**: create channels, join, speak, listen, manage own profiles.
  - **listener**: read transcripts and listen to authorized channels.
- Agents are service or delegated identities with explicit scopes, not a human
  membership role. They join and speak only where their token permits it.
- Private channels carry a member list; org-wide channels are open to all
  members of that org.
- Scope both database queries and in-memory lookups to verified membership.
  Voice pins currently keyed globally by handle need organization-scoped keys.

### 5. Storage and migrations

- Use persistent storage with backups. Prefer managed Postgres when shared
  database operations are needed; it is not a prerequisite for auth. Verify the SQLAlchemy layer against it
  (JSON columns, `db.merge` upserts) in CI.
- Author the first Alembic migration. `migrations/` is scaffolded but empty,
  and adding org columns is exactly when relying on `create_all` stops working.

### 6. Deployment shape

- Run the hub headless behind a TLS reverse proxy (Caddy or nginx). The mute
  path and `AGENT_PTT_MUTE=1` already exist.
- Initially stream audio to browsers and CLI listeners. Optional per-office
  audio nodes are a later step in the
  [distributed-channels roadmap](distributed-channels.md).
- Add rate limits per principal and a message length cap. TTS is the expensive
  resource and any token holder can burn it.
- Bound TTS and listener queues and limit concurrent synthesis so one agent
  cannot overwhelm the shared service.
- Run one application process initially. Database-backed tenancy does not
  remove the in-memory queue/connection constraint; replicas need shared
  messaging and coordinated workers.

### 7. Audit

- Messages already archive `sender_key` and `handle`. Add the principal id and
  `acting_for`.
- Log token creation and revocation, channel deletion, and profile edits to an
  `audit_log` table.
- Provide an admin UI for membership, credentials, retention, and usage limits.
  Removing a member must revoke their sessions and delegated credentials.

## Phased path

| Step | Scope |
|------|-------|
| 0. Perimeter key (done except plugins) | Single shared `AGENT_PTT_API_KEY` on all REST/WebSocket paths, delete-all confirmation header. Server, CLI, examples and web UI done; plugins pending. |
| 1. Identity and ownership | OIDC integration, users, organizations, memberships, service identities, tokens, organization ownership, and schema migrations. |
| 2. Endpoint permissions | Shared principal resolution, roles/channel permissions on every REST and WebSocket path, protected audio, and removal of participation-key authority. |
| 3. CLI and plugins | Browser-assisted login, secure credential storage, token management commands, authenticated hooks, and HTTPS/WSS support. |
| 4. Administration and deployment | Admin UI, revocation, retention, audit history, limits, backups, and one hosted process behind HTTPS. |
| 5. Distribution, when needed | Shared messaging and separate TTS/audio workers based on measured load or per-office playback needs. |

Auth and authorization must ship before organization data is exposed publicly;
distribution is not a prerequisite. Estimates depend on the chosen identity
provider and whether the first release supports one company or self-service tenants.

## Acceptance checks

- Anonymous callers cannot manage channels, read transcripts, or listen.
- A member cannot read, hear, impersonate, or delete outside their permissions,
  including by guessing another organization's IDs or reusing participation keys.
- Restricted channels and custom voices remain isolated across organizations.
- Expired/revoked tokens and removed memberships stop both new requests and
  existing streams; hooks report no credentials in logs.
- Browser writes and WebSocket connections reject invalid CSRF/origin context.
- CLI and hooks work over HTTPS/WSS; queue limits contain noisy producers.
- Migrations preserve existing data under an explicitly assigned organization,
  and backup restoration is verified.

## References

- [Device authorization flow](https://auth0.com/docs/get-started/authentication-and-authorization-flow/device-authorization-flow)
- [OWASP WebSocket security guidance](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html)

## Docs to update when implementing

- `docs/api-reference.md`: auth headers, new token endpoints, error codes
  (401/403).
- `docs/cli-reference.md`: `login`, `token`, `AGENT_PTT_TOKEN`.
- `docs/database.md`: new tables and `org_id` columns, migration workflow.
- `docs/architecture.md`: principal resolution and tenancy.
- `docs/installation.md`: reverse proxy and IdP configuration.

Related: [presence and attention](../../plans/presence-and-attention.md) for session state, heartbeats and message polling; [Copilot and Antigravity](../../plans/copilot-antigravity.md) for the other hosts.
