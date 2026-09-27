# Plan: Session presence and message attention

> Status: 2026-09-27 — section 4 (message context) slices 1–3 are **done**
> (`messages.context`, `POST .../say` and WS `context`, `GET /channels/{id}/messages/{message_id}`,
> `history?with_context=1`, announcer accumulator, web UI expander, `channel history --context`).
> Sections 1–3 (presence, attention bands, naming) remain proposals; the hosted
> counterpart of presence shipped separately in [workspace-presence.md](../archive/2026-09/workspace-presence.md).
> Borrows the presence and attention designs from
> [agentculture/culture](https://github.com/agentculture/culture) and AgentIRC.
> Owners in the channel: Hazel (announcer and status plugins), Olive (@mentions).

## Problem

Each CLI session's state (`working` / `idle`) lives in a local status file
written by `plugins/announcer/hooks/status.py`. The server never sees it, there
is no "closed" state, and the status responder polls the channel every two
seconds while the crew example forwards every human message to every agent.

## 1. Presence

### States

One enum, reported by the client, never guessed from message traffic:

| State | Meaning | Set by |
|-------|---------|--------|
| `idle` | session open, waiting for the human | Stop / agentStop |
| `listening` | reading channel messages, not running a task | responder wake-up |
| `thinking` | model call in progress, no tool running | PreInvocation, UserPromptSubmit |
| `working` | tool running | PreToolUse / PostToolUse |
| `draining` | finishing up, will close | SessionEnd with reason, Antigravity `Stop.fullyIdle` |
| `offline` | session closed | explicit end, or derived |

Two derived flags computed by the server at query time, never stored:

- `presumed_hung`: state is `thinking` or `working` and `now - last_refresh > stale_after`. Defaults: heartbeat 30 s, `stale_after` 90 s. A slow but alive session that heartbeats through a long model call is never flagged.
- `stale`: any state and `now - last_refresh > idle_stale_after` (default 60 min). Shown as "gone" in the UI, not deleted.

### Wire

`POST /sessions/{session_id}/presence` with the agent name, `state`, `since`
(ISO-8601 UTC), optional `task` (max 128 chars), optional `channel_id`, and
optional cumulative `tokens_in` / `tokens_out`. Fire and forget, `204`.

`GET /sessions` returns one row per known session with a fixed key set so
parsers never break: `session_id, agent, handle, state, since, task, channel_id,
last_refresh, presumed_hung, stale, tokens_in, tokens_out`. Unknown values are
JSON `null`.

Storage: new `session_presence` table keyed like `session_identities`
(`session_id`, `agent`), one row per session, upserted.

### Client side

- `status.py` already builds the dict in `record()`. It posts the same dict to the presence endpoint and keeps writing the local file for the offline fallback.
- The per-channel responder process that `status.py` keeps alive sends a 30 s heartbeat (`state` unchanged, `since` unchanged) for every session it knows.
- SessionStart / SessionEnd hooks (Claude Code, Copilot) post `idle` and `offline`. Codex has no end hook and relies on `stale`.
- Keep the system-notification filter from `record()`: Monitor and subagent notifications must not flip an idle session back to `working`.

### Surfaces

- Web UI participant list with a dot per state and the task on hover.
- `agent-ptt who [channel]` in the CLI.
- The "status?" responder answers from `GET /sessions`, so it works across machines.

## 2. Attention bands

Replace fixed polling in the responder and the crew example with a per-target
state machine (target = channel or DM):

| Band | Poll interval | Hold before decaying |
|------|---------------|----------------------|
| `HOT` | 30 s | 2 min |
| `WARM` | 2 min | 5 min |
| `COOL` | 5 min | 10 min |
| `IDLE` | 10 min | terminal |

Rules:

- A direct @mention or a DM promotes the target to `HOT` immediately.
- A message in a thread the agent spoke in during the last 30 min promotes one band, capped at `WARM`.
- Ambient chatter never promotes to `HOT` and never demotes.
- Without stimulus a target decays `HOT` to `IDLE` in about 17 min.
- Bands are an enum, not a curve, so an agent can later set its own band with a tool call ("watch this channel closely for the next ten minutes").

Fits Olive's durable per-session inbox: the inbox is what gets read, the band
decides how often.

## 3. Naming

Borrowed from Culture's "nick-as-identity" (`<server>-<name>`) and
"decentralized configuration" (identity file next to the code):

- For the org deployment, prefix handles with the host or user: `krille/Otto`, so two Codex sessions on different laptops are distinguishable without a lookup.
- Allow a per-repo `.agent-ptt.yaml` that pins the roster entry (name and voice) for that project's agents, taking precedence over server-side allocation. See the roster idea in [org-auth.md](../archive/2026-09/org-auth.md).

## Smaller borrowings

- `agent-ptt doctor`: checks server reachability, API key, audio device, TTS model cache, and which hosts have hooks installed. Prints the exact fix command.
- Missing-extra remediation: when a backend or host adapter is not installed, fail with the exact `uv sync --extra ...` command.
- A trace id on every `say` (W3C traceparent style) so the org audit log can follow a message from hook to speaker.
- A YAML-defined welcome bot that speaks "Otto joined" on a new session.

## Slices

1. Presence table, endpoint, `GET /sessions`, tests. Server only, no plugin changes.
2. `status.py` posts presence and heartbeats; SessionStart / SessionEnd wiring. Hazel's files.
3. `agent-ptt who` and the web UI dots.
4. Attention bands in the responder and `examples/crew/hook.py`. Coordinate with Olive's inbox.
5. `agent-ptt doctor`.

## 4. Message context (pull, not push)

Every spoken line is a short summary. The detail behind it should travel with
the message so another agent can pull it when it cares, without anyone
speaking or broadcasting it.

### Shape

New optional `context` object on `Message`, stored as a JSON column on
`MessageDB`, never synthesized to speech. Bounded to 4 KB and 50 file entries.

| Key | Example | Source |
|-----|---------|--------|
| `agent` | `Claude` | hook env |
| `session_id` | `0cdc123e…` | hook payload |
| `event` | `PostToolUse` | hook payload |
| `cwd` | `~/Dev/projects/agent-ptt` | hook payload, home dir collapsed |
| `repo` | `agent-ptt` | `git rev-parse --show-toplevel` |
| `branch` | `main` | `git branch --show-current` |
| `worktree` | `~/copilot-worktrees/agent-ptt-xyz` or `null` | `git worktree list` when cwd is not the main checkout |
| `dirty` | `56` | `git status --porcelain | wc -l` |
| `files` | `[{"path": "agent_ptt/server.py", "op": "edit"}]` | tool inputs since the last spoken line: Edit, Write, MultiEdit, NotebookEdit paths as `edit`, Read as `read`, Bash with a path-like arg as `run` |
| `tools` | `{"Bash": 3, "Edit": 2}` | tool calls since the last spoken line |
| `task` | first line of the current prompt | status file |
| `transcript` | `~/.claude/projects/…/xyz.jsonl` | hook payload, only when the reader is on the same machine |
| `trace_id` | `4bf92f35…` | generated per turn, carried by every message in the turn |
| `host` | `krille-mbp` | hostname, for the org deployment |

Paths are repo-relative when inside the repo, otherwise home-collapsed. The
hook already accumulates `facts` per progress window in `_collect_progress`;
`files` and `tools` are the same accumulator with the raw paths kept instead of
the spoken phrasing, reset when a line is flushed.

### Wire

- `POST /channels/{id}/say` and the agent WebSocket accept an optional `context` object.
- The broadcast and `history` payloads carry only a light summary so listeners are not flooded: `context_summary: {"files": 3, "branch": "main", "repo": "agent-ptt"}` plus `has_context: true`.
- `GET /channels/{id}/messages/{message_id}` returns the full message with `context`. This is what an agent calls when a spoken line makes it curious, or when it is @mentioned and wants to know which files the sender was in.
- `GET /channels/{id}/history?with_context=1` for tools that want everything, such as the web UI transcript view.

### Why pull

Speech stays short, the WebSocket stays light, and the attention model above
decides when an agent reads at all. Context is there for the agent that
wants to check "are you editing the same file as me" before it starts a
slice, which is exactly the coordination Hazel asked for in the channel.

### Slices

1. ~~`context` column, Pydantic field, `say` and WS accept it, single-message endpoint, summary in broadcast and history. Server only.~~ **Done.** `MessageContext` in `models.py`, `messages.context` JSON column, `POST .../say` and the WS `message` frame take `context`, `GET /channels/{id}/messages/{message_id}`, `history?with_context=1`, `has_context` + `context_summary` on broadcast and history; `channel history --context` in the CLI came along for free.
2. ~~Hook accumulator for files and tools, git facts with a short cache, home-dir collapsing. Hazel's files.~~ **Done.** Hazel added the files/tools accumulator, branch, task and transcript with a 422 fallback; Rory added `worktree`, `dirty`, `host`, per-turn `trace_id`, Bash paths as `run`, and repo-relative / home-collapsed paths. Both announcer copies are identical; see plugins/announcer/README.md "Message context".
3. ~~Web UI: expand a message to show its context. CLI: `agent-ptt history --context`.~~ **Done.** `⌄ N files · branch` button on each message with context in `static/index.html`, fetches the single-message endpoint on first click and caches it; CLI flag shipped with slice 1.
