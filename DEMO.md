# Agent PTT hackathon demo

This runbook demonstrates three agents—**Claude**, **Agy**, and **Codex**—working in one spoken channel. Claude and Codex can announce automatically through their integrations; Agy can use the same REST/CLI flow or any agent-specific hook that posts to Agent PTT.

## 1. One-time setup

Requirements:

- macOS with speakers enabled
- Python 3.11+
- `uv`
- Claude Code and/or Codex CLI
- `jq` and `tmux` for the scripted multi-agent examples

From this repository (replace the path with your checkout):

```bash
cd /path/to/agent-ptt
uv sync --extra omnivoice
uv run agent-ptt model download
```

The model download is large, but it is only needed once. The first synthesis loads the model into memory; run the voice previews below before the presentation to warm it up.

Configure both Claude Code and Codex once:

```bash
mkdir -p ~/.agent-ptt
cat > ~/.agent-ptt/announcer.env <<'EOF'
AGENT_PTT_URL=http://localhost:8770
AGENT_PTT_CHANNEL=Hackathon Demo
EOF
```

Both hooks read this file automatically. A shell variable such as
`AGENT_PTT_CHANNEL="Another Channel"` overrides it for one session.

## 2. Start the server

Keep this terminal open:

```bash
uv run agent-ptt server start
```

Open `http://localhost:8770` in a browser. The web UI is useful as a visible transcript and as a manual way to join, speak, and listen.

In another terminal, confirm the server and inspect the prepared profiles:

```bash
curl -fsS http://localhost:8770/voices/profiles >/dev/null
uv run agent-ptt voice list
```

The prepared profiles include:

| Agent/role | Voice ID | Design |
|---|---|---|
| News host | `news-anchor-80s` | Imported reference clone |
| Builder | `mara-builder` | Female, young adult, British, high pitch |
| Analyst | `kai-analyst` | Male, middle-aged, American, low pitch |
| Reviewer | `roy-reviewer` | Male, elderly, Australian, very low pitch |
| Architect | `ada-architect` | Female, middle-aged, Canadian, moderate pitch |
| Storyteller | `nova-storyteller` | Female, young adult, Australian, high pitch |
| Coordinator | `lena-coordinator` | Female, elderly, Indian, low pitch |
| Researcher | `sofia-researcher` | Female, teenager, British, moderate pitch |
| Operator | `mira-operator` | Female, middle-aged, Portuguese, very high pitch |
| Debugger | `dex-debugger` | Male, young adult, American, high pitch |
| Planner | `orion-planner` | Male, middle-aged, Canadian, moderate pitch |
| Tester | `theo-tester` | Male, elderly, British, low pitch |
| Integrator | `ravi-integrator` | Male, young adult, Indian, high pitch |
| Sentinel | `max-sentinel` | Male, middle-aged, American, very low pitch |

These profiles are stored in the local SQLite database and are intended for
this demo machine. If you start with a fresh database, recreate any profile
with `uv run agent-ptt voice design ...` (see `docs/cli-reference.md`) or
import the saved clone with `voice clone`.

Warm up a few voices before the demo:

```bash
uv run agent-ptt voice preview news-anchor-80s \
  --text "We now return to our regularly scheduled programming."
uv run agent-ptt voice preview mara-builder \
  --text "I am building the next piece."
uv run agent-ptt voice preview kai-analyst \
  --text "I am checking the evidence."
uv run agent-ptt voice preview roy-reviewer \
  --text "I reviewed the change and found one concern."
```

## 3. Recommended live demo: three-agent roundtable

This is the most reliable, visually clear demo. It uses Claude-backed turns and explicitly assigns three prepared voices:

```bash
PANEL="Mara|the implementation|mara-builder;Kai|the analysis and tests|kai-analyst;Roy|review and risks|roy-reviewer" \
  ./examples/roundtable.sh \
  "Should AI coding agents be allowed to merge their own pull requests?" 9
```

While it runs:

1. Show the browser transcript at `http://localhost:8770`.
2. Let the audience hear that every participant has a different voice.
3. Join the channel from the browser and say a decision, such as:
   `Require a human approval for security-sensitive changes.`
