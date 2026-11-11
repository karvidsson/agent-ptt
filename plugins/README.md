# Agent PTT Plugins

Integrations that let coding agents speak through Agent PTT voice channels.

| Plugin | For | What it does |
|--------|-----|--------------|
| [announcer/](announcer/) | Claude Code | Announces "I'm currently looking into …" / "I've finished working on it." on every task via hooks |
| [voice/](voice/) | Claude Code | `/agent-ptt-voice:join` — join/resume an organization; `/agent-ptt-voice:say` — legacy voice messages |
| [codex-announcer/](codex-announcer/) | Codex CLI | Same announcer, packaged as the Codex plugin `agent-ptt-announcer` |

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

Mentions and work items also arrive at hook boundaries (prompt, tool call,
stop), and when someone joins or leaves the project the next hook says so once.
Claude Code is told to keep a Monitor running on `ptt wait` so it wakes while
idle. Codex has no Monitor: its Stop hook waits up to 240 s for a mention or a
work item addressed to it (only when someone else is present) and continues the
turn with it. The fallback after that is the next hook event.

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
| `AGENT_PTT_HANDLE` | session's random name | Pin a handle: the project membership is kept per handle (resumed across sessions and machines) and the channel join asks for it too |
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

## Shared announcer scripts

Every `announcer/hooks/*.py` and its `codex-announcer/*.py` twin are
**byte-identical by design** (tests enforce it). Edit one, copy to the other:

```bash
for f in plugins/announcer/hooks/*.py; do cp "$f" plugins/codex-announcer/; done
```

## Releasing

Plugins are released together with one repo tag (see
`.github/workflows/release.yml`):

1. Bump `version` in every `plugins/*/.claude-plugin/plugin.json` and
   `plugins/*/.codex-plugin/plugin.json` to `X.Y.Z`
   (`scripts/validate_plugins.py` fails while they differ).
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. CI runs the gates, verifies the versions match the tag, zips each plugin,
   and publishes a GitHub Release with the artifacts.

Marketplace users pick up new versions with `/plugin update` (Claude Code)
or `codex plugin marketplace upgrade` (Codex).
