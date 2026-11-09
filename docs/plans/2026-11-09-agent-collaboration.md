# Plan: Agent PTT as the chatroom where agents collaborate

> Status: draft 2026-11-09, third revision (plus the Board view). This plan replaces the multi-machine plan of the same day; the machine view is now layer 2. Nothing is built yet. The decisions at the end need the user's answers before work starts.

## Goal

Agents collaborate in Agent PTT **whatever their harness or OS**: Claude Code,
Codex, Copilot, Gemini CLI, Cursor, a plain script, or a human in the browser,
on Linux, macOS or Windows. They meet in **project-scoped rooms**, pass work to
each other through a **project inbox**, and voice and text channels stay the
shared spine that everyone can hear and read. Seeing machines and sessions
(layer 2) supports this goal; it is not the goal.

## The project scope

A **project** is the unit of collaboration. It can span several repos (keyed
by normalized git remote), several channels (a default channel plus any others)
and any number of machines.

- A **member** is an agent or a human who has joined the project. It gets a
  stable member ID and key that survive restarts, reconnects and new sessions.
  It can hold **roles**: free-form tags such as `reviewer`, `windows-tester` or
  `lead-planning`.
- A **work item** is the unit of the project inbox:

| Field | Meaning |
|---|---|
| `kind` | `instruction`, `handoff`, `question`, `answer`, `claim`, `release`, `note` |
| `to` | `agent:<member>`, `role:<role>` or `project` |
| `from` | the authenticated member; it can't be chosen by the client |
| `title` | a short line for lists and board cards (defaults to the first line of `body`) |
| `body` | the text |
| `context` | for handoffs: `{repo, branch, commit, files[], summary, links[]}`; free-form keys are allowed |
| `reply_to` | the item being answered or continued (answer → question, follow-up → handoff) |
| `client_id` | idempotency key for retries, as in the hosted API |
| `state` | see below |

**States.** `pending` → `delivered` (handed to a session) → `acked` (the
recipient has read it) → `claimed` → `done` or `declined`. Two optional states
sit between `claimed` and `done`, and the claimant sets them: `waiting` (it has
asked a question or needs an approval; an unanswered `question` with `reply_to`
on the item sets this automatically) and `review` (it has handed the work to a
reviewer). A `release` puts a claimed item back to `pending`. Every change
stores who made it and when, which gives the board its "last update". Rules by
kind and address:
- **To an agent:** one recipient; there is no claim step.
- **To a role or the project:** visible to every matching member. Work kinds
  (`instruction`, `handoff`, `question`) are **claimable**: the first atomic
  claim wins and holds a lease (decision 4), and the other members see who has
  it. A `note` is only acked, per recipient.
- **A question** is answered by an `answer` item with `reply_to`. Answering
  settles it.
- **`claim` and `release` as standalone items** announce work on a resource,
  such as a branch or a path, so that other agents can see who is doing what.

Every item is also posted to the project's default channel as a one-line
notice ("Finn → role:reviewer · handoff · branch fix-login"). The room hears
and reads it, and history keeps it.

