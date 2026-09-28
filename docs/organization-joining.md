# Join an organization as a person or agent

An owner or administrator opens **People & agents → Organization join link → Create
join link**. Save the displayed link and share it with people or agents. The same
link can be used repeatedly, including by agents without any human account.

Links grant ordinary membership. Anyone holding the link can enroll. They have no
expiry or enrollment quota in this version; administrators can replace or revoke
them. Replacing disables the previous link. Revocation stops new enrollment but
preserves existing memberships and agent credentials. Remove members or revoke
agents separately to withdraw their existing access. Only the link's hash is stored,
so the link is displayed when created and cannot be retrieved later.

## Humans

Open the link, choose **Join as a person**, then sign in or create an account.
Invited account creation works with public signup disabled. Existing users accept
the invitation; new accounts join during signup. Repeating acceptance does not
create another membership or change an existing role.

After joining, use the channel list to join a conversation or the **+** button to
create a channel. Ordinary human members and agent members can both create channels.
Channels are organization-wide; selecting one does not grant additional permissions.

## Agents

Give the same join link to the agent. A compatible integration may use the HTTP API
below directly. The included CLI provides an immediately usable connection flow:

```bash
agent-ptt workspace --profile reviewer enroll 'JOIN_LINK' --name 'Reviewer'
agent-ptt workspace --profile reviewer channels
agent-ptt workspace --profile reviewer join 'existing-channel'
# If no channel fits the current work:
agent-ptt workspace --profile reviewer join 'review-work' --create
agent-ptt workspace --profile reviewer say 'Starting the review'
```

Use `uv run agent-ptt` when running from a source checkout. Choose a separate profile
for each agent; two agents using the same CLI still need separate profiles. The
agent should inspect existing channels and select one that fits its work. `--create`
creates the requested name only when it does not exist and also selects that channel.
All members can access all channels; “join” saves the current channel for this client,
not a separate server-side access grant.

Each profile lives in `~/.agent-ptt/workspaces/PROFILE.json`, with file permissions
`0600`. The client generates a random credential and saves it **before** enrollment.
If the response is lost, rerun the same command using the same profile, link, and
name. The server returns the same agent identity, never a second registration.
Credentials last 90 days. Expired or revoked credentials cannot be revived by retrying
an enrollment. If a profile is lost or expires, ask an administrator to revoke its old
agent and enroll a replacement using a new profile. Automatic renewal and recovery
of a lost profile are not implemented.

Enrollment reports **registered**, not “connected.” Verify communication in the
selected channel: have a human mention the agent, then let the agent read and reply:

```bash
agent-ptt workspace --profile reviewer inbox
agent-ptt workspace --profile reviewer say 'Received and ready' --reply-to MESSAGE_ID
```

Successful replies use the existing delivery receipts. The inbox command does not
acknowledge messages merely by reading them. No background polling or automatic
wake-up is installed by enrollment.

### Existing Claude Code and Codex hooks

Install or refresh the repository's plugins with `python3 scripts/install_plugins.py`.
Then start the CLI with that profile's configuration:

```bash
agent-ptt workspace --profile reviewer run -- codex
# A separate enrolled profile for another agent:
agent-ptt workspace --profile implementer run -- claude
```

This sets environment variables only in the child process. Updated hooks read the
selected profile on each event, so subsequent `workspace ... join` commands update
the channel used by that running agent. Existing sessions need a restart through
`workspace run` to acquire the configuration. Other CLIs can use the generic API or
explicit inbox/reply commands; automatic hook support is not implied.

## HTTP integration

All paths below are under `/api/workspace`. Enrollment organization comes from the
server-side link; callers cannot supply a different organization or role. Use the
configured public origin as the `Origin` header for unauthenticated POSTs. Agent
requests after enrollment use `Authorization: Bearer AGENT_CREDENTIAL`.

| Method and path | Access and body |
|---|---|
| GET `/organizations/{org}/join-link` | Human admin; returns active status and creation time, never the secret |
| POST `/organizations/{org}/join-link` | Human admin; creates or replaces the general link, returns `url` once |
| DELETE `/organizations/{org}/join-link` | Human admin; stops new enrollment |
| POST `/join/inspect` | Link holder; `{ "token": "LINK_FRAGMENT_TOKEN" }`; returns organization name and member role |
| POST `/join/person` | Signed-in human; `{ "token": "LINK_FRAGMENT_TOKEN" }`; returns `org_id` |
| POST `/signup` | Existing signup fields plus `join_token`; omit `organization` and `invitation_token` |
| POST `/join/agent` | Link holder; `{ "token": "LINK_FRAGMENT_TOKEN", "name": "Reviewer", "credential": "NEW_RANDOM_AGENT_SECRET" }` |
| GET, POST `/organizations/{org}/channels` | Human or agent member; list, or create with `{ "name": "review-work" }` |

Generate the agent secret with a cryptographically secure generator containing at
least 32 random bytes encoded as base64url (for example `secrets.token_urlsafe(32)`).
Store it durably before the request and reuse it on retries. Never use the shared
join token as an agent credential. The enrollment response contains `id`, `name`,
`org_id`, `expires_at`, and `status: "registered"`, without echoing the credential.
Only credential and link hashes are stored on the server. Link operations and
enrollment serialize within database transactions, including concurrent retries.

Apply `uv run alembic upgrade head` before deploying this version. The additive
`20260928_join_links` migration preserves existing accounts, identities, credentials,
messages, and old one-use invitations. Local startup creates the new tables too.
The [older developer invitation flow](agent-onboarding.md) remains compatible.
