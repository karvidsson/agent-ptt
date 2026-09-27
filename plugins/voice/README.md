# Agent PTT Voice — /say skill for Claude Code

Lets Claude (or you) speak messages aloud through an
[Agent PTT](https://github.com/arvidsson-geins/agent-ptt) voice channel:

```
/agent-ptt-voice:say deploy finished, all forty-two checks green
```

…or just ask in plain language — "announce out loud that the build is
fixed" — and Claude invokes the skill itself.

When a session ID is available, messages use its saved name and voice (same
identity as the [announcer plugin](../announcer/)).

## Requirements

- An Agent PTT server running locally: `uv run agent-ptt server start`
- `python3` on PATH (the script is stdlib-only)

## Install

```
/plugin marketplace add arvidsson-geins/agent-ptt
/plugin install agent-ptt-voice@agent-ptt
```

## Channel management

Use the channel skill to select a room for the current CLI session:

```text
/agent-ptt-voice:channel list
/agent-ptt-voice:channel create Release War Room
/agent-ptt-voice:channel use Release War Room
```

`create` and `use` both find the channel by name and create it if it doesn't
exist, so neither makes duplicates or fails with "not found". Speaking works
the same way: `say` and the announcer hooks create their channel on first use,
and recreate it if it was deleted mid-session (e.g. with **Clear all** in the
web UI).

Creating or selecting a channel saves a per-session override under
`~/.agent-ptt/session-channels/`. It applies to hooks and `say` on the next event
without affecting other sessions. Use `/agent-ptt-voice:channel auto` to return
to the Git repo/folder channel. Missing rooms are created automatically.
The script detects `AGENT_PTT_SESSION_ID`, `CODEX_THREAD_ID`, or `CLAUDE_SESSION_ID`;
you can also pass `--session-id <id> --agent <CLI>` to the channel command.
The shared `announcer.env` file no longer supplies a channel override.

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_PTT_URL` | `http://localhost:8770` | Agent PTT server |
| `AGENT_PTT_CHANNEL` | Repo/folder name | Process-local channel override |
| `AGENT_PTT_AGENT` | `Claude` | Speaker name prefix |

Unlike the announcer hooks, `/say` is explicitly invoked — so failures are
reported (with a hint to start the server) instead of being swallowed.
