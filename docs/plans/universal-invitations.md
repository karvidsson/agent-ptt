# Universal invitations for people and agents

> Status: product direction agreed, 2026-09-27. Planning only. Detailed design and
> implementation require approval. No universal invitation flow has been built.

## Decision

An administrator creates **Invite to organization**, not **Invite a developer**.
The same invitation link works for people, individual agents, and multiple agents.
The administrator should not need to know the recipient's CLI or orchestration tool.

This replaces the direction of the developer-specific batch invitation and private
Python launcher prototype. Do not extend that prototype as the next onboarding design.
Keep the current onboarding working until the replacement is built, as explicitly requested.
Preserve existing accounts, agents, and database migrations during that transition.

A local connector, a Herdr-specific adapter, and mandatory human ownership are not
approved architectural requirements; evaluate them only as possible integrations.

## Intended experience

### Administrator

1. Create one organization invitation.
2. Set expiration and separate limits for human joins and agent registrations.
3. Share the link through the administrator's chosen channel.
4. See remaining capacity and registrations; revoke unused invitation access when needed.

Default access is ordinary membership. Invitations must not silently grant administrator
permissions. The starting channel, if offered, must be distinguished from access scope.

### Person

Open the invitation in a browser, choose **Join as a person**, and sign in or create
an account. Redeem one human slot and join the organization. Invited account creation
must work when public signup is closed. Email verification and account recovery remain
separate launch requirements.

### Agent

Consume the same link through a supported integration and choose **Connect an agent**.
Register an individual identity and exchange the invitation for that agent's own
credential. Connecting an agent must not require registering a human first.

The invitation is a temporary enrollment capability, never a permanent API credential.
Each agent must receive a test mention and reply before the product reports it as
connected. Registration, process detection, and working two-way communication are
separate states.

### Eight agents in Herdr

The administrator creates one link permitting **one human join and eight agent
registrations**. The person uses it in the browser; her agents use the same link through
the supported connection flow. Alternatively, only agents can redeem it if that is the
intended use. Unused human capacity must not block agent enrollment, or vice versa.

Each agent has its own identity and revocable credential, including when several use
the same CLI. A shared organization agent token is not an acceptable shortcut.

## Design work to approve before implementation

1. Prototype the agent entry point: paste a link into an agent conversation, run a
   connection command, or connect through Herdr. Establish what the runtime can
   actually configure, and which sessions require restart.
2. Map supported integrations for Claude Code, Codex, and other CLIs. Identify hook,
   MCP, and generic API possibilities without claiming universal automatic support.
3. Design the human/agent choice, enrollment progress, mention/reply test, expiration,
   exhausted-link, duplicate-registration, revoked-access, and recovery screens.
4. Specify stable agent identity versus a runtime session. Reconnection should not
   create duplicate agents or consume a fresh slot for the same completed enrollment.
5. Decide credential storage, renewal, lost-response recovery, and per-agent revocation.
   Avoid requiring users to manage batches of plaintext tokens or custom launch scripts.
6. Specify the relationship between an agent, the invitation that enrolled it, a human
   administrator, and an optional machine. Record provenance without requiring a human
   account for every agent or treating self-reported CLI metadata as verified identity.
7. Define migration from the existing human-only invites and developer-batch prototype.
   Preserve accounts, agent identities, messages, credentials, and applied migrations.

## Access and concurrency requirements

- Derive organization and allowed access from the server-side invitation, not caller input.
- Store invitation secrets as hashes; avoid putting credentials in logs or analytics.
- Atomically enforce expiration, revocation, and separate remaining-use counters.
- Handle simultaneous redemptions and retries without over-allocation or partial enrollment.
- Agent credentials cannot enroll more agents or grant themselves elevated access.
- Revoking an invitation blocks new enrollment. Explicitly define the separate action
  for revoking agents or people already enrolled through it.
- A shareable link grants enrollment to its holder within its limits. The UI must explain
  this; email binding or administrator approval, if needed, is a separate design choice.
- Shared onboarding needs a reachable HTTPS service. The current Proxmox localhost
  tunnel is a test arrangement, not a distribution mechanism for external invitees.

## Acceptance criteria for the eventual implementation

- One link can admit a person and eight separately identified agents across supported CLIs.
- Agent-only onboarding succeeds without a human sign-in step.
- Human joins and agent joins consume only their corresponding capacity.
- Expired, revoked, and exhausted invitations fail clearly and safely.
- Retries and concurrent enrollment do not duplicate identities or exceed the configured limit.
- A registered agent is not labeled connected until a mention/reply test succeeds.
- One agent can be revoked without disrupting the other seven.
- Unsupported integrations clearly explain what remains manual.
- Existing data and current message/speech behavior survive the change.

## Out of scope

Implementing this plan during the cleanup, redesigning speech, adding more TTS engines,
automatically messaging invitation links to recipients, and deploying public access.
Server-side Pocket TTS remains the current speech implementation. Client inference stays
in the [deferred speech roadmap](../roadmap/client-speech.md).
