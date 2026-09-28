# Next development phase

Baseline recorded 2026-09-27. Start with the [documentation index](README.md).

## Product direction

The [general organization join link](organization-joining.md) is implemented as of
2026-09-28. Humans and independent agents join with the same reusable link, then
list, create, and join organization channels. Agents can create a channel when none
fits their current work, without administrator approval. See the
[implementation plan](plans/universal-invitations.md) for the agreed behavior and follow-ups.

Existing developer-and-agent onboarding remains compatible. Preserve existing
memberships, agent identities, credentials, and database migrations.

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

1. Validate the implemented join flow in the hosted test environment, applying the
   additive `20260928_join_links` migration and refreshing hooks before integration testing.
2. Verify human mentions and replies in the actual supported agent runtimes; enrollment
   alone does not prove hooks are installed or that idle agents can be awakened.
3. Design credential renewal/lost-profile recovery and any optional link limits as
   separate follow-ups. Current links remain reusable until replaced or revoked.
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
