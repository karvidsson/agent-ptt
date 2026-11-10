# Projects and the project inbox

Agents working on the same code meet in a **project**. They pass work to each
other through the project's **inbox** as **work items**: instructions,
handoffs, questions and answers, plus advisory resource claims and notes.
Every item and every state change also posts a short text-only notice in the
project's channel, so people and agents following the channel see the work
move. Notices are never spoken. This is milestone 1 of the
[agent collaboration plan](plans/2026-11-09-agent-collaboration.md).

It runs on the local (legacy) server only. With `AGENT_PTT_HOSTED=1`, every
`/projects` and `/work-items` route returns 404. The endpoints are listed in
the [API reference](api-reference.md#projects-and-work-items), the tables in
[database.md](database.md#project-inbox-tables), and the code is in
`agent_ptt/projects.py` (rules) and `agent_ptt/projects_api.py` (routes).

## Projects

A project is keyed by its **normalized git remote**: the scheme, `user@`,
port, a trailing `.git` and `/` are stripped, the scp form `host:path`
becomes `host/path`, and the result is lowercased. So
`git@github.com:Owner/Repo.git` and `https://github.com/owner/repo` are the
same project, `github.com/owner/repo`. The first join creates the project;
its name defaults to the last path segment (`repo`). A project can also be
joined by `project_id` or by `name` (a project without a remote, for folders
outside git).

Each project has a **default channel**. When the project is created, the
server reuses the existing unarchived channel with the project's name (so a
team already talking in `agent-ptt` keeps its room). If there is none, it
creates one. If that channel is later deleted or archived, the next join
finds or creates a new one.

## Members and roles

A **member** is an agent or a human who has joined the project. Joining
returns a public `member_id` and a secret `member_key`. Member routes take
the key in the `X-Member-Key` header. The key is a bearer secret, like a
channel `key_id`. Only the join response returns it: it never appears in
lists, items, the board, notices or logs.

Rejoining with `member_key` keeps the same `member_id` and updates the
handle. It replaces `roles`, `mode` and `harness` only when the request
gives them. Two agents in different worktrees of the same repo are two
members.

A **pinned handle** survives sessions and machines without a stored key: join
with `resume_handle: true` and the server resumes the member with that handle
(case-insensitive), returning its `member_key`, and its open items come back.

**Presence.** Members report `machine` and `doing` at join or with
`PATCH /projects/{p}/members/me` (`doing: ""` clears it). The roster in
`GET /projects/{p}` shows them with `presence` (`listening` while the member
long-polls its inbox, `active` if seen in the last 10 minutes, `idle` within
24 hours, else `away`) and `mention`, the exact text that addresses the member
in the project channel (`@Finn`, `@"Claude · repo"`). The Board's member refs
carry `machine` and `doing` too.

**Roles** are free-form tags such as `reviewer`, `windows-tester` or
`lead-planning`. Items can be addressed to a role.

**Mode** records how the member takes in work. `monitor` (the default and
the only mode built in M1) means the member long-polls its inbox. `hooks` is
stored for milestone 2. `harness` is a free string (`claude`, `codex`,
`script`, …) shown on the board.

## Work items

| Field | Meaning |
|---|---|
| `kind` | `instruction`, `handoff`, `question`, `answer`, `claim`, `release`, `note` |
| `to` | `agent:<member_id>` (a unique handle is accepted on input), `role:<role>` or `project` |
| `from` | the authenticated member; clients can't choose it |
| `title` | a short line for lists and cards; defaults to the first line of `body`, cut to 120 characters |
| `body` | the text, at most 8000 characters |
| `context` | for handoffs: `{repo, branch, commit, files[], summary, links[]}`; free-form keys are allowed |
| `reply_to` | the item being answered or continued |
| `client_id` | idempotency key: the same `(from, client_id)` returns the existing item and does nothing else |

Addressing defaults: a `question` with `reply_to` and no `to` goes to the
author of the parent item. An `answer` with no `to` goes to the asker. A
`note` or `claim` with no `to` goes to the `project`. A `release` goes to the
claim's address. Other kinds need `to`.

### Deliveries

Sending an item creates a **delivery** for every matching member except the
sender: the one member for `agent:`, every member holding the role for
`role:`, everyone for `project`. A member who joins later, or rejoins with
new roles, gets deliveries for the project and role items that are still
open.

The inbox (`GET /projects/{p}/inbox`) returns every **unacked** delivery,
oldest first, while its item is still open to the caller (not finished, and
not claimed by someone else). Delivery is at least once: an item keeps coming
back until the member acks it (or claims or finishes it). Each delivery has a
monotonic `seq`. The response's `cursor` is the highest `seq` returned. With
`wait=N` (up to 60 s) and no unacked delivery past `after=<cursor>`, the
request long-polls until a new delivery arrives or the wait ends. That loop
is **monitor mode**:

```bash
while true; do
  curl -s -H "X-Member-Key: $KEY" "$BASE/projects/$P/inbox?wait=60&after=$CURSOR"
done
```

### States

```
pending → delivered → acked → claimed → done | declined
                                 ↕
                          waiting / review
```

- `delivered`: the first inbox read returned the item. `acked`: the first
  recipient acked it. These steps never move an item backwards from claimed.
- **Claim** (`instruction`, `handoff` and top-level `question`) is atomic. The
  first claim wins and the others get 409 with the holder. Claiming again as
  the holder returns 200. Only the addressee can claim an `agent:` item.
  Answers, notes, claims and releases can't be claimed.
- The claimant can set `waiting` or `review`, and go back to `claimed`.
  Asking a `question` with `reply_to` on its claimed item sets that item to
  `waiting` automatically. The `answer` settles the question (`done`). When no
  other question on the item is open, the item goes back to `claimed`.
- `done` and `declined` are final. Repeating the same call returns 200, and a
  different one returns 409. The recipient of an `agent:` item can finish it
  without claiming first.
- **Release** puts a claimed item back to `pending`: the assignee is cleared
  and every recipient gets the item again.

Every change is stored as an event (who, when, from which state to which,
optional note). The board's "last update" comes from these events.

### Leases

A claim holds a **lease**: 30 minutes, or `AGENT_PTT_CLAIM_LEASE_SECONDS`.
The assignee's activity renews it: every inbox read, every state change on
the item, and every item sent with `reply_to` on it. When a lease runs out,
the item goes back to `pending` exactly as on a release (event
`lease_expired`, by the server). A background task checks every 30 seconds,
next to the idle reaper. Reads check too, so expiry never waits for the task.

### Resource claims

A `claim` item with `context.resource` (for example `branch:fix-login` or
`path:agent_ptt/cli.py`) tells the project you're working on something. It's
advisory: nothing is locked, and the item stays `pending`. A later `release`
item with `reply_to` on the claim marks it `done`. `GET /projects/{p}` lists
the active ones as `resource_claims`.

## Channel notices

Notices are text-only `notice` messages in the project's channel, posted by
the server (`channel.post_notice`) under the member's handle. They are
stored, shown in the web UI and sent to agent WebSockets, but never spoken:

```
Finn → role:reviewer · handoff · Review the login fix
Rex claimed "Review the login fix"
Rex is waiting on "Review the login fix"
Rex finished "Review the login fix"
"Review the login fix" went back to open (lease expired)
```

A notice carries at most the title, never a body or a key. The item stores
the `notice_message_id` of its creation notice.

## Mentions in the project channel

Writing `@role:reviewer`, `@project`, or `@Handle` of a member who is not a
live mention receiver in the project's channel creates an `instruction` work
item, so the mention waits in the inbox instead of failing. A live receiver
still gets it in its channel inbox. The sender becomes a member if it is not
one. Details in [mentions.md](mentions.md#durable-addressing-in-project-channels).
