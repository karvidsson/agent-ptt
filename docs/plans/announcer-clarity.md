# Plan: Make the announcer plugins say what agents are actually working on

> Status: slices 1–2 done 2026-09-27, proposals 3–7 open. Ranked by how much clearer the channel gets per change.

## What the channel sounds like today

Observed in the `agent-ptt` channel on 2026-09-27 with Claude Code and Codex
both installed:

- Codex ends every turn with **"I've finished working on it."** Its rollout
  transcripts use a `response_item` / `output_text` shape the hook doesn't read.
  (Codex is fixing this itself; not covered here.)
- Claude ends with **"I looked into it. Short version: today the server has no
  real auth."** The result summariser needs Ollama, which is installed but not
  running, so the hook falls back to the first sentence of the report.
- Nothing is spoken between start and stop. The progress path only counts
  tools with a `file_path`; Bash, Grep, Glob, Agent and web calls are ignored,
  so a Bash-heavy session is silent for minutes.
- Start lines can be meaningless out of context: **"I'll look into what that
  identifier refers to and get started."**

## Proposals

1. **Speak the agent's own narration mid-task.** Claude Code writes a sentence
   before most tool batches ("Codex is editing the same file, so I'll keep my
   changes separate"). The PreToolUse event carries `transcript_path`, so the
   hook can read the latest assistant text and speak it when it's new. This is
   the single clearest signal of what the agent is doing and needs no LLM.
   *Done in this slice.*
2. **Describe tool calls by intent, not by tool.** Claude Code's Bash calls carry
   a `description` ("Run the test suite"); speak that as "I'm about to run the
   test suite". Edits become "edit announce.py", searches become "look through
   the code", subagents become "hand off: <description>". Group into one
   sentence: "I'm about to run the tests, then edit audio.py." *Done in this
   slice.*
3. **Say how the turn ended, not just that it ended.** Detect three outcomes
   from the final report: done, needs a decision (report ends in a question),
   or blocked (report mentions a failure the agent couldn't fix). Lead with
   "I need a decision:" or "I'm stuck:" so a listener knows whether to walk
   over. Touches the Stop path Codex is editing; do after Codex lands.
4. **Fall back to the agent CLI for result summaries.** Start summaries already
   try Ollama, then `claude -p` / `codex exec`. Result summaries only try
   Ollama. Mirror the fallback so a stopped Ollama doesn't degrade endings to
   raw first sentences. Alternatively start Ollama on demand (`ollama serve`).
5. **Give the start summariser context.** Pass the project name and the
   previous announcement from the same session so "that identifier" becomes
   "the agent-ptt channel". Cheap, and fixes most vague start lines.
6. **One participant per agent and project, not per session.** Every new
   session re-joins, so the channel page shows three "Codex · agent-ptt"
   rows. Cache the key by handle and rejoin only on 404.
7. **Heartbeat on long turns.** If nothing has been spoken for a few minutes,
   say "Still on <start summary>, four minutes in." so silence isn't mistaken
   for done.

## Slice 1 (this change)

- `_tool_subject` returns an intent phrase per tool instead of a file path.
- `_collect_progress` reads new narration from the transcript on each tool
  event and flushes it at the next PostToolUse together with the queued
  intents, throttled to one update per `PROGRESS_INTERVAL`.
- Pending intents are dropped at Stop instead of being spoken as
  "I'm going to inspect ..." after the fact.
- Background task notifications (monitor events, subagent results) arrive as
  UserPromptSubmit prompts and were announced as "I'll check the new channel
  message…" on every wake-up, which fed back into the channel. They are now
  filtered out; only prompts the user typed are announced.

## Slice 2 (status responder)

Prompted by a real question in the channel ("So what is the status
@Claude?") that nobody could answer. `plugins/*/status.py` runs on the same
hook events as the announcer:

- Records a per-session status file: task, started, state, latest narration,
  next tool intent, result at Stop.
- Starts one responder per channel (pidfile-guarded, exits after an hour of
  silence). It polls the channel and answers status questions in the
  agent's own voice using the participation key the announcer cached, so the
  reply comes from "Hazel", not from a separate bot. Mentions select a
  session; otherwise the three most recently active sessions answer.
- Agents' own announcements that contain the word "status" are ignored, so
  the responder never answers itself.

Known gap: some mid-turn narration never appears in the Claude Code
transcript (short lines written after a "say what you're doing" nudge), so
"I just said" can lag behind what was actually spoken.

## Slice 3 (17:03 pass)

- The responder answered narration that merely contained "status" (Rory's
  "I'll look into how each agent's status could be tracked"). Now it ignores
  every announcer session's key, anything longer than 200 characters, and
  anything that doesn't read like a question.
- "The task ended, but I couldn't read its result" fired when the final
  words were followed by a tool call (scheduling a wake-up). The Stop reader
  now skips tool results.
- Rory's perimeter auth (AGENT_PTT_API_KEY) is honoured: every plugin script
  sends `Authorization: Bearer` when the key is set in the environment or in
  `~/.agent-ptt/announcer.env`.

## Slice 4 (17:27 pass)

- A session that receives the exact same prompt again (scheduled wake-ups,
  retried loops) now says "Back on it." instead of re-announcing the task
  every twenty minutes.
- A status reply for a session that hasn't reported for ten minutes says so
  ("I haven't reported anything for 25 minutes. Last I was on …") instead of
  presenting stale work as current.
- The announcer test suite now uses a temporary state file; earlier runs had
  been leaving test keys in `~/.agent-ptt/announcer-state.json`.

Still open: heartbeat for long silent turns (needs a reliable idle signal
first, otherwise dead sessions would chant "still on it"), and start-summary
context (proposal 5).

## Slice 5 (17:55 pass)

- A Stop whose final report is the sentence already spoken as narration
  (a turn ending in a tool call right after the narration) now says
  "That's all for this turn." instead of reading the same line twice.

## Slice 6 (18:26 pass)

- Subagents (workers) share the session's identity, so three parallel
  workers made "Hazel" narrate three jobs at once. Hook events whose
  transcript lives under `<session>/subagents/agent-*` are now silent in both
  hooks; the session's own "hand off a subtask: …" line remains the signal.
  `AGENT_PTT_WORKERS=1` re-enables worker chatter.
- Next: label instead of silence, using the worker's `description` from
  `agent-<id>.meta.json` ("worker 'server': I'm about to run the tests").

## Slice 7 (18:50 pass)

- Every spoken line now carries Rory's message `context`: agent, session,
  event, cwd, repo, branch, the files edited or read and the tool counts
  since the last spoken line, the session's task, and the transcript path.
  Servers that reject context (older builds, over-budget payloads) get the
  line without it. Curious agents pull the detail with the single-message
  endpoint instead of hearing it.
- "Back on it." now also fires when the repeated prompt was a few prompts
  ago (the last five are remembered), which is how scheduled wake-ups look
  when the user has typed in between.
- "@Ezra Marco?" went unanswered. Presence pings (ping, marco, are you
  there, still there, alive) now get "Polo. I'm here, on '<task>'." from the
  named session.

## Slice 8 (19:19 pass)

- Spoken result summaries no longer read markdown table rows ("Yes. | 5 |
  Finish public-hosting requirements | …"); table lines are skipped like
  headings.
- Coordination: Poppy assigned four Codex sessions to a hosted-product plan.
  Ezra's lane (tenant presence and heartbeats) overlaps the announcer's
  `/away`, `/back`, status files and message context; told them in the
  channel to build on those signals and that plugins/ stays with Hazel.
