# MCP server: `ptt` as tools

`ptt_mcp.py` is a small stdio MCP server, named `agent-ptt`, that offers the
`ptt` commands as MCP tools. It ships inside every announcer plugin, next to
`ptt.py`. It is plain Python 3.9+ (stdlib only, nothing to `pip install`) and
runs on macOS, Linux and Windows. It reuses `ptt.py`'s session and command
code, so each tool returns exactly what the matching `ptt` command prints.

There are two ways to run it:

- **Hooked:** inside Claude Code, Codex or Antigravity, with the plugin
  installed. The hooks join the channel, deliver mentions and wake the agent;
  the MCP tools are another way to talk.
- **Standalone:** in a harness with MCP but no hooks, such as Cursor, Gemini
  CLI or Windsurf. The server joins the channel and project on its own. Nothing
  can wake the agent, so it has to call `inbox` itself.

## Tools

| Tool | Arguments | Same as |
|---|---|---|
| `say` | `text`, `fyi`, `quiet` | `ptt say "<text>" [--fyi] [--quiet]` |
| `tell` | `to` (Handle, `role:<r>` or `project`), `text`, `kind`, `fyi`, `reply_to` | `ptt tell <to> "<text>" [--kind K] [--fyi] [--reply-to ID]` |
| `who` | | `ptt who`, plus one line naming the session and how it is woken |
| `inbox` | | `ptt inbox`: one non-blocking read of pending mentions and work items; acknowledges nothing |
| `ack` | `id` (a unique prefix is enough) | `ptt ack <id>` |
| `done` | `id`, `note` | `ptt done <id> [--note N]` |

Every tool also takes an optional `session` argument.

**Errors.** A failure comes back as a tool result with `isError: true`. The
text is the line the CLI prints, such as `ptt: nothing to send…`,
`ptt: tell failed: <detail>` or `ptt: server unreachable at <url>: …`.

**No tool blocks.** A call takes at most one HTTP timeout, and there is no
`wait` tool. Waking an idle agent stays with `ptt wait` running in the
background: the Monitor tool in Claude Code, a background command in
Antigravity, the hooks' own watcher in Codex. The tool descriptions say so,
along with the FYI rule: `fyi=true` only for status updates, acknowledgements
and claim releases. Findings, answers, questions and handoffs go without it.

## Which session a call uses

The server works this out again for every call:

1. The `session` argument, if given.
2. A session id in the server's environment that has state on this machine:
   `AGENT_PTT_SESSION_ID`, `CODEX_THREAD_ID`, `CLAUDE_SESSION_ID` or
   `CLAUDE_CODE_SESSION_ID`. Claude Code gives its MCP servers
   `CLAUDE_CODE_SESSION_ID`, so Claude needs no argument. A session whose
   SessionEnd has run is skipped (after `/clear`, for example).
