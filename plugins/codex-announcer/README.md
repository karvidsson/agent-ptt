# Agent PTT Announcer — Codex plugin

The same Agent PTT integration for OpenAI's Codex CLI, shipped as the Codex
plugin `agent-ptt-announcer` in this repo's `agent-ptt` marketplace: Codex announces
**"I'll inspect the project and identify the entry point."** when you submit
a prompt and **"I fixed the login redirect. The tests pass."** when it finishes, in an
[Agent PTT](https://github.com/arvidsson-geins/agent-ptt) voice channel. Lines
are streamed to the channel's listeners (the web UI or `agent-ptt listen`); the
server host's speakers play them only when the server was started with
`--speakers` (it starts muted by default).

Each CLI session gets a random name (such as `Juniper` or `Atlas`) and a
voice from the saved voice library. The server stores the pair by CLI tool and
session ID, so reconnecting, changing channels, or restarting the server keeps
that session's identity. Separate sessions in the same project get distinct names.
Names of sessions that are no longer in any channel are handed to new sessions,
so the plain names (without a " 2" suffix) don't run out.

New assignments prefer manually created profiles and spread across the least-used
voices with an installed engine. If no saved voices are usable, a fallback voice
is created. Deleting a paired voice selects a replacement on the next join while
keeping the name. Existing transcript entries retain their original names.

## Requirements

- A reachable Agent PTT server, on this machine or another: set `AGENT_PTT_URL`
  (and `AGENT_PTT_API_KEY` if the server has one) in `~/.agent-ptt/announcer.env`
- Codex CLI 0.166 or newer (plugins and plugin hooks are on by default; no
  feature flag needed)
- `python3` on PATH (the hook scripts are stdlib-only)
- Optional: [Ollama](https://ollama.com) with `gemma2` for local spoken summaries
  (otherwise Codex authentication, so the short summarization call can run)

## Install

On any machine, no checkout needed:

```bash
codex plugin marketplace add karvidsson/agent-ptt
codex plugin add agent-ptt-announcer@agent-ptt
```

Codex copies the plugin into its cache (`~/.codex/plugins/cache/agent-ptt/…`)
and runs the hooks from there. The next time Codex starts it shows **Hooks need
review**: choose **Trust all**. Trust is tied to each hook's content, so Codex
asks again after every plugin upgrade.

To talk to a server on another machine, put its address in
`~/.agent-ptt/announcer.env` (shared with the Claude Code announcer):

```text
AGENT_PTT_URL=http://<server-host>:8770
AGENT_PTT_API_KEY=...        # only if the server has one
```

**Updating.** `codex plugin marketplace upgrade` pulls the latest marketplace
from GitHub; restart Codex and trust the changed hooks. From a local checkout
of this repo, run `python3 scripts/install_plugins.py --skip-claude` instead:
it registers the main checkout as the `agent-ptt` marketplace and reinstalls
the plugin, which refreshes the cached copy.

**Removing.** `codex plugin remove agent-ptt-announcer@agent-ptt`, and
`codex plugin marketplace remove agent-ptt` to drop the marketplace.

### Migrating from the old `~/.codex/hooks.json` install

Before 0.2.1 the announcer was installed as loose entries in
`~/.codex/hooks.json` (by `install.sh` or an older `install_plugins.py`), all
running scripts under `…/plugins/codex-announcer/`. With the plugin installed
too, every hook would fire twice, so remove them:

- With a checkout: `python3 scripts/install_plugins.py --skip-claude` installs
  the plugin and deletes those entries, keeping every other hook in the file.
- Installing from GitHub: open `~/.codex/hooks.json` and delete each entry whose
  command contains `codex-announcer/` (delete the file if nothing else is left).

Hook entries for the announcer in `~/.codex/config.toml` (`[[hooks.…]]` tables)
should be deleted the same way.

## Working with other agents

At session start the plugin joins the session to its project (git remote, else
the main worktree's folder name) and tells the agent who else is there and how
to reach them with its bundled `ptt` command (`ptt.py` in this plugin, printed
as an absolute path on each machine): `say`, `who`, `inbox`, `wait`, `tell`,
`ack`, `done`. Mentions and work items are delivered at hook boundaries; see
[plugins/README.md](../README.md#working-with-other-agents-announcer-020) for
the commands, waking idle sessions, and the `AGENT_PTT_HANDLE` /
`AGENT_PTT_ROLES` / `AGENT_PTT_WAKE` settings.

Only non-FYI mentions and requests wake an idle session; `ptt say --fyi` /
`ptt tell … --fyi` (status, acknowledgements, "done", claim releases) arrive at
the next prompt or tool hook. Close mentions are batched into one wake
(`AGENT_PTT_WAKE_BATCH_SECONDS`, default 4), and each delivery is one short
preamble line plus one line per message or item (see
[Quiet wake-ups](../README.md#working-with-other-agents-announcer-020)).

### Sandbox and network

Codex's `workspace-write` sandbox has no network, so every `ptt` call needs a
sandbox escalation. Codex's auto-reviewer can refuse one: it once rejected a
findings message as "exporting … to an unverified coordination service". The
supported fix is a one-time, per-machine **rules file**. When every part of a
command matches a `prefix_rule` with `decision = "allow"`, Codex runs it
outside the sandbox and without a prompt. The plugin prints the exact rule for
the machine it runs on:

```bash
mkdir -p ~/.codex/rules
python3 <this plugin's ptt.py> codex-rules > ~/.codex/rules/agent-ptt.rules
codex execpolicy check --rules ~/.codex/rules/agent-ptt.rules python3 <same ptt.py> who
# → {"matchedRules":[…],"decision":"allow"}; restart Codex
```

The rule looks like this:

```starlark
prefix_rule(
    pattern = [["/usr/bin/python3", "python3"], "/…/agent-ptt-announcer/…/ptt.py"],
    decision = "allow",
    justification = "Agent PTT: ptt posts to the PTT server at http://…:8770",
)
```

- **Plain commands only.** Codex matches the rule only against a command it can
  parse as plain words (`codex-rs/shell-command/src/bash.rs`,
  `try_parse_word_only_commands_sequence` at rust-v0.166.0): no backslashes
  (so no `'\''` apostrophe escapes), `$`, backticks, heredocs, redirections or
  subshells. Anything else runs sandboxed, fails to reach the server and asks
  for approval (seen live in task 5). So the session context tells Codex to
  double-quote plain text, and to send anything with quotes, backticks or
  newlines with `ptt say --file <path>` after writing the file (writing it needs
  no network). `ptt` prints the same hint when it runs inside the sandbox.
- **Exact tokens.** Tokens match exactly. The rule lists that machine's Python
  and `python3`, plus this copy of `ptt.py`. That copy lives in Codex's plugin
  cache, so run `codex-rules` again after a plugin update.
- **Where Codex looks.** Rules load from `rules/*.rules` in each config layer:
  `~/.codex/rules/`, and a trusted project's `.codex/rules/`.
- **When the bypass doesn't apply.** Codex skips it when the filesystem policy
  has denied-read entries, and some restricted models ignore allow rules.
- **Broader alternative.** `network_access = true` under
  `[sandbox_workspace_write]` in `~/.codex/config.toml` gives *every* sandboxed
  command network access.
- **A plugin can't do this for you.** A plugin manifest cannot declare rules or
  permissions, so the setup is the user's step. The plugin never edits
  `~/.codex`.

Sources (openai/codex at `rust-v0.166.0`), under
`https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/`:

- Rules loading: `core/src/exec_policy.rs:54-56`, `:664-685`.
- Allow means bypass the sandbox: `core/src/exec_policy.rs:441-456` and
  `core/src/tools/sandboxing.rs:259-277`.
- Rule syntax: `execpolicy/README.md`.
- Network access setting: `core/src/config/permissions.rs:103-108`.
- Plugin manifest fields: `core-plugins/src/manifest.rs:48-73`.

### How Codex shows a wake-up

These findings are from the same source:

- **Only a Stop block continues a turn.** A Stop hook starts another model turn
  only with `{"decision": "block", "reason": …}` (or exit code 2 with the
  reason on stderr). The reason becomes the continuation prompt
  (`hooks/src/events/stop.rs:296-339`, `core/src/session/turn.rs:760-778`).
- **The other Stop fields don't help.**
  - Stop accepts `continue`, `stopReason`, `suppressOutput`, `systemMessage`,
    `decision` and `reason`, and nothing else (`hooks/src/schema.rs:88-100`,
    `:452-465`, `deny_unknown_fields`).
  - `hookSpecificOutput.additionalContext` makes the Stop output invalid.
  - `continue: false` ends the turn.
  - `suppressOutput` is parsed and discarded (`stop.rs:281`).
- **A block always renders.** The TUI always shows a blocked run as
  "• Blocked by hook" followed by the full reason
  (`tui/src/history_cell/hook_cell.rs:271-341`). The quiet "↳ Hook · …" line
  exists only for runs that complete, and those can't continue the turn.
- **Prompt and tool hooks are quiet.**
  - `additionalContext` from UserPromptSubmit and PostToolUse is a context
    entry. The TUI never shows those (`hook_cell.rs:199-206`, `:517-524`).
  - `systemMessage` would add a visible line.
  - `suppressOutput` is rejected on PostToolUse
    (`hooks/src/engine/output_parser.rs:393-396`).
- **Other ways to start a turn are not used yet.**
  - `codex queue --thread <id> --message …` (`cli/src/queue_cmd.rs`,
    experimental) queues a user message that an idle TUI picks up within
    about 10 s (`ext/queue/src/service.rs:405-450`). It is untested live, so
    the plugin doesn't use it yet.
  - The app-server `turn/start` needs Codex launched with `--remote`.
  - `notify` only reports outward.

So the plugin keeps the Stop block, the only way to wake an idle Codex, and
makes it as small as it can be. It sends one block per batch. The block holds a
single preamble line and one compact line per message or item, and the full
rules sit once in the SessionStart context. Prompt and tool hooks keep using
`additionalContext`, which Codex doesn't display.

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
| `AGENT_PTT_API_KEY` | none | The server's shared key, when it has one |
| `AGENT_PTT_HANDLE`, `AGENT_PTT_ROLES`, `AGENT_PTT_WAKE` | — | Project identity and waking; see [Working with other agents](../README.md#working-with-other-agents-announcer-020) |

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
caveats when sending text to the model. If no result can be read (for example a
turn that only handed work to another agent), the hook says nothing rather than
claiming the work is finished or sounding like an error; the session is still
marked away in the channel.
`AGENT_PTT_SUMMARIZER=ollama` skips the CLI call, and `off` uses the prompt
text only.

## Behavior notes

- **Announcing never blocks Codex.** The script forks immediately after parsing
  the event, so the hook returns instantly while the announcement happens in
  the background; every failure path exits silently.
- **An idle Codex is woken with `codex queue` (0.4.0).** With Codex 0.166+
  (`codex queue --help` works), SessionStart starts a detached
  `ptt.py codex-watch` for the session, tied to the Codex process. It
  long-polls the inbox (`wake_only`) and, when a waking mention or work item
  arrives while the session is idle, runs
  `codex queue --thread <session> --message "<short neutral prompt>"`. The open
  TUI (embedded or daemon mode) starts a turn within about 10 s, and the
  UserPromptSubmit hook delivers the real messages with the trust rules: other
  agents' text never becomes a user message. The watcher never acks, skips busy
  turns (the hooks mark them, and PostToolUse/Stop deliver there), batches
  close mentions into one prompt, pokes once (Codex keeps queued input, so a
  second poke would only become a duplicate turn), and counts every prompt against `AGENT_PTT_WAKE_MAX_PER_HOUR` (default
  30). When the cap is reached it posts one text-only FYI saying until when. It
  exits with the Codex process, when its prompt goes unanswered for about 3.5
  minutes (the window was closed), or after 24 h; the next prompt
  or Stop restarts it if it died. Log: `~/.agent-ptt/logs/codex-watch-*.log`.
  `AGENT_PTT_CODEX_QUEUE=0` turns it off.
  **After Esc, Codex holds queued input until the user's next prompt**
  (`ext/queue/src/service.rs` at rust-v0.166.0: no dispatch while the thread is
  `Interrupted`). So a session its user interrupted can't be woken by others;
  when a poke isn't picked up within 90 s the watcher posts one text-only FYI
  ("… isn't picking up wake-ups …: new mentions wait for its next prompt"). In daemon mode (plain `codex`) the
  hooks run under Codex's shared app-server, so the watcher follows that and
  stops when the session's SessionEnd marks it ended. With the TUI closed nobody is
  woken: mentions wait for the next session (`codex exec resume` would run a
  second client on the thread, so the plugin doesn't). On Windows the watcher
  can't follow the Codex process and lives for its 24 h lifetime.
- **Without `codex queue` the Stop hook waits instead.** When someone else is
  present, `receive.py` keeps the Stop hook open for up to 240 s
  (`AGENT_PTT_STOP_WAIT_SECONDS`, `AGENT_PTT_WAKE=0` to turn off) and, if a
  mention or a work item addressed to this session arrives, continues the turn
  with it — capped at `AGENT_PTT_WAKE_MAX_PER_HOUR` wakes. The session-start
  context then tells the agent to let the user know it is reachable only for
  that long after each turn. Never on
  narration, notices, FYIs, notes, claims or your own messages; mentions within
  `AGENT_PTT_WAKE_BATCH_SECONDS` (4 s) of the first arrive as one wake.
  Interrupting Codex ends the wait. The woken turn's result is announced once
  when it stops.
- **Sessions leave when they end.** Codex CLI (0.166+) has a `SessionEnd`
  hook, which the plugin registers: it leaves the channel with the session's
  cached key (`POST /channels/{id}/leave` with `{"key_id": …}`, falling back to
  `?key_id=` on older servers), also leaves the mention-inbox participant if it
  differs, and drops the session's cached key, inbox state and status file.
  Nothing is spoken; requests time out after 2 seconds and failures are
  ignored. Older Codex versions without `SessionEnd` leave sessions in the
  roster until the channel is cleaned up.
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
and `Stop`. After upgrading the plugin (`codex plugin marketplace upgrade`, or
`python3 scripts/install_plugins.py` from a checkout), restart Codex and trust
the changed hooks. In the channel UI, type `@` and
select the agent. Idle sessions keep messages pending until their next hook
event. Set `AGENT_PTT_RECEIVE=0` to disable this independently of announcements.
[Delivery semantics and API](../../docs/mentions.md).
