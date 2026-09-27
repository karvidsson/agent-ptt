# Hosted product assignments — 2026-09-27

> Archived 2026-09-27: historical implementation/design record. Session assignments
> and deployment/test claims below describe that earlier work, not current instructions.
> Start with [current documentation](../../README.md) and the [next phase](../../next-phase.md).

> Status: coordination handoff, 2026-09-27. Progress per lane is reported by its owner in the channel and is not tracked here; treat the assignments below as historical.

The user asked Poppy to distribute the five recommended workstreams to existing
agents in channel `26941a15-7eb1-41d0-8dbd-099ebb305931` and get them started.
This document is the handoff contract. Existing agent sessions share this
checkout; preserve the substantial uncommitted work already present.

Product direction: one application, the original IRC-style UI, organization
membership/RBAC for humans and agents, tenant-scoped text conversations, and
optional audio for human listeners. The `/workspace/` prototype must converge
with the existing experience. Do not redesign the product again.

## Coordination

- Announce acceptance in the channel, with your task, intended files, and any
  dependency. Report completion with changed behavior, tests, and remaining work.
- Use the current session's identity; never borrow another participant's key.
- Read `docs/workspaces.md` and your referenced plan against the current code.
  Some historical status headers are stale. Last observed full suite: 389 passing.
- Work only in the files assigned to your lane. Communicate interface needs to
  the relevant owner before editing shared files. Prefer new focused modules.
- Do not revert, reset, stash, commit, or deploy other sessions' changes. Do not
  restart the shared server on port 8770. Use isolated test databases/ports.
- Poppy coordinates/reviews integration. The immediate request is dispatch and
  start; a channel receipt alone is not proof that implementation has begun.

## Ada — original UI plus organization support (priority 1)

Session: `01a0e335-fd3e-75a2-879a-63e943dad983`.

Own `agent_ptt/static/index.html`, `workspace.html`, `workspace.css`,
`workspace.js`, and focused UI tests/docs. Preserve the existing original visual
language, channel layout, roster and voice controls. Incorporate organization
selection and account/member/agent management in that style. Keep local mode
working and connect hosted behavior to the existing workspace API. Coordinate
API needs with Olive; never silently send tenant requests to legacy endpoints.
Do not remove legacy capabilities just because hosted equivalents are pending.

Acceptance: existing local UI remains usable; hosted signup/signin, org switching,
channels, and administration work in the original visual style; tenant state is
cleared on org switches; browser checks include errors and small-screen layout.
Report any audio/presence controls awaiting the other lanes.

## Olive — tenant agent delivery and shared backend integration (priorities 1, 3)

Session: `01a0e327-63ff-7f00-828b-66995f005c89`.

Own `agent_ptt/workspace.py`, `workspace_auth.py`, `workspace_models.py`, shared
server/db integration points, and new tenant-delivery modules/tests. Own receiving
plugin changes, but coordinate with Hazel before editing existing announcer,
status, or session-routing helpers. Reuse the durable mention/inbox semantics in
`docs/mentions.md`, bound to authenticated organization/agent identities. Connect
existing agents to the workspace API through explicit opt-in configuration;
keep local plugin behavior working. Do not claim that hook delivery wakes an
idle agent. Make delivered versus acted-on/answered semantics explicit.

Acceptance: authorized human targets an agent, only that tenant/agent receives
the message, retries do not lose it, acknowledgements cannot cross recipients,
and replies carry authenticated agent identity. Test cross-tenant access,
revocation, and reconnect/retry behavior. Coordinate API contracts with Ada.
Integrate Ezra/Milo modules through small reviewed registration changes.

### Olive follow-on — hosting foundation (priority 5)

After the delivery/API contract settles, own the migration/Postgres validation
slice and update the launch checklist in `docs/workspaces.md` and `org-auth.md`.
Preserve existing local data; do not silently assign it to an organization.
Document and test remaining account verification/recovery and resource-limit
requirements. Keep provider selection and external deployment out of this slice;
report what is still needed rather than claiming public-launch readiness.

## Ezra — tenant-scoped presence (priority 2)

Session: `01a0e3be-562a-7a83-94b0-6b19225b945a`.

Own new `agent_ptt/workspace_presence.py`, its tests, and presence documentation.
Use the existing Base, DB, and principal/role helpers; keep new table declarations
inside your module. Expose a router/installation function for Olive to register.
Implement authenticated session presence, server timestamps, heartbeat expiry,
and working/idle/offline views based on `docs/plans/presence-and-attention.md`.
Agent-reported state and inferred stale status must remain distinguishable.
Do not let clients update another identity or read another organization's roster.
Coordinate heartbeat-client changes with Olive/Hazel, and UI fields with Ada.

Acceptance: deterministic time-based tests, cross-tenant authorization, an agent
only updating its own sessions, and no inference of model activity from ordinary
chat messages. Report exact integration hooks and the client contract.

## Milo — interchangeable speech delivery (priority 4)

Session: `01a0e23f-4588-7221-83de-4ac55ee401d1`.

Own new `agent_ptt/workspace_speech.py`, a separate client speech-adapter module,
focused tests, and `docs/plans/client-speech.md`. Keep message persistence and
text delivery independent of speech. Implement a tenant-authorized server speech
path using existing TTS interfaces, with bounded work and clear voice metadata;
provide a client playback adapter Ada can attach without editing Ada's files.
Do not expose global voice profiles across tenants. Coordinate model/route
registration with Olive instead of modifying shared files directly.

Investigate a browser Pocket TTS provider as a separate compatibility experiment;
verify supported browsers, model download/memory, latency and voice portability.
Do not label unmeasured performance as verified or make browser synthesis a
prerequisite for chat. Server speech and browser synthesis should implement the
same conceptual text-plus-voice playback contract, with cancellation/mute and
message ordering. Avoid duplicate playback during fallback.

Acceptance: text chat still succeeds when synthesis fails, tenant authorization
on audio, bounded speech work, fake-TTS tests, and documented browser findings.
No public deployment or claims of synchronized audio between listeners.
