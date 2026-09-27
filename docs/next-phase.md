# Next development phase

Baseline recorded 2026-09-27. Start with the [documentation index](README.md).

## Product direction

The next onboarding design is [one invitation link for people and agents](plans/universal-invitations.md).
The direction is agreed; the detailed design needs approval before implementation.
Do not build a developer-specific invitation or require a particular CLI or local connector.

**Keep the existing developer-and-agent onboarding working until its replacement is built.**
This is an explicit product decision. Its [instructions](agent-onboarding.md) document the
current implementation, not the intended future experience. Preserve existing memberships,
agent identities, credentials, and database migrations when replacing it.

Speech generation stays on the server using Pocket TTS. Browser generation is
[deferred](roadmap/client-speech.md); voice selection and local reference cloning remain supported.

## Working foundation

- Local channels, CLI commands, browser playback, and Claude Code/Codex announcement hooks.
- Hosted organizations, human authentication, roles, agent credentials, and tenant-scoped APIs.
- Durable agent mentions and hook-based delivery. An idle CLI is not automatically awakened;
  delivery depends on its integration and lifecycle events. See [mentions](mentions.md).
- Hosted presence and server-side speech with a browser playback adapter.
- Container deployment with PostgreSQL and a [Proxmox test environment](proxmox-testing.md).

The hosted service remains a testing foundation. Consult the
[workspace launch checklist](workspaces.md) and [deployment guide](deployment.md)
before planning public production use. Do not infer production readiness from archived plans.

## Where to work next

1. Review the universal invitation experience, including exactly how an agent consumes a link.
2. Approve identity, invitation capacity, credential recovery, and integration behavior.
3. Implement the approved replacement with migration and compatibility coverage.
4. Work through public-launch gaps separately; keep client speech deferred until benchmarked.

Other open proposals are indexed under [plans](plans/README.md). Old workstream assignments
and completed or superseded designs live in [the archive](archive/README.md).
They are historical context, not instructions for the next development session.

## Cleanup boundary

This cleanup reorganizes documentation, removes redundant roadmap stubs and an unused
engine package marker, and clears generated caches. It preserves application behavior,
the existing onboarding, database history, local database, saved voice assets, development
environment, machine settings, and all unrelated working-tree changes. It does not redeploy
or reset the Proxmox service, rewrite Git history, or create a clean Git baseline by discarding work.

Use the checks in [testing](testing.md) before the next implementation handoff.
