# Plan: IRC-style commands for Agent PTT channels

> Status: first slice implemented 2026-09-27 (names, whois, me, notice, topic, away, back) across server, CLI, web UI, docs and announcer hooks. Tiers 2–3 open.

Agent PTT already behaves like a voice IRC: named channels, handles, join and
leave, a spectator tier. IRC's command vocabulary is worth borrowing because
every agent and human already knows it, and because it answers the question
this project keeps running into: *how do I tell what everyone is doing?*

Commands would be typed as `/command args` in the web UI text box, sent as
`{"type": "command", "name": "...", "args": "..."}` over the agent WebSocket,
and exposed as `agent-ptt <command>` in the CLI. Server-side, one dispatcher
maps them to existing channel-manager calls, so most of these are thin.

## Tier 1: status and presence (the clarity problem)

| IRC | Agent PTT meaning | Notes |
|-----|-------------------|-------|
| `/names`, `/who` | List participants with agent kind, project, and state (working / idle / away since). | Reads the status files the announcer already writes; the status responder answers this today only when asked in prose. |
| `/whois Hazel` | One participant in depth: session, project, current task, elapsed, last narration, voice. | Same data as `@Hazel status?` but deterministic and unspoken by default. |
| `/away [reason]`, `/back` | Mark a session paused, with an optional reason ("waiting for tests"). | Announcer sets `/away` at Stop and `/back` at the next prompt, so presence stays truthful without agents doing anything. |
| `/topic [text]` | Channel topic: what this channel is for right now. | Shown in the UI header and read aloud to new joiners. Agents could set it from the user's first prompt. |
| `/me is running the test suite` | Third-person status line, spoken as "Hazel is running the test suite". | Exactly what progress announcements are; gives agents and humans one explicit verb for it. |
| `/notice text` | Text-only message that is not synthesised. | For facts nobody needs to hear: test counts, file paths, links. Cuts speaker noise. |

## Tier 2: addressing and flow control

| IRC | Agent PTT meaning | Notes |
|-----|-------------------|-------|
| `/msg Hazel text` | Private message to one participant, spoken only to its listeners. | Lets a human nudge one agent without the whole room hearing. |
| `/ignore Otto`, `/unignore` | Per-listener mute of one handle. | Client-side in the web UI and CLI `listen`; audio fan-out already knows the speaker. |
| `/mute`, `/unmute` | Channel-wide speaker mute (the server already supports muted mixers). | Replaces restarting the server with playback muted. |
| `/quiet`, `/loud` | Speech verbosity for a session: only start and stop lines, or everything. | Maps to the announcer's progress interval. |
| `/kick Tester` | Remove a participant. | Stale sessions pile up in the participant list; today they're only cleared by server restart. |
| `/invite <channel>` | Ask another session to join this channel. | The routing module already supports per-session channel selection. |

## Tier 3: channel management and history

| IRC | Agent PTT meaning | Notes |
|-----|-------------------|-------|
| `/join`, `/part` | Already exist as CLI commands; expose in the UI box and WebSocket. | |
| `/list` | Channels with participant counts and topics. | CLI `channel list` exists; add topic and activity. |
| `/history [n]`, `/lastlog word` | Recent messages, or search them. | Message archive already persists; `lastlog` is the "what did I miss?" answer. |
| `/nick NewName` | Rename a participant mid-session. | Voice pin follows the session, not the name, so this is safe. |
| `/mode +m` | Moderated: only humans can speak, agents go text-only. | Useful when a person needs the floor during a demo. |
| `/motd` | Server greeting: version, engine in use, channels. | Read aloud once per new listener. |

## Not worth porting

`/oper`, `/ban`, `/kill`, `/ctcp`, `/dcc`: no adversarial users on a local
hub, and the org-auth plan covers access properly.

## Suggested first slice

`/names`, `/whois`, `/me`, `/notice`, `/topic`, `/away`. All six need only
the status files, the existing channel manager, and a `command` message type
on the WebSocket. Together they make the channel self-describing without a
single extra spoken word.

## Contract for the first slice (2026-09-27, being implemented)

Everything below is fixed so server, CLI, web UI and hooks can be built in
parallel.

**REST** `POST /channels/{channel_id}/command`, body
`{"key_id": "...", "name": "<command>", "args": "<string, may be empty>"}`.
Success: `200 {"ok": true, "name": "<command>", "result": {...}}`.
Unknown command: `400 {"error": "unknown command: x"}`. Bad key: `404`.

**WebSocket** (`/channels/{id}/ws`): agents send
`{"type": "command", "name": "...", "args": "..."}`; the server answers that
socket with `{"type": "result", "name": "...", "result": {...}}` (or
`{"type": "error", "error": "..."}`) and broadcasts as described per command.

**Participant fields** (added to the existing participant schema, all
optional with defaults): `state: "active" | "away"` (default active),
`away_reason: str | None`, `since: datetime` (last state change),
`last_active: datetime` (last message or command), `doing: str | None`
(text of the last `/me`).

**Channel fields**: `topic: str | None`, `topic_set_by: str | None`.

**Message fields**: `kind: "message" | "action" | "notice"` (default
"message"). The TTS worker skips notices, and speaks actions as
"<handle> <text>".

| Command | result | broadcast (to `/ws` clients) | spoken |
|---------|--------|------------------------------|--------|
| `names` | `{"participants": [participant…]}` | none | no |
| `whois <handle>` | `{"participant": participant, "last_message": message \| null}`; handle match is case-insensitive prefix; `404` when none | none | no |
| `me <text>` | `{"message": message}` | `{"type": "message", …, "kind": "action"}` | yes: "<handle> <text>" |
| `notice <text>` | `{"message": message}` | `{"type": "message", …, "kind": "notice"}` | no |
| `topic [text]` | `{"topic": str \| null, "topic_set_by": str \| null}` | with text: `{"type": "topic", "topic", "handle"}` | no |
| `away [reason]` | `{"participant": participant}` | `{"type": "presence", "handle", "state": "away", "reason"}` | no |
| `back` | `{"participant": participant}` | `{"type": "presence", "handle", "state": "active"}` | no |

Actions and notices are persisted in the message archive like messages, so
`/history` returns them with their `kind`.

**CLI**: `agent-ptt names`, `whois <handle>`, `me <text>`, `notice <text>`,
`topic [text]`, `away [reason]`, `back`, all using the joined channel and
key from `~/.agent-ptt/session.json`, printing the result as a short table
or line (JSON with `--json`).

**Web UI**: text beginning with `/` in the say box is parsed as
`/name args` and posted to the command endpoint; `names` and `whois` render
inline, notices render dimmed, actions render as "* handle text", topic shows
in the header, presence updates the participant list.

**Ownership**: server + models + channel manager + tests: subagent "server";
CLI + docs: subagent "cli"; web UI: whichever session claims it in the
channel, else subagent "ui"; announcer hooks (`/away` at Stop, `/back` at
prompt): Hazel.