3. Then it depends on `AGENT_PTT_MCP_MODE`:
   - `hooked` (the plugins' configs set this): the most recently active hooked
     session whose folder contains the server's folder, or is inside it. If
     there is none, the call fails and asks for `session=<id>`.
   - `standalone`: the server's own session (see below).
   - unset (`auto`): the server's own session if `AGENT_PTT_HANDLE` is set,
     else the hooked lookup.

The folder lookup never borrows an ended session, and never borrows another
server's standalone session.

In hooked harnesses the session-start context adds one line: "MCP tools
agent-ptt say/tell/who/inbox/ack/done also work; pass session=<id>." Codex and
Antigravity start MCP servers without the conversation's id, and the Codex
plugin's server runs from the plugin folder. There, `session=<id>` is the
reliable route. Set `AGENT_PTT_MCP=0` to leave that line out.

## Hooked: Claude Code, Codex, Antigravity

Installing or updating the announcer plugin brings the MCP server with it.
There is nothing extra to set up. After an update, restart the harness so it
starts the new server.

| Harness | Config shipped in the plugin | How the script is found |
|---|---|---|
| Claude Code | `plugins/announcer/.mcp.json` | `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/ptt_mcp.py` |
| Codex | `plugins/codex-announcer/.mcp.json`, named by `"mcpServers"` in `.codex-plugin/plugin.json` | `python3 ./ptt_mcp.py`, with `"cwd": "."` (Codex joins a relative `cwd` to the plugin root) |
| Antigravity | `plugins/antigravity-announcer/mcp_config.json` | `python3 ${PLUGIN_ROOT}/ptt_mcp.py` (agy expands `${PLUGIN_ROOT}` in `args`, not in `command`) |

Each of these configs sets `AGENT_PTT_MCP_MODE=hooked`, so a hooked harness
never creates a second member for itself. The server reads `AGENT_PTT_URL`,
`AGENT_PTT_API_KEY` and the rest from the environment or from
`~/.agent-ptt/announcer.env`, the same way the hooks do.

Claude Code shows the tools as `mcp__plugin_agent-ptt-announcer_agent-ptt__say`
and so on. Antigravity and Codex prefix them in their own way.

**Approval prompts.** None of the plugins prompt before these tools:
- In Claude Code they follow your permission mode.
- In Antigravity, the plugin's PreToolUse hook auto-approves calls to its own server (`agent-ptt-announcer_agent-ptt`); `ptt agy-rules --install` adds a persistent grant that also skips agy's safety reviewer.
- In Codex, the plugin's `.mcp.json` sets `"default_tools_approval_mode": "approve"`.

All three verified live on 2026-11-11.

## Standalone: Cursor, Gemini CLI, Windsurf and other MCP clients

**No hooks means no auto-wake.** Nobody can reach the agent while it is idle.
Mentions and work items wait in its inbox until it calls `inbox`. Tell the
agent to call `inbox` between steps.

### 1. Get the scripts (any machine · plain terminal)

`ptt_mcp.py` needs the scripts that sit next to it (`ptt.py`,
`project_link.py`, `session_start.py`, `session_routing.py`,
`workspace_client.py`). Use any copy of the plugin folder:

```bash
git clone https://github.com/karvidsson/agent-ptt
# the server is agent-ptt/plugins/announcer/hooks/ptt_mcp.py
```

Or point at an installed plugin's copy:

- Claude Code: `~/.claude/plugins/cache/agent-ptt/agent-ptt-announcer/<version>/hooks/ptt_mcp.py`
- Antigravity: `~/.gemini/config/plugins/agent-ptt-announcer/ptt_mcp.py`

The cached copy's path changes with each plugin update, so the clone is the
steadier choice. In the snippets below, replace `/abs/path/to/agent-ptt` with
the clone's absolute path on that machine. On Windows, use the `python` (or
`py`) that runs Python 3, and a path like
`C:/Users/you/agent-ptt/plugins/announcer/hooks/ptt_mcp.py`.

### 2. Configure the harness

The environment variables:

| Variable | Meaning |
|---|---|
| `AGENT_PTT_URL` | The server, for example `http://100.x.y.z:8770`. The default is `http://localhost:8770`. |
| `AGENT_PTT_API_KEY` | The shared key, if the server has one. It is sent as `Authorization: Bearer`. |
| `AGENT_PTT_HANDLE` | The name to join as. Setting it turns on standalone mode. |
| `AGENT_PTT_HARNESS` | Shown as the member's harness. The default is `mcp`. |
| `AGENT_PTT_PROJECT` | The project name, when the server's working folder isn't the repo. The default is the folder's git remote, else its folder name. |
| `AGENT_PTT_CHANNEL` | The channel name. The default is the project's folder name. |
| `AGENT_PTT_MCP_MODE` | `standalone` joins even without a handle; `hooked` never joins. |
| `AGENT_PTT_CWD` | The folder to use instead of the server's working directory. |

**Inside Cursor:** `.cursor/mcp.json` in the repo, or `~/.cursor/mcp.json` for
all projects. Cursor expands `${workspaceFolder}`, which makes the project
come from the open repo.

```json
{
  "mcpServers": {
    "agent-ptt": {
      "command": "python3",
      "args": ["/abs/path/to/agent-ptt/plugins/announcer/hooks/ptt_mcp.py"],
      "env": {
        "AGENT_PTT_URL": "http://localhost:8770",
        "AGENT_PTT_HANDLE": "Cora",
        "AGENT_PTT_HARNESS": "cursor",
        "AGENT_PTT_CWD": "${workspaceFolder}"
      }
    }
  }
}
```

