# Plan: Announcer support for GitHub Copilot CLI and Google Antigravity

> Status: proposal, 2026-09-27. Research done, nothing built.

## Summary

Both tools have a hook system close enough to Claude Code's that the existing
announcer (`plugins/announcer/hooks/announce.py` + `status.py`) can serve them
with a thin adapter instead of a new plugin. The work is mostly: read a
different config file layout, normalize event names and payload fields, and
parse a different transcript format for the "Stop" summary.

| | Claude Code | Codex CLI | Copilot CLI | Antigravity (agy CLI + IDE) |
|---|---|---|---|---|
| Config | plugin `hooks.json` | `~/.codex/hooks.json` | `~/.copilot/hooks/*.json` or `~/.copilot/settings.json`; repo `.github/hooks/*.json` | `~/.gemini/config/hooks.json` (global) or `<repo>/.agents/hooks.json` (workspace, wins) |
| Events we need | UserPromptSubmit, PreToolUse, PostToolUse, Stop | same | `userPromptSubmitted`, `preToolUse`, `postToolUse`, `agentStop`, plus `sessionStart`/`sessionEnd` | `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop` |
| Session id | `session_id` | `thread_id` | `sessionId` / `session_id` | `conversationId` |
| Prompt text | `prompt` | `prompt` | `prompt` | not in payload; read `USER_INPUT` from transcript |
| Tool fields | `tool_name`, `tool_input` | same | `toolName`, `toolArgs` (or snake_case) | `toolCall.name`, `toolCall.args`, `toolCall.toolSummary` |
| Transcript | JSONL, `type: assistant` | JSONL | `transcriptPath` on `agentStop` | `transcriptPath`, JSONL with `type: PLANNER_RESPONSE` / `USER_INPUT`, top-level `content` |
| Stdout | ignored | ignored | JSON object; `agentStop` accepts `{"decision": "allow"}` | JSON object required; `{}` for most, `{"decision": "allow"}` for PreToolUse, `{"decision": "stop"}` for Stop |
| Async | `"async": true` | detach via fork | blocking, `timeoutSec` (default 30, fail-open) | blocking, `timeout` seconds |

Krille's machine already has both hooked by herdr: `~/.copilot/settings.json`
has a `SessionStart` hook and `~/.gemini/config/hooks.json` has a `PreInvocation`
hook. Both are `type: command`, so the merge logic must add our entries next to
them instead of overwriting, the same way `scripts/install_plugins.py` merges
Codex hooks.

## GitHub Copilot CLI

Docs: hooks reference and "Using hooks with Copilot CLI" on docs.github.com.

- **Format.** `{"version": 1, "hooks": {"<event>": [{"type": "command", "bash": "...", "powershell": "...", "timeoutSec": 10}]}}`. Event names are camelCase, PascalCase aliases are accepted for VS Code compatibility.
- **Where.** User level `~/.copilot/hooks/agent-ptt.json` is the right place for us: a separate file, no merge needed, loaded on CLI restart. Repo level `.github/hooks/*.json` also works and is the only source the cloud coding agent honors.
- **Payload.** Both camelCase and snake_case variants exist depending on the event. The adapter should read `sessionId` or `session_id`, `toolName` or `tool_name`, `toolArgs` or `tool_input`, `transcriptPath` or `transcript_path`. `agentStop` carries `stop_hook_active` like Claude Code, so the existing guard applies.
- **Extra events worth using.** `sessionStart` and `sessionEnd` (with `reason`) give the explicit open and close signals the presence idea needs. `subagentStart` / `subagentStop` can be filtered out so subagent chatter does not flip status.
- **Blocking.** Hooks block until the command exits. `announce.py` already forks and lets the parent exit immediately, so this is fine.
- **Stdout.** Print `{}` on every path. The current script prints nothing, which Copilot tolerates, but an explicit empty object is safer.
- **Scope.** Docs cover the CLI only. VS Code Copilot agent mode reads the PascalCase names from the same repo files, unverified. The Copilot desktop app is not documented, treat as unsupported until tested.

## Google Antigravity

Sources: Mete Atamel's "Where does Antigravity look for hooks?", the I/O 2026 feature deep dive, and the claude-mem issue #4057 that reverse engineered the payloads.

