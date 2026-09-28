# Roadmap

See the [next-phase handoff](../next-phase.md) for the working baseline and priorities.

| Direction | Status |
|---|---|
| [Universal invitations for people and agents](../plans/universal-invitations.md) | Core reusable join flow implemented; see [usage](../organization-joining.md) |
| [Production hosting and tenancy](../workspaces.md) | Test foundation implemented; public-launch gaps remain |
| [Client-side Pocket TTS](client-speech.md) | Deferred; benchmark and prototype before rollout |
| [Horizontal scaling for hosted mode](horizontal-scaling.md) | Deferred until the user base outgrows one container; replicate, don't shard |
| [Other open proposals](../plans/README.md) | Partially implemented or awaiting design decisions |

Speech currently uses server-side Pocket TTS only. Catalog selection, stable voice assignments,
and local reference-audio cloning are supported; see [voices](../voices.md).
Keep the existing [developer-and-agent onboarding](../agent-onboarding.md) working until its
universal replacement is built. Historical designs live in [the archive](../archive/README.md).
