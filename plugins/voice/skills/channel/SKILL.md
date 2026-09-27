---
description: List, create, or switch the current CLI session's Agent PTT voice channel.
argument-hint: [list|create|use|auto] [channel name]
allowed-tools: Bash(python3 *channel.py*)
---

Manage the current session's Agent PTT channel by running:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/channel.py" $ARGUMENTS
```

Examples:

```text
/agent-ptt-voice:channel list
/agent-ptt-voice:channel create Release War Room
/agent-ptt-voice:channel use Release War Room
/agent-ptt-voice:channel auto
```

`create` and `use` find or create the named room, then select it only for this
CLI session. The script reads `AGENT_PTT_SESSION_ID`, `CODEX_THREAD_ID`, or
`CLAUDE_SESSION_ID`. If unavailable, pass the actual current CLI session ID with
`--session-id <id> --agent Claude` (or `Codex`). Never invent an ID or substitute
a project name: it must match the ID supplied to that CLI's announcer hooks.
If the actual ID is unavailable, explain that selection requires it.

The choice is stored under `~/.agent-ptt/session-channels/`, isolated by server,
CLI source, and session ID. It affects subsequent hook events and manual `say`
commands without restarting. It never changes another session's channel.

`auto` clears the explicit choice for this session. Without an explicit choice,
the Git main checkout name determines the channel, including from subfolders and
worktrees; outside Git, the current folder name is used. The room is reused or
created automatically. Old global channel settings in `announcer.env` are ignored.
