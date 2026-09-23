# Agent PTT Voice — /say skill for Claude Code

Lets Claude (or you) speak messages aloud through an
[Agent PTT](https://github.com/arvidsson-geins/agent-ptt) voice channel:

```
/agent-ptt-voice:say deploy finished, all forty-two checks green
```

…or just ask in plain language — "announce out loud that the build is
fixed" — and Claude invokes the skill itself.

Messages are spoken with the project's own auto-designed voice (same
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

Use the channel skill to keep Claude Code and Codex on the same channel:

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

Creating or selecting a channel updates `~/.agent-ptt/announcer.env`. Both
announcer hooks read that file, so channel selection is shared across CLIs.
Restart a CLI that is already running after switching channels.

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_PTT_URL` | `http://localhost:8770` | Agent PTT server |
| `AGENT_PTT_CHANNEL` | `Claude Code` | Channel to speak in |
| `AGENT_PTT_AGENT` | `Claude` | Speaker name prefix |

Unlike the announcer hooks, `/say` is explicitly invoked — so failures are
reported (with a hint to start the server) instead of being swallowed.