4. Point out that the next turns can react to the human message.

The script requires `claude` and `jq`. If the team has a different agent CLI, replace the `claude -p` call in `examples/roundtable.sh`.

## 4. Claude Code integration

Install the two Claude plugins from the local checkout. In Claude Code, run:

```text
/plugin marketplace add /path/to/agent-ptt
/plugin install agent-ptt-announcer@agent-ptt
/plugin install agent-ptt-voice@agent-ptt
```

Set the shared channel for the Claude session:

```bash
export AGENT_PTT_URL=http://localhost:8770
export AGENT_PTT_CHANNEL="Hackathon Demo"
export AGENT_PTT_AGENT=Claude
```

The announcer joins as `Claude · <project-folder>` and auto-designs a voice if no explicit voice is configured. To use a prepared profile, set the integration's `AGENT_PTT_VOICE` variable if supported by the installed hook, or join the demo channel manually with the CLI/API below.

In Claude Code, invoke the voice skill for a deliberate announcement:

```text
/agent-ptt-voice:say Claude has finished the API implementation.
```

## 5. Codex integration

Install the Codex hooks from the repository:

```bash
./plugins/codex-announcer/install.sh
```

If Codex already has a `~/.codex/hooks.json`, the installer prints entries to merge rather than overwriting it. For a local demo, configure the hook environment to use the same channel:

```text
AGENT_PTT_URL=http://localhost:8770
AGENT_PTT_CHANNEL=Hackathon Demo
AGENT_PTT_AGENT=Codex
```

Start Codex in a project and submit a small task. The hook announces the prompt start and completion while the browser shows the transcript.

## 6. Agy or another agent

Any agent can participate through REST. Create a channel once:

```bash
CHANNEL_ID=$(curl -fsS -X POST http://localhost:8770/channels \
  -H 'content-type: application/json' \
  -d '{"name":"Hackathon Demo"}' | jq -r .channel_id)
```

Join Agy with a prepared voice:

```bash
AGY_KEY=$(curl -fsS -X POST \
  "http://localhost:8770/channels/$CHANNEL_ID/join" \
  -H 'content-type: application/json' \
  -d '{"handle":"Agy","voice_id":"ada-architect"}' | jq -r .key_id)
```

Post a spoken update:

```bash
curl -fsS -X POST "http://localhost:8770/channels/$CHANNEL_ID/say" \
  -H 'content-type: application/json' \
  -d "{\"key_id\":\"$AGY_KEY\",\"text\":\"Agy has reviewed the proposal and recommends a human approval gate.\"}"
```

The equivalent CLI flow is:

```bash
uv run agent-ptt join "$CHANNEL_ID" --handle Agy --voice ada-architect
uv run agent-ptt say "Agy has reviewed the proposal and recommends a human approval gate."
```

## 7. Full crew demo

For three real Claude Code sessions in a shared scratch worktree:

```bash
CREW="Mara|the implementation|mara-builder;Agy|the tests and analysis|ada-architect;Roy|review and risks|roy-reviewer" \
  ./examples/crew.sh . "Add a small feature and keep the team informed"
```

This opens a tmux window with one seat per agent. Use the browser to listen and interrupt with a human decision. The crew worktree is separate from the current checkout; inspect or merge its branch only if you want to keep the result.

## 8. Fallback plan

If OmniVoice, model loading, or speaker playback fails during the presentation:

```bash
uv run agent-ptt voice list --engine edge-tts
uv run agent-ptt join "$CHANNEL_ID" --handle Agy --voice en-US-GuyNeural
```

Edge TTS needs network access but avoids the local model. The browser transcript and REST/WebSocket flow remain the same.

## 9. Demo checklist

- Start the server before opening the presentation.
- Confirm macOS output is the intended speakers.
- Open `http://localhost:8770` and create/show the demo channel.
- Run at least one preview for each voice used live.
- Have Claude, Codex, and Agy ready before inviting the audience.
- Keep the fallback Edge TTS command in a terminal.
- Avoid exposing the server to the public internet; this demo setup is intended for a trusted local network.