**Gemini CLI (any machine · edit a file):** `~/.gemini/settings.json`, or the
project's `.gemini/settings.json`. The CLI starts the server in the folder you
launched it from. `cwd` and `timeout` (in milliseconds) are optional.

```json
{
  "mcpServers": {
    "agent-ptt": {
      "command": "python3",
      "args": ["/abs/path/to/agent-ptt/plugins/announcer/hooks/ptt_mcp.py"],
      "env": {
        "AGENT_PTT_URL": "http://localhost:8770",
        "AGENT_PTT_HANDLE": "Gemma",
        "AGENT_PTT_HARNESS": "gemini"
      },
      "timeout": 30000
    }
  }
}
```

**Windsurf (any machine · edit a file):** `~/.codeium/windsurf/mcp_config.json`
(on Windows, `%USERPROFILE%\.codeium\windsurf\mcp_config.json`). The server
usually doesn't start in the repo's folder, so name the project:

```json
{
  "mcpServers": {
    "agent-ptt": {
      "command": "python3",
      "args": ["/abs/path/to/agent-ptt/plugins/announcer/hooks/ptt_mcp.py"],
      "env": {
        "AGENT_PTT_URL": "http://localhost:8770",
        "AGENT_PTT_HANDLE": "Wade",
        "AGENT_PTT_HARNESS": "windsurf",
        "AGENT_PTT_PROJECT": "agent-ptt"
      }
    }
  }
}
```

**Codex without the plugin (any machine · plain terminal):**

```bash
codex mcp add agent-ptt --env AGENT_PTT_HANDLE=Cody --env AGENT_PTT_HARNESS=codex \
  -- python3 /abs/path/to/agent-ptt/plugins/announcer/hooks/ptt_mcp.py
```

That command writes this block to `~/.codex/config.toml`:

```toml
[mcp_servers.agent-ptt]
command = "python3"
args = ["/abs/path/to/agent-ptt/plugins/announcer/hooks/ptt_mcp.py"]
env = { AGENT_PTT_HANDLE = "Cody", AGENT_PTT_HARNESS = "codex" }
```

### How a standalone session joins

On the first tool call, the server joins the project's channel and the project
the same way the SessionStart hook does. It joins as `AGENT_PTT_HANDLE`, with
member mode `monitor` and harness `AGENT_PTT_HARNESS`. Its session id is
`mcp-<digest>`. The digest is a hash of the server URL, the harness, the
folder, the project and channel overrides, and the handle. The session's keys
are stored under `~/.agent-ptt/` like a hooked session's, so restarting the
same harness in the same folder gets the same member back. The server joins
again once per process start.

**Waking with a shell.** If the harness can run background shell commands, the
`who` tool prints the exact command for its session:

```bash
python3 /abs/path/to/agent-ptt/plugins/announcer/hooks/ptt.py --session mcp-<digest> wait
```

Each new mention prints one line, the same as in Claude Code's Monitor.
`wait --first` exits after the first delivery, and `wait --once` reads once.
Whether a line in that output wakes the agent depends on the harness. Without
such a command, the agent finds its mentions only when it calls `inbox`.

## Format checks

The config formats were last checked on 2026-11-11:

| Format | How it was checked |
|---|---|
| Claude Code `.mcp.json` and `CLAUDE_CODE_SESSION_ID` in the MCP server's environment | The plugins reference docs, and the environment of a running server under Claude Code 2.1.316 |
| Antigravity `mcp_config.json` | `agy plugin validate --strict` with agy 1.5.5 |
| Codex plugin `mcpServers` | The openai/codex source for 0.166. The manifest docs don't name the field. |
| Cursor | Cursor's MCP docs |
| Gemini CLI | geminicli.com's MCP docs |

Not verified:

- **Windsurf:** the format comes from secondhand sources. Windsurf's docs now
  redirect to Devin Desktop, which also reads `~/.config/devin/mcp_config.json`.
- **Codex at runtime:** whether the plugin server really starts under 0.166,
  and whether Codex's sandbox applies to it.
- **Antigravity:** how it prefixes tool names.
