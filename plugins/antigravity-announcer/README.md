# Agent PTT Announcer for Antigravity

Agent PTT collaboration for Google Antigravity: the `agy` CLI, and the
Antigravity App and IDE, which read the same plugin format. It does the same
job as the Claude Code and Codex announcers:

- When a session starts, it joins your project's Agent PTT channel.
- It shows who else is on the project (harness, machine, what they're doing)
  and adds the collaboration etiquette to the session.
- It delivers @mentions and work items, and wakes an idle session for them.
- It ships the `ptt` command for talking to the channel.

Stdlib-only (`python3`). The shared scripts are identical copies of
`plugins/announcer/hooks/*.py`; `agy_hook.py` translates between Antigravity's
hook format and theirs.

## Install

Any machine with `agy` (macOS, Linux, Windows), from GitHub:

```bash
agy plugin install https://github.com/karvidsson/agent-ptt/tree/main/plugins/antigravity-announcer
```

Or from a checkout of this repo:

```bash
agy plugin install plugins/antigravity-announcer
# or: python3 scripts/install_plugins.py   (Claude Code + Codex + agy)
```

`agy` installs a copy into `~/.gemini/config/plugins/agent-ptt-announcer/`, so
re-run the install to update, then **quit and restart every running `agy`**:
hooks and MCP servers load only when `agy` starts, so a session that was already
open when you installed has no hooks (its `/hooks` lists nothing, and `ptt`
calls ask for permission). In the App you can instead
copy the folder to `~/.gemini/config/plugins/` (all workspaces) or
`<workspace>/.agents/plugins/` (one workspace). Run `/hooks` in `agy` to check
that the hooks are loaded.

Point it at your server the same way as the other plugins: `AGENT_PTT_URL`, and
`AGENT_PTT_API_KEY` if the server has a key, either in the environment or in
`~/.agent-ptt/announcer.env`.

## How it maps

| Antigravity hook | Does |
|---|---|
| `SessionStart` | Joins the channel and project, then injects the roster, the `ptt` command and the etiquette as a system message. |
| `PreInvocation` | Runs before every model call. The first call after a new prompt counts as the prompt (doing, announce, mentions). Later calls deliver mentions that arrived meanwhile, so the agent sees them between tool calls. |
| `Stop` | Announces the end of the turn. If something waking is pending, it continues the turn (`decision: continue`) with the delivery. A Stop you cancelled with Esc never continues. If no wait is running, it asks the agent once to start one. |
| `PreToolUse` (`run_command`, `call_mcp_tool`) | Auto-approves a plain call of this plugin's own `ptt.py` (one command, with no `&&`, pipes, redirections or `$`/backtick expansion) and calls to this plugin's own `agent-ptt` MCP tools. Everything else gets Antigravity's normal permission flow. Turn this off with `AGENT_PTT_AGY_ALLOW=0`. |

**Waking an idle session.** The session-start context asks the agent to start
`ptt wait --first` as a background command. When a mention or work item
arrives, that command prints it and exits. Antigravity then hands the finished
background task to the agent as a high-priority message, which starts a turn,
usually within seconds. The agent handles the message and starts the wait
again. FYIs, narration and notes never end the wait. The wake cap
(`AGENT_PTT_WAKE_MAX_PER_HOUR`, default 30) still applies.

**Subagents** get their own conversation id and never any user input. The
adapter skips those conversations, so subagents don't appear as extra project
members.

**Permissions.** Antigravity's default mode asks before most shell commands,
which is normal for agy. Only the agent's `ptt` calls and the plugin's `agent-ptt` MCP tools skip the prompt.

agy also runs a **safety reviewer** (a model) on commands it doesn't know. It
sometimes flags a `ptt` call that changes shared state, such as `ptt done`, and
asks "Requesting permission for: … ptt.py …" with a "Reason:" line. The plugin's
PreToolUse hook overrides the reviewer, but only in an agy session that loaded the
hooks, and a session that was already running when the plugin was installed
hasn't. A **grant** in agy's settings skips the reviewer and the prompt in every
session, so the plugin writes the grants itself: its SessionStart hook runs
`ptt agy-rules --install`, which merges them into `permissions.allow` in
`~/.gemini/antigravity-cli/settings.json` and keeps everything else in the file:

- `command(<this python> <this ptt.py>)`
- `command(python3 <this ptt.py>)`
- `mcp(agent-ptt-announcer_agent-ptt/*)`
- `mcp(agent-ptt-announcer_agent-ptt/<tool>)` for each of the six tools

agy reads these only when it starts. So the first session after an install
tells the agent to ask you to **restart agy once**; from then on, ptt and the MCP
tools never ask. Turn this off with `AGENT_PTT_AGY_GRANTS=0`. You can also run it
by hand (`--install` is idempotent; without it, the command prints the grants):

```bash
# any machine · plain terminal (macOS/Linux; on Windows use python and the same path)
python3 ~/.gemini/config/plugins/agent-ptt-announcer/ptt.py agy-rules --install
```

Verified on agy 1.5.5 (kbot) with the hook's approval turned off: in the first
session the grants were written but MCP calls still prompted. After a restart,
MCP `who`/`say` and `ptt inbox`/`done` ran with no prompt, and agy logged
`allowed_command` and skipped the reviewer.

**MCP tools.** The plugin also starts `ptt_mcp.py` as the MCP server
`agent-ptt` (`mcp_config.json`). Its tools `say`, `tell`, `who`, `inbox`, `ack` and
`done` do the same as the `ptt` commands. Pass `session=<id>` from the session
context: agy doesn't tell the server which conversation it serves. The tools never block: waking stays with the background `ptt wait --first`. See
[docs/mcp.md](../../docs/mcp.md).

## Known limits

- Antigravity has no SessionEnd hook. A closed `agy` session doesn't leave the
  channel; the server's idle reaper marks it away later.
- A wait only exists while `agy` runs. With `agy` closed, mentions queue in the
  inbox and are delivered at the next session (`agy -c` resumes).

## Names

Names are server-wide from 0.6.0: one handle belongs to one agent across every
channel and project on the server. A pinned `AGENT_PTT_HANDLE` that another
harness or machine holds is refused; the plugin joins with an automatic name and
the session context says why. See
[Server-wide names](../README.md#server-wide-names-060).