- **Format.** Top level is a named group, then events. `PreInvocation`, `PostInvocation` and `Stop` are flat lists of `{"type": "command", "command": "...", "timeout": 30}`. `PreToolUse` and `PostToolUse` are grouped: `[{"matcher": "*", "hooks": [{"type": "command", "command": "..."}]}]`. Getting this wrong makes agy reject the whole file, several plugins have hit that.
- **Where.** Global `~/.gemini/config/hooks.json` (already exists here with herdr's group, so we add an `agent-ptt` group next to it) or per-repo `<repo>/.agents/hooks.json`. Plugin form `~/.gemini/config/plugins/<name>/hooks.json` also exists. All three flavours (IDE agent manager, agy CLI, SDK) read the same locations.
- **Payload.** camelCase protojson. Every event has `conversationId`, `transcriptPath`, `workspacePaths` (array) and `artifactDirectoryPath`. Tool events have `stepIdx` and `toolCall` with `name`, `args`, and on PostToolUse `toolAction` and `toolSummary`. `Stop` has `terminationReason`, `fullyIdle` and `error`.
- **No prompt in the payload.** `PreInvocation` only carries `invocationNum` and `initialNumSteps`. The "I'm going to ..." line has to come from the last `USER_INPUT` record in the transcript, and only when `invocationNum` is 1 for the turn so we do not re-announce on every model call.
- **Transcript.** JSONL with `type: PLANNER_RESPONSE` or `USER_INPUT`, `source: MODEL` or `USER_EXPLICIT`, and `content` at top level. `_last_assistant_text` needs a second parser branch.
- **Stdout is mandatory.** agy parses stdout as JSON. `PreToolUse` must return `{"decision": "allow"}` or the tool is denied (cmux issue #5358 is exactly that failure). `Stop` returns `{"decision": "stop"}`, everything else `{}`. Print the JSON before forking, then detach.
- **cwd.** Use `workspacePaths[0]` for the project name.

## Implementation slices

1. **Payload normalizer** in `session_routing.py` or a new `hosts.py`: one function that maps a raw hook event from any host to the Claude Code shape (`hook_event_name`, `session_id`, `cwd`, `prompt`, `tool_name`, `tool_input`, `transcript_path`), keyed on which fields are present. `announce.py` and `status.py` then stay host-agnostic. Add the transcript parser branch for Antigravity. Set `AGENT_PTT_AGENT` to `Copilot` or `Antigravity` in the hook command so the handle and voice pool key stay distinct.
2. **Stdout contract.** Emit the per-host JSON reply on stdout before `_detach()`, with `{}` as default.
3. **Installers.** Extend `scripts/install_plugins.py`: write `~/.copilot/hooks/agent-ptt.json`, and merge an `agent-ptt` group into `~/.gemini/config/hooks.json` with the same marker-based replace logic used for Codex. Add `--skip-copilot` and `--skip-antigravity`. `scripts/validate_plugins.py` should validate the rendered Antigravity file shape (flat vs grouped) since agy fails silently.
4. **Tests.** Fixture payloads for each host and event in `tests/test_announcer.py`, asserting the normalized event and the stdout reply. Antigravity transcript fixture for the Stop summary.
5. **Docs.** `plugins/README.md` and `docs/installation.md` gain Copilot and Antigravity sections. Note the two unverified surfaces: VS Code Copilot agent mode and the Copilot desktop app.

Effort: about a day for slices 1 to 4 for both hosts, half a day for a single one.

## Open questions to verify on a real session

- Whether Copilot CLI sends camelCase or snake_case for `preToolUse` in the installed version (`copilot` on this machine), capture one payload with a `cat > /tmp/payload.json` hook before writing the adapter.
- Whether the Antigravity IDE agent manager passes `workspacePaths` for multi-root workspaces in the order we expect.
- Whether Antigravity's `Stop` fires once per turn or once per agent loop exit (`fullyIdle` may be the turn boundary).

Related: the presence idea in the chat with Hazel and Rory benefits from Copilot's
`sessionStart` / `sessionEnd` and Antigravity's `Stop.fullyIdle`, which are cleaner
open and close signals than Claude Code offers today.
