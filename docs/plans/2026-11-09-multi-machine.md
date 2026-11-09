# Plan: Agent PTT as the hub for agents on several machines

> Status: draft 2026-11-09, revised the same day so that Herdr is optional. Nothing is built yet. The decisions at the end need the user's answers before work starts.

## Goal

The user runs coding agents on several machines and drives them from a laptop.
Agent PTT should show what all of them are doing, live, in one place:
machines → projects → agent sessions → tasks. It already carries the agents'
voices and text. Now it also needs to show the structure behind them. Channels
stay the centre: a project row links to its repo channel, and a session row
links to its participant there.

**No machine needs Herdr or orch.** A machine with only Claude Code or Codex
and the announcer plugin shows up. Herdr and the orch registries are optional
adapters that add detail when they are present.

## One source-neutral model

Every source maps into the same four records. The server merges them. The UI
renders only the fields some source has filled in, so a Herdr-less machine
looks complete, just with less detail.

| Record | Key | Fields | Filled by |
|---|---|---|---|
| **Machine** | `machine` name (configured, default hostname) | hostname, os, `last_seen`, `reachable` (derived), `sources[]` | hooks, node |
| **Project** | repo identity: normalized git remote URL, else the repo folder name | name, `channel_id`, per-machine paths | hooks (cwd), node |
| **Session** | agent `session_id` (Claude/Codex id) | `agent` (claude/codex), machine, project, cwd, `state`, `doing`, `participant_id`, `started`, `last_seen`, `pid`?, `herdr`? (pane, tab, workspace) | hooks, node scan, Herdr |
| **Task** | source plus id, e.g. `orch:agent-ptt/area-planning#2` | title, project, `state`, lead and worker sessions, last status line | orch adapter; later free-standing tasks from the UI |

An **attention item** (question, approval, blocked agent, task waiting) points at
a session or task and records how it can be answered (see *Acting*).

