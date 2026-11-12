# Agent PTT — Testing Guide

## Automated Tests

```bash
uv sync                                  # installs dev dependencies (pytest, ruff)
uv run pytest                            # run the full Python suite
uv run pytest tests/test_api.py -k name  # run a single file or test
uv run ruff check .                      # lint
uv run ruff format --check .             # format check (CI); drop --check to reformat
python3 scripts/validate_plugins.py      # plugin manifests and the marketplace registry
node --test tests/workspace-ui.test.mjs tests/workspace-speech.test.mjs   # browser-side workspace JS
node --test tests/board.test.mjs         # Board (work items kanban) logic
```

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`,
`pytest -q`, `node --test tests/board.test.mjs` and `validate_plugins.py` on
every push and pull request. The
`node --test` files are plain Node (no npm install, no `package.json`); run
them locally when touching `agent_ptt/static/workspace*.js` or `board.js`.

The suite in `tests/` covers the channel manager, data models, TTS registry, audio mixer bookkeeping, all REST + WebSocket endpoints, the workspace API and migrations, and the plugin hook scripts (run as subprocesses). TTS synthesis and speaker playback are faked (`tests/conftest.py`), and a temporary SQLite database is used — no network, audio hardware, or running server required. Set `AGENT_PTT_TEST_POSTGRES` to also run the migration tests against a real PostgreSQL (see [workspaces.md](workspaces.md#hosting-boundary-and-persistence)).

What automated tests **cannot** cover is the audible path: real pocket-tts synthesis and actual speaker output. Use the manual walkthrough below for that.

## Manual Testing

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) installed

### Setup

```bash
cd /path/to/agent-ptt
uv sync
```

## Start the Server

```bash
uv run agent-ptt server start --port 8770
```

Leave this running in its own terminal. The server starts muted; for the audible
walkthrough below add `--speakers` (`uv run agent-ptt server start --port 8770 --speakers`)
so speech plays on this machine, or follow along with `agent-ptt listen` / the web UI.
The startup log says which mode is active (`Host speaker playback: on|off`).

## Test Commands (in a new terminal)

```bash
cd /path/to/agent-ptt

# Create a channel
uv run agent-ptt channel create "Test Room"

# List channels (grab the channel ID from here)
uv run agent-ptt channel list

# Join a channel (replace <channel-id> with the actual ID)
uv run agent-ptt join <channel-id> --handle "Krille" --voice "marius"

# Send a message — speech on the speakers (with --speakers) and to listeners 🔊
uv run agent-ptt say "Hello world, this is Agent PTT"

# List available voices
uv run agent-ptt voices

# Listen as a spectator (in yet another terminal)
uv run agent-ptt listen <channel-id>

# Leave the channel
uv run agent-ptt leave

# Check your current config/session
uv run agent-ptt config
```

## Multi-Agent Test

Open 3 terminals:

**Terminal 1 — Server:**
```bash
cd /path/to/agent-ptt
uv run agent-ptt server start --port 8770 --speakers
```

**Terminal 2 — Agent 1:**
```bash
cd /path/to/agent-ptt
uv run agent-ptt channel create "War Room"
# copy the channel ID
uv run agent-ptt join <channel-id> --handle "Claude" --voice "alba"
uv run agent-ptt say "Hello, I'm Claude. Ready to discuss."
```

**Terminal 3 — Agent 2:**
```bash
cd /path/to/agent-ptt
uv run agent-ptt join <channel-id> --handle "GPT" --voice "marius"
uv run agent-ptt say "Hey Claude, GPT here. Let's go."
```

Both messages will be synthesized with different voices and played through the speakers (because of `--speakers`).

## Project inbox acceptance (Claude Code + Codex)

The milestone 1 acceptance test of the [collaboration plan](plans/2026-11-09-agent-collaboration.md):
two harnesses, both in monitor mode through the CLI, work one task together,
and the task moves across the Board. Automated coverage of the same rules
(delivery, reconnect, claim races, leases, idempotency) is in
`tests/test_projects.py` and `tests/test_project_cli.py`; this walkthrough
checks the real harnesses.

1. Start a server (`uv run agent-ptt server start`) and open
   `http://<host>:8770/ui/board.html` in a browser. Point each agent's CLI at
   it (the session's `base_url`).
2. Open two checkouts or worktrees of the same repo (same `origin` remote):
   one with a Claude Code session (A), one with a Codex session (B).
3. A runs `agent-ptt project join --handle Finn --role lead`;
   B runs `agent-ptt project join --handle Rex --role reviewer`.
   Both land in the same project (created from the remote) and its channel.
   `agent-ptt project show` lists both members, with harness `claude` / `codex`.
4. A hands off a branch:
   `agent-ptt send "Please review the login fix" --kind handoff --to role:reviewer --title "Review fix-login" --summary "session cookie fix"`.
   The Board shows the card in **Open**.
5. B runs `agent-ptt inbox --wait 60`, sees the handoff, and
   `agent-ptt claim <id>`. The card moves to **In progress**, assigned to Rex.
   `agent-ptt claim <id>` from A fails.
6. B asks: `agent-ptt ask <id> "Should the cookie be SameSite=Lax?"`. The card
   moves to **Waiting** and shows the question.
7. A runs `agent-ptt inbox --wait 60`, then `agent-ptt answer <question-id> "Yes, Lax."`.
   The card moves back to **In progress**.
8. B reads the answer from its inbox and runs `agent-ptt done <id> --note approved`.
   The card moves to **Done**.
9. Open the project's channel in `/ui/` (the panel's channel link): every step
   is there as a text notice (`Finn → role:reviewer · handoff · …`,
   `Rex claimed …`, `Rex is waiting on …`, `Rex is back on …`,
   `Rex finished …`). `agent-ptt item <id>` prints the thread and events.

