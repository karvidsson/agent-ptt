# Agent PTT Announcer — Claude Code plugin

Makes your Mac announce what Claude Code is working on, through an
[Agent PTT](https://github.com/arvidsson-geins/agent-ptt) voice channel:

- When you submit a prompt: **"I'm currently looking into the login redirect bug…"**
- When Claude finishes: **"I've finished working on it."**

Each project joins the channel as `Claude · <folder>` **without picking a
voice**, so Agent PTT's auto-voice-designer pins a distinct voice per
project — you can tell your repos apart by ear. With the LLM designer
installed, the voice even matches the project name's vibe.

## Requirements

- An Agent PTT server running locally: `uv run agent-ptt server start`
- `python3` on PATH (the hook is stdlib-only, no dependencies)

## Install

From a Claude Code session:

```
/plugin marketplace add arvidsson-geins/agent-ptt
/plugin install agent-ptt-announcer@agent-ptt
```

Or try it without installing:

```bash
claude --plugin-dir /path/to/agent-ptt/plugins/announcer
```

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_PTT_URL` | `http://localhost:8770` | Agent PTT server |
| `AGENT_PTT_CHANNEL` | `Claude Code` | Channel to announce in |
| `AGENT_PTT_ANNOUNCE` | `1` | Set `0` to disable announcements |

For a persistent setup shared by Claude Code and Codex, create
`~/.agent-ptt/announcer.env` once:

```text
AGENT_PTT_URL=http://localhost:8770
AGENT_PTT_CHANNEL=Hackathon Demo
```

Shell environment variables override this file, so you can temporarily point
one terminal at another channel.

## Behavior notes

- **Never blocks coding.** The hooks run async, and every failure path
  (server down, network hiccup) exits silently. If the server isn't
  running, nothing happens.
- The first announcement per project may take a little longer while the
  voice is designed and pinned; after that it's instant.
- Participation keys are cached in `~/.agent-ptt/announcer-state.json`
  and refreshed automatically when the server restarts.
- Spectate from anywhere: `uv run agent-ptt listen <channel-id>`.
