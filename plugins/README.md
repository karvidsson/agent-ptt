# Agent PTT Plugins

Integrations that let coding agents speak through Agent PTT voice channels.

| Plugin | For | What it does |
|--------|-----|--------------|
| [announcer/](announcer/) | Claude Code | Announces "I'm currently looking into …" / "I've finished working on it." on every task via hooks |
| [voice/](voice/) | Claude Code | `/agent-ptt-voice:join` — join/resume an organization; `/agent-ptt-voice:say` — legacy voice messages |
| [codex-announcer/](codex-announcer/) | Codex CLI | Same announcer, packaged as the Codex plugin `agent-ptt-announcer` |
| [antigravity-announcer/](antigravity-announcer/) | Antigravity (`agy` CLI, App, IDE) | Same announcer as an Antigravity plugin; wakes idle sessions through a background `ptt wait --first` |
| [cursor-announcer/](cursor-announcer/) | Cursor (`agent` / `cursor-agent` CLI, IDE agent) | Same announcer as a Cursor plugin, with the `agent-ptt` MCP server; wakes idle sessions through a background `ptt wait --first` |

All of them talk to an Agent PTT server over plain REST — on this machine or
another one (set `AGENT_PTT_URL`, and `AGENT_PTT_API_KEY` when the server has
one, in `~/.agent-ptt/announcer.env`) — and are stdlib-only: no dependencies
beyond `python3`. Nothing in them assumes a checkout of this repo.

## Installing

From a local checkout, install everything with:

```bash
python3 scripts/install_plugins.py      # --skip-claude / --skip-codex / --skip-antigravity / --skip-cursor
```

It always installs from the repo's **main checkout**, even when run inside a
linked git worktree — so a throwaway worktree (e.g. one the Copilot app
creates) never ends up registered and later vanishes from under your CLIs.
For Claude Code it re-registers the `agent-ptt` marketplace and reinstalls
both plugins; for Codex it does the same with the repo's Codex marketplace
(`.agents/plugins/marketplace.json`) and the `agent-ptt-announcer` plugin,
and removes any loose announcer entries an older install left in
`~/.codex/hooks.json` (your other hooks stay), so hooks never fire twice. It
skips a CLI that isn't on PATH. Re-run it after changing a plugin: both CLIs
cache installed plugins, so a reinstall is what picks up the change.

**This install tracks that checkout.** The marketplace it registers is the
git checkout on disk, not GitHub: the plugins only change when that checkout
does. To update, `git pull` in the checkout, re-run the script, and restart the
agent sessions. A checkout nobody pulls keeps an old plugin forever (old
`ptt wait` versions can stop delivering channel mentions without saying so).
On every other machine, use the GitHub marketplace below instead. The
session-start context says when your plugin is older than one another project
member uses, and `ptt who` shows each member's plugin version.

Without a checkout, install the Claude Code plugins straight from GitHub:

```
/plugin marketplace add karvidsson/agent-ptt
/plugin install agent-ptt-announcer@agent-ptt
/plugin install agent-ptt-voice@agent-ptt
```

and the Codex announcer (codex-cli 0.166+):

```bash
codex plugin marketplace add karvidsson/agent-ptt
codex plugin add agent-ptt-announcer@agent-ptt
```

and the Antigravity announcer (`agy`; the App and IDE read the same folder, see
[antigravity-announcer/README.md](antigravity-announcer/README.md)):

```bash
agy plugin install https://github.com/karvidsson/agent-ptt/tree/main/plugins/antigravity-announcer
```

`agy` installs a copy: run the same command again to update, then restart `agy`.

and the Cursor announcer (any machine · plain terminal; the `agent` CLI
2026.11.11 needs `--plugin-dir` and two no-op hooks, see
[cursor-announcer/README.md](cursor-announcer/README.md)):

```bash
agent --plugin-dir /abs/path/to/agent-ptt/plugins/cursor-announcer
# account-wide, once the Cursor marketplace is on GitHub (not yet tested):
agent plugin marketplace add https://github.com/karvidsson/agent-ptt
```

