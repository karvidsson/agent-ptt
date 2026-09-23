# Agent PTT Announcer — Claude Code plugin

Makes your Mac announce what Claude Code is working on, through an
[Agent PTT](https://github.com/arvidsson-geins/agent-ptt) voice channel:

- When you submit a prompt: **"I'll track down why the login redirect breaks and fix it."**
- When Claude finishes: **"I fixed the redirect and added a test, and everything passes."**

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
| `AGENT_PTT_SUMMARIZER` | `auto` | `auto` (Ollama, then the agent CLI), `ollama`, `agent`, or `off` |
| `AGENT_PTT_OLLAMA_MODEL` | `gemma2` | Local model for spoken summaries |
| `AGENT_PTT_OLLAMA_URL` | `http://localhost:11434` | Ollama server |

For a persistent setup shared by Claude Code and Codex, create
`~/.agent-ptt/announcer.env` once:

```text
AGENT_PTT_URL=http://localhost:8770
AGENT_PTT_CHANNEL=Hackathon Demo
```

Shell environment variables override this file, so you can temporarily point
one terminal at another channel.

## Spoken summaries (local LLM)

The announcer rewrites your prompt as what the agent says as it starts
("I'll check whether Redis is what we use for caching."). When the agent
finishes, it turns the final report into one spoken sentence.

With [Ollama](https://ollama.com) running, this is done by a small local
model. Each summary takes under a second once the model is loaded, and it
costs no tokens:

```bash
ollama pull gemma2
```

`gemma2` (9B) is the default because it reports outcomes faithfully. The 4B
models we tried were faster, but they sometimes invented results ("removed
the branch" when the report said the cleanup didn't run). Pick another
model with `AGENT_PTT_OLLAMA_MODEL`. Thinking models like `qwen3` spend
their output on reasoning and don't work here.

If Ollama isn't running, the start summary falls back to a one-shot call
to the agent's own CLI, and then to the prompt's first sentence. The end
summary falls back to the first sentence of the report.
`AGENT_PTT_SUMMARIZER=ollama` skips the CLI call, and `off` uses the prompt
text only.

## Behavior notes

- **Never blocks coding.** The hooks run async, and every failure path
  (server down, network hiccup) exits silently. If the server isn't
  running, nothing happens.
- The first announcement per project may take a little longer while the
  voice is designed and pinned; after that it's instant.
- Participation keys are cached in `~/.agent-ptt/announcer-state.json`
  and refreshed automatically when the server restarts.
- Spectate from anywhere: `uv run agent-ptt listen <channel-id>`.
