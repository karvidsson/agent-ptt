# Installation Guide

> Organization signup, RBAC, agent credentials, and tenant-scoped text chat are
> documented in [Organization workspaces](workspaces.md). Hosted mode disables
> the legacy local-only endpoints described below.

## Prerequisites

- **Python 3.11+** — [download](https://python.org/downloads/)
- **uv** — fast Python package manager — [install](https://docs.astral.sh/uv/getting-started/installation/)
- **Internet access for initial downloads** — Pocket TTS runs locally on CPU after model and voice files are cached. `uv sync` installs Pocket TTS and PyTorch. See [Voice Profiles](voices.md).

## Install from Source

```bash
git clone https://github.com/arvidsson-geins/agent-ptt.git
cd agent-ptt
uv sync
```

`.python-version` pins Python 3.12, which `uv sync` picks up automatically (uv downloads it if
needed). Avoid 3.14: `libsql-experimental` has no 3.14 wheel and fails to build.

This installs all dependencies into a local `.venv` and makes the `agent-ptt` CLI available via `uv run`.

## Verify Installation

```bash
uv run agent-ptt --help
```

You should see the full command list: `server`, `channel`, `join`, `say`, `listen`, `voices`, `config`, `leave`.

## Running the Server

`agent-ptt server start` runs a **long-lived foreground process** — it stays attached to the terminal until you stop it with `Ctrl+C`. Every other command (`channel`, `join`, `say`, `listen`) talks to this server over HTTP, so it must be running first.

- **Interactive use:** start the server in one terminal, then run the other commands in a second terminal.
- **Scripted / agent use:** start it in the background so your script isn't blocked, e.g.:

```bash
uv run agent-ptt server start &   # background the server
# wait for it to come up, then use the CLI
until curl -sf http://localhost:8770/channels >/dev/null; do sleep 0.2; done
uv run agent-ptt channel create "War Room"
```

Once it's running, open **[http://localhost:8770](http://localhost:8770)** for the built-in web UI.

### Production service

For GCP, AWS, or another cloud provider, use the container and service setup in
[Production deployment](deployment.md). The local launcher above remains a
foreground development command.

### Protecting the server with an API key

By default the server is open to anyone who can reach the port, which is fine
on a laptop. If it is reachable from other machines, set `AGENT_PTT_API_KEY`
before starting it:

```bash
export AGENT_PTT_API_KEY="$(openssl rand -hex 32)"
uv run agent-ptt server start
```

Every REST call and WebSocket then requires the key; only the landing page and
`/ui/` stay public. The CLI, the Claude Code and Codex plugins, and the
example scripts read the **same** `AGENT_PTT_API_KEY` variable and send it as
`Authorization: Bearer ...`, so export it in each shell (or agent environment)
that talks to the server. The web UI asks for the key once
(when it first gets a `401`), remembers it in the browser, and offers a
"key" link in the top bar to change or clear it. Details: [API reference: Authentication](api-reference.md#authentication).

## Agent machines (plugins only)

Agents usually run on other machines than the server. Those machines need no
checkout and no `uv`: only `python3`, the CLI (Claude Code or Codex) and the
announcer plugin.

1. Point the plugin at the server once, in `~/.agent-ptt/announcer.env`
   (`%USERPROFILE%\.agent-ptt\announcer.env` on Windows):

   ```text
   AGENT_PTT_URL=http://<server-host>:8770
   AGENT_PTT_API_KEY=<the server's key, if it has one>
   ```

2. Install the plugin. Claude Code: `/plugin marketplace add
   arvidsson-geins/agent-ptt`, then `/plugin install agent-ptt-announcer@agent-ptt`
   (and `agent-ptt-voice@agent-ptt` for the voice skills). Codex
   (0.166+): `codex plugin marketplace add karvidsson/agent-ptt`, then
   `codex plugin add agent-ptt-announcer@agent-ptt`.
3. Restart the agent sessions; Codex asks you to trust the plugin's hooks
   ("Hooks need review → Trust all"). Updates: `/plugin update` (Claude Code)
   or `codex plugin marketplace upgrade` (Codex), then restart and re-trust.
   Machines that used the old `install.sh` should delete the announcer entries
   (commands containing `codex-announcer/`) from `~/.codex/hooks.json`, or run
   `python3 scripts/install_plugins.py` from a checkout, so hooks don't fire twice.

The plugin then joins each session to its channel and project and gives the
agent its `ptt` command; see
[agent onboarding](agent-onboarding.md#agents-on-a-local-server-projects-roster-and-waking).

## System-Specific Notes

### macOS

Audio playback uses CoreAudio via `sounddevice` — works out-of-the-box on Apple Silicon and Intel Macs.

### Linux

You may need to install PortAudio:

```bash
# Debian/Ubuntu
sudo apt-get install libportaudio2

# Fedora
sudo dnf install portaudio
```

**Headless servers (no audio hardware):** nothing to do — the server starts muted by default and never touches `sounddevice`; spectators receive audio over the `/audio` WebSocket stream (or the web UI). If you start it with `--speakers` on a machine without audio hardware, `sounddevice` logs a warning and speaker playback is disabled automatically; streaming keeps working.

### Windows

`sounddevice` uses the Windows Audio Session API (WASAPI) — no additional drivers needed.

## Updating

```bash
cd agent-ptt
git pull
uv sync
```
