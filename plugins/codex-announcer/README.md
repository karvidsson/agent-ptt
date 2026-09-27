# Agent PTT Announcer — Codex CLI hooks

The same Agent PTT integration for OpenAI's Codex CLI: your Mac announces
**"I'll inspect the project and identify the entry point."** when you submit
a prompt and **"I fixed the login redirect. The tests pass."** when Codex finishes, through an
[Agent PTT](https://github.com/arvidsson-geins/agent-ptt) voice channel.

Each CLI session gets a random name (such as `Juniper` or `Atlas`) and a
voice from the saved voice library. The server stores the pair by CLI tool and
session ID, so reconnecting, changing channels, or restarting the server keeps
that session's identity. Separate sessions in the same project get distinct names.

New assignments prefer manually created profiles and spread across the least-used
voices with an installed engine. If no saved voices are usable, a fallback voice
is created. Deleting a paired voice selects a replacement on the next join while
keeping the name. Existing transcript entries retain their original names.

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
| `AGENT_PTT_CHANNEL` | Project name | Process-local channel override; ignored in the shared config file |
| `AGENT_PTT_AGENT` | `Claude` | CLI identity namespace (set to `Codex` by the hook entries) |
| `AGENT_PTT_ANNOUNCE` | `1` | Set `0` to disable announcements |
| `AGENT_PTT_SUMMARIZER` | `auto` | `auto` (Ollama, then the agent CLI), `ollama`, `agent`, or `off` |
| `AGENT_PTT_OLLAMA_MODEL` | `gemma2` | Local model for spoken summaries |
| `AGENT_PTT_OLLAMA_URL` | `http://localhost:11434` | Ollama server |

Claude and Codex share the persistent settings in
`~/.agent-ptt/announcer.env`, for server and summarizer settings:

```text
AGENT_PTT_URL=http://localhost:8770
```

Shell environment variables override this file for temporary experiments.

By default, announcements go to a channel named after the current project.
Git subfolders and linked worktrees use the main checkout's name; outside Git,
the working folder's name is used. Claude and Codex in the same project share
that channel. Projects with the same folder name also share a channel.

Channel selections are saved per session, not globally. Use the channel skill
with `use <name>` to find or create a room for the current session, or `auto` to
return to repo/folder routing. Other sessions keep their own routes. Selection
changes apply on the next hook event without restarting the CLI.

The old `AGENT_PTT_CHANNEL` entry in `~/.agent-ptt/announcer.env` is ignored.
A process environment `AGENT_PTT_CHANNEL` remains an explicit local override;
a saved session selection takes precedence. `auto` also overrides that environment
setting for the session. Server and summarizer settings still use the shared file.

## Progress updates

Between start and finish, the hook also speaks what the agent is doing, so a
long turn is never silent:

- **The agent's own narration.** When the agent writes something to you before
  its next tool call ("Codex is editing the same file, so I'll wait"), that
  sentence is spoken once, at the next completed tool call.
- **Intent, not tool names.** Tool calls are queued and spoken as one sentence:
  **"I'm about to run the test suite, then edit audio.py."** Shell calls use
  the agent's own description of the command, edits name the file, and
  searches are grouped as "look through the code". Three queued intents or
  45 seconds of silence trigger an update.
- Background task notifications (monitors, subagent results) reach the prompt
  hook too, but are not announced — only what you actually typed is.

## Ask for status in the channel

A second hook script, `status.py`, runs next to the announcer on the same
events. It records what each session is doing (task, elapsed time, latest
narration, next tool call) in `~/.agent-ptt/status/`, and keeps one small
responder process per channel alive. When anyone in the channel asks
**"status?"**, **"what are you working on?"** or mentions a session by name
(**"@Hazel status?"**), the agent answers in its own voice:

> I've been on 'fix the flaky login test' for 12 minutes. I just said: the
> cause is a shared fixture. Right now I'm about to run the tests.

After a turn ends it answers with the outcome: *"I finished 'fix the flaky
login test' three minutes ago. Fixed it, 12 tests pass."*

Set `AGENT_PTT_STATUS=0` to record status without starting the responder.
The responder exits on its own an hour after the last session in its channel
reported anything.

## Spoken summaries (local LLM)

The announcer rewrites your prompt as what the agent says as it starts
("I'll check whether Redis is what we use for caching."). When the agent
finishes, it turns the final report into one or two short spoken sentences: the
task and outcome, followed by verification or remaining work when reported.

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
summary falls back to extracts from the report, prioritizing a blocker or caveat
over another accomplishment. Long reports use this fallback to avoid losing
caveats when sending text to the model. If no result can be read, the hook says
so instead of claiming the work is finished.
`AGENT_PTT_SUMMARIZER=ollama` skips the CLI call, and `off` uses the prompt
text only.

## Behavior notes

- **Never blocks Codex.** The script forks immediately after parsing the
  event, so the hook returns instantly while the announcement happens in
  the background; every failure path exits silently.
- Without Ollama, the hook uses `codex exec --sandbox read-only` for its short
  start-summary call, so Codex works without Claude installed. If Codex is
  unavailable, it falls back to the original prompt.

Completion extraction supports Claude assistant messages and Codex final-answer
and task-complete records. Codex commentary, tool arguments, and reasoning are
excluded. Spoken fallback text removes markdown and replaces full paths with
filenames; detailed paths remain in the original agent report.

## Receive channel mentions

The synchronous `receive.py` hook registers this session as a mention receiver
and delivers explicit `@name` requests on `UserPromptSubmit`, `PostToolUse`,
and `Stop`. Refresh installed plugins with `python3 scripts/install_plugins.py`,
restart Codex, and trust the changed hooks. In the channel UI, type `@` and
select the agent. Idle sessions keep messages pending until their next hook
event. Set `AGENT_PTT_RECEIVE=0` to disable this independently of announcements.
[Delivery semantics and API](../../docs/mentions.md).