- **States** use one enum across sources: `idle | working | blocked | away | offline`,
  plus the derived `presumed_hung`/`stale` flags borrowed from the hosted
  presence contract ([workspaces.md](../workspaces.md#sessions-and-presence)).
- **Merge rule:** each field remembers its source and `observed_at`. Hooks win
  for `doing` and presence. Herdr wins for `blocked` and pane placement. A
  process scan only proves that a session exists: when its process is gone,
  the session is `offline` at once, without waiting for the idle reaper.
- **Projects span machines.** agent-ptt on two machines is one project with
  sessions on both. The tree can be grouped by machine or by project.

## Sources, from required to optional

**0. The announcer hooks: the baseline, already installed.** They join every
session to its repo channel and report session start and end, presence
(away/back) and `doing`, and the idle reaper cleans up after them. They miss
three things: the machine, the repo path, and anything between hook events. The
fix is cheap. On join and on presence updates, the hooks also send `machine`
(`AGENT_PTT_MACHINE`, defaulting to the hostname), `cwd` and the git remote.
These are optional fields on the existing join request. The server then derives
the whole machine → project → session tree from participants. **No daemon is
needed for milestone 1.** The limit is that a machine with no live sessions
looks the same as a machine that is switched off. The view says "no live
sessions, last seen 14:02".

**1. `agent-ptt node`: an optional per-machine process, with no Herdr needed.**
It adds what the hooks can't see:
- a machine heartbeat (15 s; *unreachable* after 45 s), so a machine that is
  down is told apart from one that is idle;
- a process scan (`/proc` on Linux, `ps` on macOS) for `claude` and `codex`
  processes: pid, cwd, repo and start time. This catches sessions without the
  plugin, and it marks a session offline as soon as its process exits;
- enrollment: a per-machine token, stored only as a hash on the server (the
  hosted agent-credential pattern), so that the machine identity is no longer
  self-asserted;
- the acting endpoint (below).

It dials out to the server over one WebSocket (`/machines/{name}/ws`). It sends
a full snapshot on connect and every 60 s, with deltas in between. When the
server is down it keeps only the latest snapshot. Tailscale stays the network
boundary.

**2. Adapters inside the node, used only when present.**
- **Herdr**, detected with `herdr api snapshot`. It adds workspaces, tabs, the
  pane for each session (joined on `agent_session.value` = `session_id`), the
  `blocked` status from `events.subscribe`, and the `agent.prompt` action.
- **orch registries**, detected through `~/.claude/orchestra/projects/*`. They
  add tasks, leads, workers, states, status lines and open `question-*.md`
  files, and the `orch send` action.

**What fits from the hosted workspace layer.** It has the right shapes: hashed
per-principal credentials, the presence contract, and a durable inbox with
receipts. It runs only in hosted mode (Postgres, accounts, no legacy channel
restore). The tailnet server on kbot runs in legacy mode. Recommendation: build
on the legacy server, reuse those shapes, and leave hosted mode for later
(decision 3).

## Acting degrades gracefully

Each attention item and each "send" offers only the routes its machine
supports. The UI says which route a message takes, and every action is posted
to the project channel as the audit trail.

| Route | Needs | What it can do | Limit |
|---|---|---|---|
| **Inbox** (mentions, exists today) | hooks only | answer an agent's question; give a running session a new instruction | delivered at the session's next hook event; an idle session waits until someone touches it |
| **Headless run** | node, opt-in | send a task to a machine: run `claude -p` or `codex exec` in a project folder on it; its hooks report into the channel like any session | a new session, not the one that asked; allowlisted projects only |
| **Prompt a pane** | node + Herdr | wake an idle session: `agent.prompt` | — |
| **orch send** | node + orch | give a task, an answer or an approval to an area lead: `orch send <area> "task N: …"` | — |

Two things remain outside the routes:
- **Permission prompts** (`blocked`) are only visible through Herdr. Answering
  them from the web is out of scope. Claude Code's permission hooks may allow
  it later; that needs investigating first.
- **Arbitrary keystrokes or shell commands** are never accepted from the web.
  The node accepts a fixed set of verbs and is opt-in per machine
  (`--allow-actions`).

## UI

A **Machines** view next to the channel view in the no-build web UI:
- **Attention strip** on top: questions, approvals, blocked agents and waiting
  tasks, oldest first, each with an answer box that names its route.
- **Tree** below, grouped by machine or by project. Rows show a state chip, the
  agent, `doing`, the task, and small source badges (hooks / node / herdr /
  orch). Columns no source has filled in (tab, task) are hidden.
- The layout collapses to a list on the phone. Voice speaks attention events
  only. Pane terminal output is not shown (decision 6).

## Milestones

1. **Hooks-only machine view, with no daemon.** The announcer and
   codex-announcer send `machine`, `cwd` and the git remote on join and
   presence. The server stores them on the participant (`init_db`
   add-column path), and `GET /machines` derives the tree. A Machines page
   shows machine → project → session with state and `doing`. Answering through
   the existing inbox works from day one. It runs on any machine with the
   plugin, Herdr or not. A plugin version bump is required.
2. **Plain node.** Heartbeat, process scan, enrollment tokens, a
   reachable/unreachable machine state, and sessions without hooks.
3. **Adapters.** Herdr (tabs, blocked, panes) and orch (tasks, status lines,
   questions). The attention strip, with voice for attention events.
4. **Act beyond the inbox.** Headless runs, `agent.prompt` and `orch send`
   through the node's allowlisted verbs, opt-in per machine.
5. **Later.** A per-project timeline, investigating permission-prompt
   answering, and optionally hosted mode.

## Decisions for the user

1. **Machines and names.** Which machines will report? Use the hostname, or a
   name set per machine (`AGENT_PTT_MACHINE`)? Does the server stay on kbot?
   *Recommendation:* hostname by default, with an override; server on kbot.
2. **Project identity.** Is one repo on two machines one project, or two?
   *Recommendation:* one project, keyed by git remote, shown per machine
   inside it.
3. **Base.** The legacy tailnet server (no accounts), or hosted workspace mode?
   *Recommendation:* the legacy server now, reusing hosted shapes.
4. **How far toward acting.** Inbox answers only, or also headless runs started
   from the web or phone (a new agent session on a machine), and Herdr/orch
   actions where present? *Recommendation:* inbox from milestone 1; the rest at
   milestone 4, opt-in per machine.
5. **Auth.** kbot runs without `AGENT_PTT_API_KEY`, so on the tailnet anyone
   can read, and once milestone 4 ships, anyone can start runs. Set a key and
   machine tokens before milestone 4? *Recommendation:* yes.
6. **Voice and privacy.** Speak attention events only? Keep pane terminal
   output and process command lines out of the UI? *Recommendation:* yes to
   both.
