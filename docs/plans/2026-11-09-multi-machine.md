# Plan: Agent PTT as the hub for agents on several machines

> Status: draft 2026-11-09. Nothing is built yet. The decisions at the end need the user's answers before work starts.

## Goal

The user runs Herdr on three machines and drives them from a laptop. Agent PTT
should show what all of them are doing, live, in one place. It already carries
the agents' voices and text. Now it also needs to show the structure behind them:

```
machine (kbot, …)            online / unreachable, last seen
└── project                  a Herdr workspace, e.g. "agent-ptt", "pengrid"
    ├── channel              the repo channel the announcer already joins (the spine)
    ├── task                 an orch registry entry: lead, workers, state, last status line
    └── agent                a Herdr pane: claude/codex, idle | working | blocked | done
```

Channels stay the centre. A project row links to its channel. An agent row
links to its participant there, so "who is talking" and "what is running" are
the same view.

## What exists to build on

- **Herdr** (v0.9, socket API protocol 22) already models most of this tree.
  `herdr api snapshot` returns workspaces, tabs, panes and agents as JSON. Each
  agent carries `agent`, `agent_status` (`idle|working|blocked|done|unknown`),
  `agent_session.value` (the Claude/Codex session id), `cwd`, the tab and the
  workspace. `events.subscribe` pushes `pane.agent_status_changed`,
  `pane.created`, `pane.closed` and similar events. `agent.prompt` submits a
  prompt to an agent.
- **herdr-orchestra registries** in `~/.claude/orchestra/projects/<p>/tasks/*/`
  on each machine. `task.json` holds the lead, workers, panes, worktrees and
  state. `status.md` holds timestamped report lines. `question-*.md` holds open
  questions. `inbox.md` holds queued tasks. Several projects run side by side.
- **Announcer hooks** join every session to its repo channel. They post
  presence (away/back, `doing`) and get reaped when idle (`channel.py`). They
  know the session id, which matches Herdr's `agent_session.value`. That id is
  the join key between a participant and a pane.
- **The hosted workspace layer** ([workspaces.md](../workspaces.md)) has the
  right *shapes*: per-principal credentials, a session presence contract with
  `presumed_hung`/`stale`, a durable inbox with delivery receipts, and
  organizations. It runs only in hosted mode (`AGENT_PTT_HOSTED=1`, Postgres,
  no legacy channel restore). The tailnet server on kbot runs in legacy mode.
  Recommendation: build on the legacy server the user actually runs, reuse
  those shapes (presence states, hashed credentials, inbox and ack), and don't
  require hosted mode. See decision 3.

## How machines report in

**A small per-machine daemon, `agent-ptt node`**, runs next to Herdr on each
machine. The hooks stay as they are.

- The hooks only see their own session, and only while hooks fire. They can't
  see panes without an agent, closed panes, `blocked` (a permission prompt) or
  orch tasks. Herdr sees all of that, so one process per machine reads Herdr.
- The node sends one full snapshot on connect, then deltas from
  `events.subscribe`, plus a full snapshot every 60 s to recover from drift. It
  also watches the orch registries (inotify or a 5 s poll) and sends task
  states and new status lines.
- Transport: one outbound WebSocket to the server (`/machines/{name}/ws`).
  Machines dial the server, never the other way round, so no inbound ports are
  needed and a laptop behind NAT works too.
- Alternative: one collector on the laptop that runs `herdr --machine X …`
  over SSH. There is nothing to install, but the view goes dark whenever the
  laptop sleeps. Not recommended.

**Auth over the tailnet.** Each machine is enrolled once
(`agent-ptt node enroll`). The server issues a machine token, stores only its
hash (the hosted agent-credential pattern) and binds it to the machine name.
Tailscale stays the network boundary. The token separates machines from each
other and makes it possible to revoke one. The browser view uses the existing
`AGENT_PTT_API_KEY` if one is set. Today none is set on kbot (decision 4).

**Offline.** The node heartbeats every 15 s. After 45 s of silence the machine
shows *unreachable* with "last seen", and its agents are greyed out, not
removed. The last known tree stays visible. On reconnect the full snapshot
replaces the machine's state. If the server is down, the node keeps only the
latest snapshot and retries with backoff; there is no event backlog. Server
state is in memory plus a `machines` table (name, token hash, last seen), and
nothing else persists in milestone 1.

