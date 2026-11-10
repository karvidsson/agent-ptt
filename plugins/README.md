# Agent PTT Plugins

Integrations that let coding agents speak through Agent PTT voice channels.

| Plugin | For | What it does |
|--------|-----|--------------|
| [announcer/](announcer/) | Claude Code | Announces "I'm currently looking into …" / "I've finished working on it." on every task via hooks |
| [voice/](voice/) | Claude Code | `/agent-ptt-voice:join` — join/resume an organization; `/agent-ptt-voice:say` — legacy voice messages |
| [codex-announcer/](codex-announcer/) | Codex CLI | Same announcer, packaged as Codex hooks |

All of them talk to an Agent PTT server over plain REST — on this machine or
another one (set `AGENT_PTT_URL`, and `AGENT_PTT_API_KEY` when the server has
one, in `~/.agent-ptt/announcer.env`) — and are stdlib-only: no dependencies
beyond `python3`. Nothing in them assumes a checkout of this repo.

## Installing

From a local checkout, install everything with:

```bash
python3 scripts/install_plugins.py      # --skip-claude / --skip-codex
```

It always installs from the repo's **main checkout**, even when run inside a
linked git worktree — so a throwaway worktree (e.g. one the Copilot app
creates) never ends up registered and later vanishes from under your CLIs.
For Claude Code it re-registers the `agent-ptt` marketplace and reinstalls
both plugins; for Codex it points the announcer entries in
`~/.codex/hooks.json` at the main checkout and leaves your other hooks
alone. Re-run it after changing a plugin: Claude Code caches plugins by
version, so a reinstall is what picks up the change.

Without a checkout, install the Claude Code plugins straight from GitHub:

```
/plugin marketplace add arvidsson-geins/agent-ptt
/plugin install agent-ptt-announcer@agent-ptt
/plugin install agent-ptt-voice@agent-ptt
```

## Adding a new plugin

1. Create `plugins/<name>/` with a `.claude-plugin/plugin.json` (at minimum
   `name` + `version`; keep `version` in lockstep with the repo release tag —
   CI enforces this on release).
2. Put content in the standard auto-discovered directories inside the plugin:
   `hooks/hooks.json` (+ scripts), `skills/<skill>/SKILL.md`, `agents/`,
   `commands/`. Skills are preferred over commands for new work.
3. Register it in `.claude-plugin/marketplace.json` at the repo root with
   `"source": {"type": "path", "path": "./plugins/<name>"}`.
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
| `ptt say "@Theo found X in a.py" [--quiet]` | speak in the channel (`--quiet`: text only); `@Handle` reaches them even when they're away, `@role:<r>` / `@project` become work items |
| `ptt who` | roster: handle, roles, harness, machine, presence, doing |
| `ptt inbox` | pending mentions and work items, with bodies |
| `ptt wait [--once]` | one line per new mention/work item, forever — run it in Claude Code's Monitor tool |
| `ptt tell <Handle\|role:<r>\|project> "<text>" [--kind …] [--reply-to <item>]` | send a work item |
| `ptt ack <id>` / `ptt done <id> [--note …]` | acknowledge / finish a mention or work item |

Mentions and work items also arrive at hook boundaries (prompt, tool call,
stop), and when someone joins or leaves the project the next hook says so once.
Claude Code is told to keep a Monitor running on `ptt wait` so it wakes while
idle. Codex has no Monitor: its Stop hook waits up to 240 s for a mention or a
work item addressed to it (only when someone else is present) and continues the
turn with it. The fallback after that is the next hook event.

| Variable | Default | Description |
|---|---|---|
| `AGENT_PTT_HANDLE` | session's random name | Pin a handle: the project membership is kept per handle (resumed across sessions and machines) and the channel join asks for it too |
| `AGENT_PTT_ROLES` | none | Comma-separated project roles, e.g. `lead,reviewer`; a role containing `lead` or `orchestrator` marks a coordinator |
| `AGENT_PTT_WAKE` | on | `0` turns off waking (no Monitor advice, no Codex Stop wait); `1` marks this session a coordinator |
| `AGENT_PTT_WAKE_MAX_PER_HOUR` | `6` | Loop guard: at most this many Stop-hook wakes per hour |
| `AGENT_PTT_STOP_WAIT_SECONDS` | `240` (Codex) | How long the Codex Stop hook waits; `0` disables, max 280 |

All of these can go in the shell environment or `~/.agent-ptt/announcer.env`.
State lives under `~/.agent-ptt/` (`projects/` for memberships and session
pointers, `inboxes/` for channel keys). Workspace (hosted) mode ignores all of
this.

## Shared announcer scripts

Every `announcer/hooks/*.py` and its `codex-announcer/*.py` twin are
**byte-identical by design** (tests enforce it). Edit one, copy to the other:

```bash
for f in plugins/announcer/hooks/*.py; do cp "$f" plugins/codex-announcer/; done
```

## Releasing

Plugins are released together with one repo tag (see
`.github/workflows/release.yml`):

1. Bump `version` in every `plugins/*/.claude-plugin/plugin.json` to `X.Y.Z`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. CI runs the gates, verifies the versions match the tag, zips each plugin,
   and publishes a GitHub Release with the artifacts.

Marketplace users pick up new versions with `/plugin update`.
