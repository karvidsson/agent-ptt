# Agent PTT Plugins

Integrations that let coding agents speak through Agent PTT voice channels.

| Plugin | For | What it does |
|--------|-----|--------------|
| [announcer/](announcer/) | Claude Code | Announces "I'm currently looking into …" / "I've finished working on it." on every task via hooks |
| [voice/](voice/) | Claude Code | `/agent-ptt-voice:say` skill — speak any message on demand |
| [codex-announcer/](codex-announcer/) | Codex CLI | Same announcer, packaged as Codex hooks |

All of them talk to a locally running server (`uv run agent-ptt server start`)
over plain REST and are stdlib-only — no dependencies beyond `python3`.

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

## Shared announcer script

`announcer/hooks/announce.py` and `codex-announcer/announce.py` are
**byte-identical by design** (a test enforces it). Edit one, copy to the
other:

```bash
cp plugins/announcer/hooks/announce.py plugins/codex-announcer/announce.py
```

## Releasing

Plugins are released together with one repo tag (see
`.github/workflows/release.yml`):

1. Bump `version` in every `plugins/*/.claude-plugin/plugin.json` to `X.Y.Z`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. CI runs the gates, verifies the versions match the tag, zips each plugin,
   and publishes a GitHub Release with the artifacts.

Marketplace users pick up new versions with `/plugin update`.
