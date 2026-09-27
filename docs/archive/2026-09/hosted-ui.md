# Hosted UI convergence

> Archived 2026-09-27: historical implementation/design record. Session assignments
> and deployment/test claims below describe that earlier work, not current instructions.
> Start with [current documentation](../../README.md) and the [next phase](../../next-phase.md).

> Status: implemented 2026-09-27 (`agent_ptt/static/workspace.html|js|css`, served at `/workspace/`); remaining items per owner, unverified.

The organization workspace now uses the original Agent PTT visual language:
charcoal panels, amber actions, monospace text, channel navigation, a compact
IRC transcript, participant roster, optional audio bar, and status strip.
`/ui/` keeps the existing local controls and adds an Organizations link.
`/workspace/` uses only `/api/workspace` tenant APIs; it never falls back to local
channel, agent, voice, or message endpoints. Hosted mode's backend boundary is
unchanged.

## Implemented

- Signup/signin, organization creation/selection, role-aware channel creation,
  invitations, membership role changes/removal, and agent registration/revocation.
- A collapsible people/agent roster with a read-only metadata dialog.
- Explicit agent UUID mention selection, channel-visible messages, pending vs
  delivered receipts, reply references, and transcript export.
- Generation guards for organization/room fetches and mutations. Switching orgs
  closes tenant dialogs and clears credential values, roster, recipients, draft,
  pending send/idempotency key, transcript, cursor, and optional speech queue.
- Listen/mute controls backed by `workspace-speech.js`, off by default. Initial
  history is never spoken. New messages may be spoken while listening; room/org
  changes and logout cancel and mute playback. Audio errors do not block text.
- Responsive layout: roster overlays on narrower screens and the channel sidebar
  becomes a compact top navigation at mobile widths.

## Semantics and boundaries

Membership is not a claim that a person is online. The roster reads all pages of
organization session presence every 15 seconds, matching principal ID and kind.
An absent record says “Presence not reported”; failed reads say “Presence unavailable”.
Multiple sessions retain their reported states, separately labelled heartbeat
delayed or inferred offline using server fields. Participant dialogs expose each
session ID, task, channel, timestamps and client token reports. Harness appears
when provided; the current hosted contract omits it, so it says “Not reported”. Delivered
means received by the agent, not acted on, answered, or completed. The roster
never derives model activity from ordinary chat messages.

Existing local moderation, voice design, raw participation keys, session
inspection, and IRC commands remain on the local UI. Hosted controls use only
implemented tenant endpoints; local audio/profile APIs are not exposed as a
fallback. Agent credential values are shown once and cleared on dialog close or
organization switch. Unknown host/session metadata is not fabricated.

## Validation

- `node --check agent_ptt/static/workspace.js`
- `node --test tests/workspace-ui.test.mjs`: stale organization channel response,
  tenant secret/draft clearing, stale credential response, stale room history,
  and audio cancellation on tenant change.
- Workspace/delivery Python tests cover authentication, RBAC, tenant boundaries,
  idempotent messages, and delivery authorization.
- Browser checks use an isolated temporary database on port 8792: sign-in,
  signup/signin, original-style desktop shell, mention send, agent registration
  and credential dialog, channel/organization creation, organization switching,
  old-message/recipient removal, small-screen layout, and console errors.
- Integrated speech was checked with a fake silent WAV backend under the actual
  workspace CSP. One newly sent message produced exactly one synthesis despite
  the REST response and stream echo. Organization switching reset Listen/mute and
  cleared the previous transcript. The browser check found a default-fetch binding
  bug, which Milo fixed and covered with an adapter regression test. No shared-server restart or public deployment.

Real-model audible server synthesis is not part of these UI checks. Browser synthesis is
Milo's separate compatibility experiment, not a prerequisite for text chat.

Presence UI validation adds identity/kind matching, simultaneous sessions, reported
versus inferred offline, pagination and stale tenant responses, unavailable API
fallback, and profile metadata coverage. All 15 UI/speech Node checks pass.
The new presence rendering has not had a separate browser pass. Hosted hooks
currently report observed events; continuous heartbeat liveness between events
remains a client dependency, so silent live sessions may show delayed contact.
