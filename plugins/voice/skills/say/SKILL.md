---
description: Speak a message aloud in the Agent PTT voice channel — synthesized to speech and streamed to the channel's listeners (web UI, `agent-ptt listen`). Use when the user asks to announce, say, speak, or narrate something out loud — e.g. "announce that the build is green", "tell the channel we're deploying", "say it out loud".
argument-hint: [message]
allowed-tools: Bash(python3 *say.py*)
---

Speak the message in the Agent PTT voice channel by running:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/say.py" "$ARGUMENTS"
```

If `$ARGUMENTS` is empty, compose a one-sentence spoken update yourself from
the current conversation context (what you just did or found), then run the
script with it.

Guidelines for the message:
- Keep it short and speech-friendly — one or two sentences, no markdown, no
  code, no URLs. It is synthesized to speech and streamed to the channel's
  listeners (the web UI or `agent-ptt listen`); the server host's own
  speakers play it only if the server was started with `--speakers`.
- Numbers and identifiers should be listenable ("forty-two tests passed",
  not "42/42 ✓").

The script uses this session's selected channel, or its Git repo/folder channel,
and reuses its assigned name and voice when a session ID is available and
prints `🔊 said: …` on success. If it fails, it prints why — the most common
cause is that the Agent PTT server isn't running (`uv run agent-ptt server
start`); relay that to the user rather than retrying.
