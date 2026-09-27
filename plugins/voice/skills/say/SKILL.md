---
description: Speak a message aloud through the Agent PTT voice channel on the user's speakers. Use when the user asks to announce, say, speak, or narrate something out loud — e.g. "announce that the build is green", "tell the channel we're deploying", "say it out loud".
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
  code, no URLs. It will be synthesized to audio and played on speakers.
- Numbers and identifiers should be listenable ("forty-two tests passed",
  not "42/42 ✓").

The script uses this session's selected channel, or its Git repo/folder channel,
and reuses its assigned name and voice when a session ID is available and
prints `🔊 said: …` on success. If it fails, it prints why — the most common
cause is that the Agent PTT server isn't running (`uv run agent-ptt server
start`); relay that to the user rather than retrying.
