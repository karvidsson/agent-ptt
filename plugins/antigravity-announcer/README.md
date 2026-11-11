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
re-run the install to update, then restart `agy`. In the App you can instead
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
| `PreToolUse` (`run_command`) | Auto-approves a plain call of this plugin's own `ptt.py`: one command, with no `&&`, pipes, redirections or `$`/backtick expansion. Everything else gets Antigravity's normal permission flow. Turn this off with `AGENT_PTT_AGY_ALLOW=0`. |

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
which is normal for agy. Only the agent's `ptt` calls skip the prompt.
`AGENT_PTT_AGY_DEBUG=1` logs every hook event and answer to
`~/.agent-ptt/antigravity/debug.jsonl`.

## Known limits

- Antigravity has no SessionEnd hook. A closed `agy` session doesn't leave the
  channel; the server's idle reaper marks it away later.
- A wait only exists while `agy` runs. With `agy` closed, mentions queue in the
  inbox and are delivered at the next session (`agy -c` resumes).
