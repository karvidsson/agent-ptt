# Agent PTT Announcer — Codex CLI hooks

The same Agent PTT integration for OpenAI's Codex CLI: your Mac announces
**"I'll inspect the project and identify the entry point."** when you submit
a prompt and **"I've finished working on it."** when Codex finishes, through an
[Agent PTT](https://github.com/arvidsson-geins/agent-ptt) voice channel.

Codex sessions join as `Codex · <folder>`, so Codex gets different
auto-designed voices than Claude Code — you can tell the two agents (and
every project) apart by ear.

## Requirements

- An Agent PTT server running locally: `uv run agent-ptt server start`
- Codex CLI with hooks support
- `python3` on PATH (the hook script is stdlib-only)
- Optional: [Ollama](https://ollama.com) with `gemma2` for local spoken summaries
  (otherwise Codex authentication, so the short summarization call can run)

## Install

```bash
python3 scripts/install_plugins.py --skip-claude
```

This merges the announcer hooks into `~/.codex/hooks.json`, pointing at the
repo's main checkout even if you run it from a git worktree. Other hooks in
the file are kept, and re-running it replaces stale announcer entries
instead of duplicating them.

`./plugins/codex-announcer/install.sh` still works for a first install, but
it uses the checkout it lives in and refuses to touch an existing
`hooks.json`.

### Alternative: inline in `~/.codex/config.toml`

```toml
[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = 'AGENT_PTT_AGENT=Codex python3 "/absolute/path/to/agent-ptt/plugins/codex-announcer/announce.py"'
timeout = 60

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = 'AGENT_PTT_AGENT=Codex python3 "/absolute/path/to/agent-ptt/plugins/codex-announcer/announce.py"'
timeout = 60
```

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_PTT_URL` | `http://localhost:8770` | Agent PTT server |
| `AGENT_PTT_CHANNEL` | `Claude Code` | Channel to announce in |
| `AGENT_PTT_AGENT` | `Claude` | Speaker name prefix (set to `Codex` by the hook entries) |
| `AGENT_PTT_ANNOUNCE` | `1` | Set `0` to disable announcements |
| `AGENT_PTT_SUMMARIZER` | `auto` | `auto` (Ollama, then the agent CLI), `ollama`, `agent`, or `off` |
| `AGENT_PTT_OLLAMA_MODEL` | `gemma2` | Local model for spoken summaries |
| `AGENT_PTT_OLLAMA_URL` | `http://localhost:11434` | Ollama server |

Claude and Codex share the persistent settings in
`~/.agent-ptt/announcer.env`, so configure the channel once instead of
editing Codex hooks:

```text
AGENT_PTT_URL=http://localhost:8770
AGENT_PTT_CHANNEL=Hackathon Demo
```

Shell environment variables override this file for temporary experiments.

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

- **Never blocks Codex.** The script forks immediately after parsing the
  event, so the hook returns instantly while the announcement happens in
  the background; every failure path exits silently.
- Without Ollama, the hook uses `codex exec --sandbox read-only` for its short
  start-summary call, so Codex works without Claude installed. If Codex is
  unavailable, it falls back to the original prompt.