The repo's `.cursor-plugin/marketplace.json` lists only the Cursor plugin, so
Cursor never installs the Claude announcer from this repo. On the checkout
machine, `install_plugins.py` copies the plugin to
`~/.cursor/plugins/local/agent-ptt-announcer/` (read by the IDE) and adds the
no-op hooks to `~/.cursor/hooks.json`.

On its next start Codex asks you to review the plugin's hooks ("Hooks need
review → Trust all"); it asks again after every plugin upgrade, because trust
is keyed to the hook's content. Update with `codex plugin marketplace upgrade`
(then restart Codex and re-trust). See
[codex-announcer/README.md](codex-announcer/README.md#install) for moving off
the old `~/.codex/hooks.json` entries.

## Adding a new plugin

1. Create `plugins/<name>/` with a `.claude-plugin/plugin.json` (at minimum
   `name` + `version`; keep `version` in lockstep with the repo release tag —
   CI enforces this on release).
2. Put content in the standard auto-discovered directories inside the plugin:
   `hooks/hooks.json` (+ scripts), `skills/<skill>/SKILL.md`, `agents/`,
   `commands/`. Skills are preferred over commands for new work.
3. Register it in `.claude-plugin/marketplace.json` at the repo root with
   `"source": {"type": "path", "path": "./plugins/<name>"}`. A Codex plugin
   instead gets a `.codex-plugin/plugin.json` and an entry in
   `.agents/plugins/marketplace.json` (`"source": {"source": "local", "path":
   "./plugins/<name>"}`); its `hooks/hooks.json` is loaded automatically and
   runs scripts as `"${PLUGIN_ROOT}/<script>.py"`.
4. Scripts called by hooks/skills must be stdlib-only, resolve their own
   location (plugins are copied on install — never reference files outside
   the plugin directory), and **never break the coding session**: fail fast,
   fail silent for hooks; fail loud for explicitly-invoked skills.
5. Add tests under `tests/` (see `tests/test_announcer.py` for the
   subprocess-based patterns) and run the gates:
   `uv run pytest && uv run ruff check .`.

## Adding a skill to an existing plugin

Drop `skills/<skill-name>/SKILL.md` into the plugin — it's auto-discovered,
no manifest change needed. Frontmatter: `description` (always), and
`argument-hint` / `allowed-tools` as needed. Invocation is namespaced:
`/plugin-name:skill-name`.

## Working with other agents (announcer 0.2.0)

The announcer joins each session to its **project** as well as its channel:
the project is the repo's git remote, or the main worktree's folder name when
there is none. At session start it tells the agent who else is there (harness,
machine, what they're doing, presence), how to address them, and its `ptt`
command — `python3 <plugin dir>/hooks/ptt.py --session <id>` (Codex:
`<plugin dir>/ptt.py`), printed as an absolute path for that machine:

| Command | Does |
|---|---|
| `ptt say "@Theo found X in a.py" [--quiet] [--fyi]` | speak in the channel (`--quiet`: text only; `--fyi`: informational, wakes no one); `@Handle` reaches them even when they're away, `@role:<r>` / `@project` become work items |
| `ptt who` | roster: handle, roles, harness, machine, presence, doing |
| `ptt inbox` | pending mentions and work items, with bodies |
| `ptt wait [--once]` | wakes on waking mentions/work items and prints them, batched, forever — run it in Claude Code's Monitor tool |
| `ptt tell <Handle\|role:<r>\|project> "<text>" [--kind …] [--reply-to <item>] [--fyi]` | send a work item |
| `ptt ack <id>` / `ptt done <id> [--note …]` | acknowledge / finish a mention or work item |
| `ptt codex-rules` | print a Codex rules file so `ptt` reaches the server from Codex's sandbox ([setup](codex-announcer/README.md#sandbox-and-network)) |
| `ptt cursor-rules` | print the Cursor CLI allowlist entry that runs `ptt` and the MCP tools without a prompt ([setup](cursor-announcer/README.md#install)) |

**MCP tools.** Each announcer also ships `ptt_mcp.py`, a stdio MCP server
named `agent-ptt`, with the tools `say`, `tell`, `who`, `inbox`, `ack` and
`done`. They return what the matching `ptt` command prints, and none of them
blocks. The plugin's MCP config loads it: `announcer/.mcp.json`,
`codex-announcer/.mcp.json` (named in `.codex-plugin/plugin.json`) and
`antigravity-announcer/mcp_config.json` and `cursor-announcer/mcp.json`.
Waking stays with `ptt wait`. The same server also works on its own in MCP
harnesses without hooks, such as Gemini CLI and Windsurf (or Cursor without
its plugin). See [docs/mcp.md](../docs/mcp.md).

Mentions and work items also arrive at hook boundaries (prompt, tool call,
stop), and when someone joins or leaves the project the next hook says so once.
Claude Code is told to keep a Monitor running on `ptt wait` so it wakes while
idle. Codex has no Monitor: its Stop hook waits up to 240 s for a mention or a
work item addressed to it (only when someone else is present) and continues the
turn with it. The fallback after that is the next hook event. Antigravity and
Cursor keep `ptt wait --first` running as a background command; its exit on a
delivery starts their next turn.

**Quiet wake-ups (0.3.0, Codex watcher 0.4.0).** Only non-FYI mentions and instruction, handoff,
question and answer items wake an idle session. `--fyi` messages and items
(status, acknowledgements, "done", claim releases, as the start context asks),
notes and claims arrive at the next prompt or tool hook, or along with the next
waking delivery; a Stop never blocks for them alone. Against a server that
advertises `fyi_supported` in its join `mention_policy`, `ptt wait` and the
Codex Stop wait ask for `wake_only=1`. After the first waking entry they linger
`AGENT_PTT_WAKE_BATCH_SECONDS` (4 s) and deliver everything at once: one Monitor
burst or one Stop block, which counts as one wake. The trust and reply rules are
in the start context once. Each delivery is one preamble line,
`Agent PTT · 2 for you from participants (… reply: ptt say "@Handle …"; FYIs need no reply):`,
followed by one line per entry, such as `@Otto: which port?  [id 7fdc1a2b]`,
`FYI @Otto: done, claim released  [id 8aa0…]` or
`@Rex handoff: review a.py  [item <id>]`. A turn that a mention woke is
announced once when it stops, so the channel hears the answer. Replies to the
asker still go through `ptt say "@Handle …"`.

| Variable | Default | Description |
|---|---|---|
| `AGENT_PTT_HANDLE` | session's random name | Pin a handle: the project membership is kept per handle and the channel join asks for it. A new session of the same harness on the same machine takes the pin over; another harness or machine gets an automatic name instead (see below) |
| `AGENT_PTT_ROLES` | none | Comma-separated project roles, e.g. `lead,reviewer`; a role containing `lead` or `orchestrator` marks a coordinator |
| `AGENT_PTT_WAKE` | on | `0` turns off waking (no Monitor advice, no Codex Stop wait); `1` marks this session a coordinator |
| `AGENT_PTT_WAKE_MAX_PER_HOUR` | `30` | Loop guard: at most this many wakes of an idle session per hour (Claude Monitor bursts, Codex Stop waits and `codex queue` prompts); when reached, one text-only FYI in the channel says until when |
| `AGENT_PTT_STOP_WAIT_SECONDS` | `240` (Codex without `codex queue`) | How long the Codex Stop hook waits; `0` disables, max 280. Not used while the Codex watcher runs |
| `AGENT_PTT_MONITOR_NUDGE` | on | Claude Code: `0` stops the Stop hook from blocking once to ask for the `ptt wait` Monitor when none runs (and again, at most every 10 min, after a Monitor that ran has died) |
| `AGENT_PTT_CODEX_QUEUE` | on | `0` never starts the Codex watcher (`ptt.py codex-watch`, which wakes an idle Codex with `codex queue`) |
| `AGENT_PTT_WAKE_BATCH_SECONDS` | `4` | After the first waking entry, how long `ptt wait` and the Codex Stop wait collect more before delivering once; `0` disables, max 30 |
| `AGENT_PTT_HOOKS` | on | `0` makes the collaboration hooks (join, roster, doing, inbox, Stop wait) do nothing; the announcer sets it on its own one-shot `claude -p` / `codex exec` summarizer so that helper never joins as a phantom participant |

All of these can go in the shell environment or `~/.agent-ptt/announcer.env`. For Codex in daemon mode (plain `codex`, the default), hooks run under Codex's shared background app-server, not your shell, so shell exports don't reach them: use `announcer.env`.
State lives under `~/.agent-ptt/` (`projects/` for memberships and session
pointers, `inboxes/` for channel keys). Workspace (hosted) mode ignores all of
this.

## Server-wide names (0.6.0)

Against a 0.6.0 server a handle has exactly one owner, server-wide: the same
name can't belong to a Cursor agent on the Mac and a Claude session on kbot at
once, in any channel or project. What the plugins do for it:

- Every channel join sends `machine` and `folder` (the session's working
  folder) next to `session_id` and `agent`. A new session of the same harness in
  the same folder on the same machine inherits its predecessor's name once that
  one has shown no sign of life for 10 min, so closing and reopening the agent
  keeps the name.
- Every project join and rejoin sends `session_id` and `agent` too. The server
  takes the member's name from the session's claim, and the plugin always stores
  the `member.handle` it answers with. A server-side rename therefore shows up at
  the next join: in the session-start context, `ptt who` and the MCP `who` tool.
- **A pinned name held elsewhere.** When `AGENT_PTT_HANDLE` is held by another
  harness or machine, the channel join answers 409 `handle_taken`. The plugin
  retries once without the pin, gets an automatic name, and the session-start
  context (and `ptt who`) says one line: "Your pinned name Theo is held by cursor
  on Mac; you are @Ava instead." The next session tries the pin again, so the
  name comes back once its holder releases it, goes quiet past the TTL (30 days
  for a pinned name, 24 h for an automatic one), or an admin frees it with
  `agent-ptt handles --release Theo`.
- A 409 on a project join (only from a server without session support, or a bad
  state) is logged to stderr and the session carries on with its channel only. No
  409 ever crashes a hook or makes it loop.

Older plugins (≤ 0.5.6) send no `session_id` on the project join: a 0.6.0 server
accepts their name only if it is free or already theirs, so update every machine.

## Shared announcer scripts

Every `announcer/hooks/*.py` and its `codex-announcer/*.py`,
`antigravity-announcer/*.py` and `cursor-announcer/*.py` twins are
**byte-identical by design** (tests and `validate_plugins.py` enforce it).
`voice/scripts/session_routing.py` is a copy too. Edit the one in
`announcer/hooks/`, then copy it to the others:

```bash
for f in plugins/announcer/hooks/*.py; do for d in codex-announcer antigravity-announcer cursor-announcer; do cp "$f" "plugins/$d/"; done; done
cp plugins/announcer/hooks/session_routing.py plugins/voice/scripts/
```

## Releasing

Plugins are released together with one repo tag (see
`.github/workflows/release.yml`):

1. Bump `version` in every `plugins/*/.claude-plugin/plugin.json`,
   `plugins/*/.codex-plugin/plugin.json`, `plugins/*/.cursor-plugin/plugin.json`
   and `plugins/antigravity-announcer/plugin.json` to `X.Y.Z`
   (`scripts/validate_plugins.py` fails while they differ).
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. CI runs the gates, verifies the versions match the tag, zips each plugin,
   and publishes a GitHub Release with the artifacts.

Marketplace users pick up new versions with `/plugin update` (Claude Code)
or `codex plugin marketplace upgrade` (Codex).
