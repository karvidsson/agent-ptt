---
description: List, create, or switch the shared Agent PTT voice channel used by Claude Code and Codex.
argument-hint: [list|create|use] [channel name]
allowed-tools: Bash(python3 *channel.py*)
---

Manage the shared Agent PTT channel by running:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/channel.py" $ARGUMENTS
```

Examples:

```text
/agent-ptt-voice:channel list
/agent-ptt-voice:channel create Release War Room
/agent-ptt-voice:channel use Release War Room
```

`create` and `use` update `~/.agent-ptt/announcer.env`, which is shared by
Claude Code and Codex. The selected channel applies to future hook events;
restart either CLI if it is already running.