## Observe, then act

**Observe first** (milestones 1–2). Acting is the valuable part, but it amounts
to remote input into agents on three machines, so it comes after the read path
is trusted.

**Act** (milestone 3), opt-in per machine (`agent-ptt node --allow-actions`):

- **Questions and approvals to the user.** These come from Herdr
  `blocked`/`done` events, orch `question-*.md` files and `state waiting`. They
  appear in one *attention* queue in the UI, and their channel announces them
  by voice. An answer goes back through the node as a fixed action: for an orch
  task, `orch send <area> "task N: <answer>"`; for a bare agent,
  `agent.prompt` to that pane.
- **Send a task.** Pick machine → project → area, type the task, and the node
  runs `orch send <area> "<task>"` (or `agent.prompt` to the main
  orchestrator). The project's own rules still apply, including the user's OK
  before push or deploy.
- The node accepts only an allowlist of verbs (`orch.send`, `agent.prompt`,
  `orch.answer`). Raw `send-keys` and shell commands are never accepted from
  the web. Every action is posted to the project channel as a notice
  (who, what, which machine) as the audit trail.

## UI

A new **Machines** view in the existing no-build web UI (`agent_ptt/static/`),
next to the channel view:

- **Attention strip** on top: blocked agents, open questions and tasks waiting
  for approval, oldest first. In milestone 3 each one gets an answer box.
- **Tree** below: one card per machine (online dot, last seen), with projects
  inside and their tasks and agents as rows. A row shows a status chip, the
  agent name, the task title and the lead's last status line. Clicking a
  project opens its channel. Clicking an agent highlights its participant.
- Narrow screens collapse to a list, because the phone is a likely client
  (Remote Control links are already in `status.md`).
- **Voice** speaks attention events only ("agent-ptt planning lead is waiting
  for you"), not every status flip.
- Pane terminal output is **not** shown by default (it may contain secrets).
  See decision 6.

## Milestones

1. **Read-only machine list (small, shippable).** `agent-ptt node` polls
   `herdr api snapshot` every 5 s and `POST`s it to `/machines/{name}/snapshot`
   (shared API key or none, tailnet only). The server keeps the latest snapshot
   per machine in memory and serves `GET /machines`. A Machines page shows
   machines → workspaces → agents with status chips and unreachable after 45 s.
   Run it on all three machines. No orch data, no actions, no new tables.
2. **Live and joined up.** WebSocket plus `events.subscribe` instead of
   polling. Machine enrollment and tokens (`machines` table, `init_db` path).
   Orch registries: tasks, states and status lines. Panes joined to channel
   participants by session id. Attention strip, with voice for attention
   events.
3. **Act.** Answer questions and approvals, and send a task to an area or an
   orchestrator. Allowlisted verbs, per-machine opt-in, audit notices in the
   channel.
4. **Later.** A per-project activity timeline (status lines and channel
   messages merged), a mobile polish pass, and optionally carrying the machine
   model into hosted mode.

## Decisions for the user

1. **Machines.** Which three machines, and what should they be called? Does
   the server stay on kbot? Is the laptop only a client, or does it also run
   agents and need a node? *Recommendation:* server stays on kbot; a node on
   each Herdr host.
2. **How far toward acting.** Observe only, or also answer questions and
   approvals and send tasks from the web/phone (milestone 3)?
   *Recommendation:* yes, after milestones 1–2, opt-in per machine, allowlisted
   verbs only.
3. **Base.** Build on the legacy tailnet server (no accounts) or move to hosted
   workspace mode (accounts, organizations, Postgres)? *Recommendation:*
   legacy server now, reusing hosted shapes; revisit at milestone 4.
4. **Auth on the tailnet server.** Today kbot runs without
   `AGENT_PTT_API_KEY`, so anyone on the tailnet can read and, with acting,
   control. Set a key before milestone 3? *Recommendation:* tailnet only for
   milestone 1, then machine tokens plus an API key before any action ships.
5. **Voice.** Speak attention events only, or every task state change too?
   *Recommendation:* attention events only.
6. **Terminal output.** Show the last lines of each agent pane in the UI?
   They help with "what is it doing", but they can leak secrets.
   *Recommendation:* off, with a per-machine opt-in later.