Expected: no step needs a hook. Restarting either agent and rejoining from
the same checkout keeps its member; unacked items come back on its next
`inbox`.

## Cross-machine collaboration (roster, no-remote projects, wake, durable addressing)

Agents normally run on other machines than the server. This walkthrough checks
what the automated tests can't: two real machines, real hooks, real idle
sessions. Automated coverage: `tests/test_projects.py`, `tests/test_mentions.py`,
`tests/test_receive.py` and `tests/test_project_cli.py`.

**Setup.** One server reachable from both machines (`http://<server-host>:8770`;
set `AGENT_PTT_API_KEY` there if it is on a shared network). Machine **M1** and
machine **M2**, each with only the announcer plugin installed (see
[plugins/README.md](../plugins/README.md)) or the CLI installed as a tool
(`uv tool install git+https://github.com/karvidsson/agent-ptt`), pointed at the
server (`agent-ptt config --server …` or the plugin's `~/.agent-ptt/announcer.env`)
and given the same API key. Nothing on either machine refers to the server's
filesystem.

1. **A project for a folder without a git remote.** On M1 and M2 create a git
   repo with no `origin` called `review-demo` (or a worktree of one). Start a
   Claude Code session on M1 with `AGENT_PTT_ROLES=lead` and a Codex session
   on M2. Both join the project `review-demo` (the main worktree's folder
   name). It appears in `agent-ptt project list` and on the Board
   (`/ui/board.html`). With the CLI instead of the plugin:
   `agent-ptt project join --handle Theo --role reviewer` on M1 and
   `--handle Poppy` on M2 (names are server-wide: M2 asking for Theo gets
   `Theo is held by … on M1`).
2. **Roster at start.** The second session's start context lists the first:
   handle, roles, harness, machine (its hostname), what it is doing, presence,
   and the exact `@mention` to use. `agent-ptt project show` prints the same
   lines (`Poppy  @Poppy  roles lead  claude  on m1-host  active  — …`).
   `agent-ptt project doing "reading the docs"` on one machine shows up in
   the other's `project show`.
3. **The commands are portable.** Every command in the start context is an
   absolute path on *that* machine (the plugin's own `ptt.py`, run with that
   machine's Python) or the `agent-ptt` CLI; none points into a checkout on
   the server.
4. **Mention an absent agent → queued.** End the M2 session (or wait until it
   is reaped). From M1, say `@Theo please review chapter 2` in the project
   channel. Expected: no 422; the message is posted, and a project work item
   `instruction` to Theo appears (a notice in the channel, a card on the Board).
   Rejoin on M2 with the pinned handle (a new session with `AGENT_PTT_HANDLE=Theo`
   on the machine that holds Theo, or `agent-ptt project join --handle Theo
   --resume-handle` there): it is the same member and the item is in its inbox
   (`ptt.py inbox` / `agent-ptt inbox`).
   A name that matches nobody still fails, and the error lists who can be
   addressed.
5. **The Monitor wakes an idle agent.** In the M1 Claude Code session, start
   the Monitor on the command the start context gives (`ptt.py wait`, or
   `agent-ptt inbox --follow --ack` with the CLI), then leave the session idle
   at its prompt. From M2, `ptt.py tell Poppy "found a broken link in ch. 3"`
   (or `agent-ptt send --kind note --to agent:Poppy "…"`). Expected: within a
   few seconds the Monitor prints
   `[ptt] note from Theo: found a broken link in ch. 3 (item …)` and the
   session starts a turn on it without anyone typing. The item is not
   injected a second time at the next hook.
6. **Idle but present.** Leave the M1 session idle past the reaper grace.
   Expected: it shows as `away` in the channel's participant list and web UI
   instead of disappearing, and a `@Poppy` mention still reaches its channel
   inbox. Any activity brings it back to active; ending the session removes it.
7. **Role addressing.** From M1, say `@role:reviewer please take chapter 4`
   in the project channel (or `ptt.py tell role:reviewer "…"`). Expected: a
   work item to `role:reviewer`, delivered to every member with that role,
   including one that is not in the channel right now; whoever claims it is
   shown on the Board. `@project …` reaches every member.

## Acceptance: a folder review split between Claude and Codex, no pasted instructions

Task 2's acceptance test. The user gives each agent only its normal task
and never pastes keys, IDs, curl lines or instructions about Agent PTT.

**Setup** as above: one server, machine M1 with Claude Code, machine M2 with
Codex, both with the announcer plugin at the current version and the server URL
and API key in `~/.agent-ptt/announcer.env`. A folder (`docs-to-review/`, a
handful of Markdown files, no git remote) is present on both machines, as a
copy or a clone. Give the agents handles and roles, e.g. `AGENT_PTT_HANDLE=Poppy`
on M1 and `AGENT_PTT_HANDLE=Theo` on M2.

1. On M1 start Claude Code in the folder and type only:
   *"Review the docs in this folder for errors and unclear passages."*
2. A minute later, on M2 start Codex in the same folder and type the same task.
3. Expected without further input from the user:
   - Each session's start context names the other (harness, machine, what it is
     doing) and the `ptt` commands. The later one introduces itself
     (`ptt.py say "@Poppy Theo here (Codex on m2) …"`) and the two agree a
     split of the files in the channel, by `@mention` or `ptt.py tell`.
   - Poppy keeps a Monitor on `ptt.py wait`; Theo is woken by the Codex
     route the plugin documents (see the wake-up section of
     [plugins/README.md](../plugins/README.md)). Neither sits idle while a
     mention to it waits.
   - Each reviews only its share, posts findings and a final handoff
     (`ptt.py tell <other> "…" --kind handoff`), not narration, and answers the
     other's `@mentions`. Neither edits a file the other claimed.
   - The channel and the Board show the split, the findings and the handoffs.
     `ptt.py inbox` on either side ends empty, every item acked or done.
4. Fail the test if the user has to relay a message, paste a key or an ID,
   or start a turn by hand to make an agent read a mention. Note the
   auto-wake count: it must stay under the hourly cap
   (`AGENT_PTT_WAKE_MAX_PER_HOUR`), and no wake may come from narration,
   notices or the agent's own messages.

## Popular Voices

| Voice ID | Engine |
|----------|--------|
| `alba` | Pocket TTS |
| `marius` | Pocket TTS |
| `javert` | Pocket TTS |
| `jean` | Pocket TTS |
| `fantine` | Pocket TTS |
| `cosette` | Pocket TTS |
| `eponine` | Pocket TTS |
| `azelma` | Pocket TTS |


Run `uv run agent-ptt voices` for the full list of English voices.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///agent_ptt.db` | Database connection (swap to Turso: `libsql://...`) |
| `AGENT_PTT_API_KEY` | unset | When set, every REST route and WebSocket needs it (see [Authentication](api-reference.md#authentication)) |
| `AGENT_PTT_HOSTED` | `0` | `1` enables the multi-tenant workspace and disables the local channel routes |
| `AGENT_PTT_SPEAKERS` | unset | `1`/`true`/`yes`/`on` plays speech on the host's speakers (same as `server start --speakers`); off by default |
| `AGENT_PTT_MUTE` | unset | `1` forces host speakers off, overriding `AGENT_PTT_SPEAKERS` (the Docker image sets it) |
| `AGENT_PTT_TARGET_LUFS` | `-18` | Loudness every synthesized clip is normalized to; `off` disables (see [Loudness Normalization](voices.md#loudness-normalization)) |

## Targeted agent mentions

Run `uv run pytest tests/test_mentions.py tests/test_receive.py` for routing,
restart recovery, receipt isolation, and hook handoff/retry coverage. For a
live check, refresh the plugins and start an agent session, select it from
the browser composer’s `@` menu, and send a request while it works. The receipt
should change from pending to delivered after a tool or turn boundary. Repeat
while idle: it stays pending until another hook event runs, unless the session
runs the plugin's wake-up loop (`ptt.py wait` under Claude Code's Monitor, or
the Codex route in [plugins/README.md](../plugins/README.md)); see
[Cross-machine collaboration](#cross-machine-collaboration-roster-no-remote-projects-wake-durable-addressing). Delivered records
a hook handoff, not task completion. See [Agent mentions](mentions.md).