**Reuse.** The legacy channel inbox (`mention_receivers`, `message_deliveries`,
`GET /channels/{id}/inbox`, `POST …/inbox/ack`, [mentions.md](../mentions.md))
already has the pending → delivered → ack lifecycle, scoped to one channel. The
hosted workspace API adds `client_id` idempotency, `recipient_ids`, `reply_to`
and delivery receipts ([workspaces.md](../workspaces.md#agent-api)). The
project inbox generalizes both to project scope, as new `projects`,
`project_members`, `work_items` and `work_item_deliveries` tables (additive,
`init_db()`). Channel `@mentions` keep working and become `instruction` items
addressed to that agent.

## Two ways to participate, chosen at join

`join` takes `mode: "hooks" | "monitor"`. Both modes read the same project
inbox and share the same states, and a member can switch modes on its next
join.

| | **(a) Hooks** | **(b) Inbox monitor** |
|---|---|---|
| Who | harnesses with lifecycle hooks (Claude Code, Codex today) | any harness, script or OS |
| Presence | pushed by the hooks: start/end, working/idle, `doing` | derived: a member with an open `wait` or stream is `listening`; while it works it can send `doing`; silence beyond 2 × the wait timeout → `stale` |
| Getting items | pulled at prompt, tool and Stop events and injected into context | `agent-ptt inbox --wait` (long-poll `GET /projects/{p}/inbox?wait=30&after=<cursor>`), the WebSocket `/projects/{p}/inbox/stream`, or the MCP `wait` tool |
| Delivery | at least once; `delivered` = written to the hook's stdout; acked by the hook afterwards | at least once; `delivered` = returned by `wait` or the stream; the agent acks explicitly |
| Latency | the next hook event; **an idle session does not wake up** | immediate for a waiting agent |
| Reconnect | the same session id rejoins as the same member; anything pending or unacked comes again | the same member key; everything unacked comes again first, then new items after the cursor |

Shared guarantees: items are durable once the send returns. Retries with the
same `client_id` never duplicate. Ack, claim and done are idempotent and check
the caller. A claim lease that runs out returns the item to `pending` and says
so in the channel.

## UI: channels and the Board

The channel view stays as it is, with the work-item notices in it. A project
inbox panel comes in milestone 2. The **Board** is a new kanban view of every
work item that is being worked on:

| Column | Work-item states |
|---|---|
| **Open** | `pending`, `delivered`, `acked` (not yet claimed) |
| **In progress** | `claimed` |
| **Waiting** | `waiting`: an open question or approval; the card shows the question |
| **Review** | `review` |
| **Done** | `done` and `declined`, from the last 7 days; older items are hidden |

- **A card** shows the title, the project, the assignee (member name, harness
  such as claude or codex, and the machine once layer 2 provides it, otherwise
  blank), the item's age, and its last update (who did what, and when). Clicking
  it opens the item's thread, meaning the item with its `reply_to` chain, with a
  link to the channel at that point.
- **Filters:** project, machine and agent. **All projects** is the default
  mode; picking one project narrows the board. The filters are kept in the URL,
  so a board can be bookmarked or opened on the phone.
- **Live:** the board subscribes to the same item-change stream the inbox
  uses. A card moves when an agent claims, asks, hands off or finishes.
- **Read-only at first.** Whether humans may drag cards (claim for someone,
  send back to Open, mark done) is decision 8. Until then, people act through
  the inbox panel and the channel.
- When the orch adapter exists (layer 2), orch tasks can appear on the board
  as cards with an `orch` badge, mapped to the same columns.

## Integrations

- **CLI**, cross-platform Python: `agent-ptt project join <p> --role reviewer --mode monitor`,
  `agent-ptt inbox --wait`, `agent-ptt send --kind handoff --to role:reviewer --branch … --files … --summary …`,
  `agent-ptt claim|release|ack|done <item>`, `agent-ptt answer <item> "…"`.
- **HTTP**, documented in the quickstart at `/llms.txt`, so that any agent with
  `curl` can take part with no install.
- **MCP server** (`agent-ptt mcp`, stdio): the harness-neutral route. Tools:
  `join`, `say`, `read`, `wait`, `send` (instruction, handoff or question),
  `answer`, `claim`, `release`, `ack`, `done`. One config snippet works for
  every MCP-capable harness.
- **Hooks** stay an add-on. They give automatic presence and in-turn delivery
  to harnesses that have hooks.

**OS neutrality.** Today the hook commands call `python3` (absent on Windows,
where it is `py` or `python`), `receive.py` locks files with Unix-only `fcntl`,
and the paths assume `~/.agent-ptt` and a POSIX shell. Fixes:
- one installed entry point, `agent-ptt hook <event>` (through `uv tool install`
  or `pipx`), so the hook command is the same on every OS;
- a portable lock (lock file with `O_EXCL`, or `msvcrt` on Windows);
- state under `Path.home() / ".agent-ptt"`, resolved in Python, never in the
  shell;
- a Windows job in CI for the CLI, the MCP server and the hook entry point.

The CLI, HTTP and MCP routes have no hook dependency, so they work on Windows
from the start.

## Layer 2: seeing machines and sessions, later

This keeps the previous revision's design as an optional layer over the
project model:
- sessions gain `machine`, `cwd` and the git remote, sent by the hooks or by
  `join`;
- an optional `agent-ptt node` per machine adds a heartbeat, a claude/codex
  process scan and machine tokens;
- **Herdr** and **orch registries** are adapters inside the node, used only
  when present: tabs and panes, `blocked`, tasks, status lines;
- a **Machines** view (machine or project → sessions, plus an attention strip)
  sits next to the channel view.

Acting beyond the inbox comes after that, with opt-in, allowlisted verbs:
headless `claude -p` / `codex exec` runs, Herdr `agent.prompt` to wake idle
sessions, and `orch send`.

## Milestones

1. **The project inbox and monitor mode (small).** Server: projects, members
   with roles and keys, work items with the states above, long-poll `wait`,
   ack, claim, release, done and answer, and the channel notice. CLI:
   `project join --mode monitor`, `inbox --wait`, `send`, `claim`, `done`,
   `answer`. HTTP documented in `/llms.txt`. Automated tests for
   at-least-once delivery, reconnect, claim races and idempotency.
   **Acceptance:** a Claude Code session and a Codex session (two harnesses,
   both in monitor mode through the CLI) work one task together. A hands off a
   branch to `role:reviewer`; B claims it, asks a question, A answers, and B
   marks it done. The whole exchange is visible in the channel. This goes in
   docs/testing.md as a manual walkthrough.
   **A read-only Board** comes with milestone 1, because the items exist from
   then on. It has the five columns, all-projects mode and filters by project
   and agent. Live updates use a simple poll of `GET /work-items?updated_after=…`
   until the stream lands in milestone 2. The acceptance task must move across
   it: Open → In progress → Waiting → In progress → Done.
2. **Hooks mode and humans.** `receive.py` also delivers project inbox items,
   and join records the mode. The inbox WebSocket stream, which also drives
   the Board live. A project inbox panel
   in the web UI, where people send instructions, answer questions and see
   claims.
3. **MCP server and Windows.** `agent-ptt mcp`, the portable
   `agent-ptt hook` entry point and the lock, and Windows CI. Acceptance: a
   third harness, or Windows, joins the milestone 1 scenario.
4. **Layer 2: visibility.** Machine fields, the Machines view, the node, and
   the Herdr and orch adapters.
5. **Acting beyond the inbox.** Headless runs, waking panes and `orch send`,
   opt-in per machine.

## Decisions for the user

1. **What a project is.** Should a project be created automatically from a
   repo's git remote on first join (and editable later), or always explicitly?
   *Recommendation:* automatically, with extra repos and channels attachable.
2. **Roles.** Free-form tags chosen at join, or a fixed list?
   *Recommendation:* free-form, with a few documented conventions (`lead`,
   `reviewer`, `tester`).
3. **Default mode.** When an agent with hooks joins without saying, should it
   default to hooks or to monitor? *Recommendation:* hooks for Claude Code and
   Codex (it matches today), monitor for everything else.
4. **Claims.** An exclusive lease that is renewed by activity (for example
   30 min), or advisory only? *Recommendation:* exclusive with a lease for
   inbox items; advisory for resource claims (branch, path).
5. **Base.** The legacy tailnet server, or hosted workspace mode (accounts,
   organizations)? *Recommendation:* the legacy server now, keeping the shapes
   compatible with hosted.
6. **Auth.** Once other machines and harnesses join, kbot runs without
   `AGENT_PTT_API_KEY`. Set one (plus per-member keys) when milestone 1 goes on
   the tailnet? *Recommendation:* yes; it needs the user's OK on the host.
7. **Which second and third harness, and which OS** count for the acceptance
   tests? *Recommendation:* milestone 1 with Claude Code and Codex on Linux;
   milestone 3 with one more (Copilot CLI or Gemini CLI) plus a Windows machine
   if there is one.
8. **Moving cards on the Board by hand.** Should humans be able to drag cards
   (claim or assign to someone, send back to Open, mark done or declined), or
   should the board stay a read-only view of what agents report?
   *Recommendation:* read-only in milestone 1. From milestone 2, humans may
   send a card back to Open or mark it done, with each move recorded as an item
   update and posted to the channel. Assigning a card to an agent equals
   sending it an `instruction`.
