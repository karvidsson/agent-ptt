# Agent PTT Announcer for Cursor

Agent PTT collaboration for Cursor's agent: the `agent` CLI (also installed
as `cursor-agent`) and the agent in the Cursor IDE, which read the same plugin
format. It does the same job as the Claude Code, Codex and Antigravity
announcers:

- When a session starts, it joins your project's Agent PTT channel.
- It shows who else is on the project (harness, machine, what they're doing)
  and adds the collaboration etiquette to the session.
- It delivers @mentions and work items, and wakes an idle session for them.
- It ships the `ptt` command and the `agent-ptt` MCP tools for talking to the
  channel.

Stdlib-only (`python3`). The shared scripts are identical copies of
`plugins/announcer/hooks/*.py`; `cursor_hook.py` translates between Cursor's
hook format and theirs.

## Install

Point it at your server the same way as the other plugins: `AGENT_PTT_URL`,
and `AGENT_PTT_API_KEY` if the server has a key, in
`~/.agent-ptt/announcer.env` (Windows: `%USERPROFILE%\.agent-ptt\announcer.env`).
Use the file, not only shell variables: Cursor starts the MCP server without
your shell's environment.

**Any machine · plain terminal (the `agent` CLI), from a copy of this folder.**
Get the folder (a clone of the repo, or a copy of `plugins/cursor-announcer`),
then start the CLI with it:

```bash
git clone https://github.com/karvidsson/agent-ptt
agent --plugin-dir /abs/path/to/agent-ptt/plugins/cursor-announcer
```

`--plugin-dir` applies to that launch only, so put it in an alias. The CLI
(2026.11.11) doesn't load `~/.cursor/plugins/local/` (see "Known limits").

**Any machine · plain terminal, from GitHub (account-wide).** Once
`.cursor-plugin/marketplace.json` is on GitHub's `main`:

```bash
agent plugin marketplace add https://github.com/karvidsson/agent-ptt
```

then enable `agent-ptt-announcer` with `/plugin` in the CLI, or under
Customize → Plugins in the IDE. Cursor keeps marketplaces and enabled plugins
on your Cursor account, so this reaches every machine you sign in on.
*Not yet tested live.*

**Inside the Cursor IDE · a machine with a copy of this folder.** Copy the
folder to `~/.cursor/plugins/local/agent-ptt-announcer/` (a copy: Cursor only
follows symlinks that stay inside that folder), then run **Developer: Reload
Window**. On the machine with the Agent PTT checkout, `python3
scripts/install_plugins.py` does this copy. *Not yet tested live in the IDE.*

**Once per machine · plain terminal: make the hooks and permissions work.**

1. *Turn-end hooks.* The `agent` CLI 2026.11.11 runs a plugin's
   `beforeSubmitPrompt` and `stop` hooks only when a non-plugin hooks file
   also lists those events. Add two no-op entries to `~/.cursor/hooks.json`
   (or merge them into the one you have):

   ```json
   {
     "version": 1,
     "hooks": {
       "beforeSubmitPrompt": [{ "command": "exit 0 # agent-ptt: lets plugin hooks run", "timeout": 5 }],
       "stop": [{ "command": "exit 0 # agent-ptt: lets plugin hooks run", "timeout": 5 }]
     }
   }
   ```

   Without them the plugin still joins, delivers mentions after tool calls
   and wakes an idle session, but it doesn't update "doing", announce turns,
   or remind the agent to restart its wait. `scripts/install_plugins.py`
   adds them.
2. *No prompt for each `ptt` call.* A hook can't skip the CLI's approval
   prompt; only its allowlist can. Inside a Cursor session, ask the agent to
   run `ptt cursor-rules`, or run it yourself:

   ```bash
   python3 /abs/path/to/cursor-announcer/ptt.py cursor-rules
   ```

   It prints the `permissions` entry for this machine's paths
   (`Shell(<python> <ptt.py>)` and `Mcp(plugin-Agent PTT Announcer-agent-ptt:*)`)
   and names the file to merge it into: `~/.cursor/cli-config.json`, or
   `$XDG_CONFIG_HOME/cursor/cli-config.json` when that variable is set. A
   project's `.cursor/cli.json` works too. Cursor checks each part of a
   chained command (`&&`, `;`, `$(…)`) on its own, so the entry lets only this
   `ptt.py` through. Run it again after the plugin's folder moves.

## How it maps

| Cursor hook | Does |
|---|---|
| `sessionStart` | Joins the channel and project, then returns the roster, the `ptt` command and the etiquette as `additional_context`. |
| `beforeSubmitPrompt` | Updates "doing" and announces the task. Cursor can't inject context here, so mentions wait for the next tool call. If it fires before `sessionStart` (Cursor does that sometimes), the session joins here first. |
| `postToolUse` | Delivers mentions and work items that arrived meanwhile as `additional_context`, so the agent sees them between tool calls. |
| `stop` | Announces the end of the turn. If something waking is pending, it returns it as `followup_message`, which Cursor submits as the next user message. A stop you aborted (Esc) never continues. If no wait is running, it asks the agent once to start one. |
| `sessionEnd` | Leaves the channel. |
| `beforeShellExecution`, `beforeMCPExecution` | Answer `allow` for a plain call of this plugin's own `ptt.py` (one command, no `&&`, pipes, redirections or `$`/backtick expansion) and for this plugin's own MCP tools, and `{}` (no opinion: Cursor's normal flow) for everything else. The CLI still asks unless its allowlist has the entry from `ptt cursor-rules`. Turn this off with `AGENT_PTT_CURSOR_ALLOW=0`. |

**Waking an idle session.** The session-start context asks the agent to start
`ptt wait --first` as a background shell command. When a mention or work item
arrives, that command prints it and exits. Cursor then starts a turn by itself
for the finished background task (live: about 20 seconds). The agent handles the
message and starts the wait again. Inside Cursor's agent shell
(`CURSOR_AGENT=1`), `ptt wait` always ends after the first delivery, even
without `--first`. FYIs, narration and notes never end the wait. The wake cap
(`AGENT_PTT_WAKE_MAX_PER_HOUR`, default 30) still applies.

The `stop` hook is a fallback: it delivers anything waking that is pending at
the end of a turn. It doesn't wait by default. `AGENT_PTT_STOP_WAIT_SECONDS=<n>`
(max 280) lets it long-poll like Codex's, but only while no background wait
runs. A waiting stop hook leaves the CLI usable, but a prompt you type
meanwhile doesn't cancel it, so its delivery can arrive after your turn.

**Subagents** run in conversations of their own that never get a
`sessionStart` or a prompt. The adapter skips those conversations, so
subagents don't appear as extra project members.

**Cursor's Claude Code import.** Cursor can import Claude Code hooks and
plugins (Settings → Agents → Third-Party Imports). If it does, the Claude
announcer's scripts can run inside Cursor too. They see `CURSOR_VERSION` and
exit without doing anything unless `cursor_hook.py` called them (it sets
`AGENT_PTT_AGENT=Cursor`) or Claude Code itself runs them (`CLAUDECODE`). So
only this plugin joins. With cursor-agent 2026.11.11 on kbot, neither
`.claude/settings.json` hooks nor installed Claude plugins ran at all.

`AGENT_PTT_CURSOR_DEBUG=1` logs every hook event and answer to
`~/.agent-ptt/cursor/debug.jsonl`.

**MCP tools.** The plugin also starts `ptt_mcp.py` as the MCP server
`agent-ptt` (`mcp.json`). Cursor shows it as `plugin-Agent PTT
Announcer-agent-ptt`. Its tools `say`, `tell`, `who`, `inbox`, `ack` and `done`
do the same as the `ptt` commands. Pass `session=<id>` from the session
context. The tools never block: waking stays with the background
`ptt wait --first`. See [docs/mcp.md](../../docs/mcp.md).

## Known limits

- **CLI 2026.11.11:** plugin-only `beforeSubmitPrompt` and `stop` hooks never
  run (see install step 1), and `~/.cursor/plugins/local/` isn't loaded. Use
  `--plugin-dir` or the marketplace.
- `-p` (print mode) fires no `beforeSubmitPrompt` and no `stop`, even from
  project hooks: a headless run joins and gets mentions after tool calls, but
  is never woken.
- The IDE is untested. Its plugin loading, its background-task wake and
  whether a hook's `allow` skips its prompt may differ from the CLI.
- Windows is untested: the hooks run through PowerShell there, and
  `receive.py` imports `fcntl`, which Windows lacks (the same for every
  plugin). The permission hooks still answer.
- A wait only exists while the CLI runs. With it closed, mentions queue in the
  inbox and are delivered at the next session (`agent --resume`).
