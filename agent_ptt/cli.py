"""CLI client — Typer-based command interface for Agent PTT."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
from rich import print as rprint
from rich.console import Console
from rich.markup import escape
from rich.table import Table

app = typer.Typer(
    name="agent-ptt",
    help="🎙️ Voice channels for AI agents — push-to-talk with human spectators",
    add_completion=False,
)
console = Console()

# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

SESSION_DIR = Path.home() / ".agent-ptt"
SESSION_FILE = SESSION_DIR / "session.json"
DEFAULT_BASE_URL = "http://localhost:8770"


def _save_session(data: dict) -> None:
    """Save session data to disk."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2, default=str))


def _load_session() -> dict:
    """Load session data from disk."""
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())
    return {}


def _get_base_url() -> str:
    """Get the server base URL from session or default."""
    session = _load_session()
    return session.get("base_url", DEFAULT_BASE_URL)


# ---------------------------------------------------------------------------
# Server commands
# ---------------------------------------------------------------------------

server_app = typer.Typer(help="Server management")
app.add_typer(server_app, name="server")


@server_app.command("start")
def server_start(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int = typer.Option(8770, help="Bind port"),
    mute: bool = typer.Option(
        False,
        "--mute",
        help="Disable local speaker playback while keeping streamed audio available",
    ),
):
    """Start the Agent PTT server."""
    import logging
    import os

    import uvicorn

    if mute:
        os.environ["AGENT_PTT_MUTE"] = "1"
    # Make app loggers (TTS pipeline, audio mixer) visible alongside uvicorn's
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")

    rprint(f"[bold green]🎙️  Starting Agent PTT server on {host}:{port}[/bold green]")
    uvicorn.run(
        "agent_ptt.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# Channel commands
# ---------------------------------------------------------------------------

channel_app = typer.Typer(help="Channel management")
app.add_typer(channel_app, name="channel")


@channel_app.command("create")
def channel_create(name: str = typer.Argument(help="Channel name")):
    """Create a new voice channel."""
    base = _get_base_url()
    resp = httpx.post(f"{base}/channels", json={"name": name})
    if resp.status_code == 200:
        data = resp.json()
        rprint(f"[bold green]✅ Channel created:[/bold green] {data['name']}")
        rprint(f"   ID: [cyan]{data['channel_id']}[/cyan]")
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@channel_app.command("list")
def channel_list():
    """List all active channels."""
    base = _get_base_url()
    resp = httpx.get(f"{base}/channels")
    if resp.status_code == 200:
        channels = resp.json()
        if not channels:
            rprint("[dim]No active channels[/dim]")
            return

        table = Table(title="Active Channels")
        table.add_column("Name", style="cyan")
        table.add_column("ID", style="dim")
        table.add_column("Participants", justify="right")

        for c in channels:
            table.add_row(
                c["name"],
                c["channel_id"],
                str(len(c.get("participants", {}))),
            )
        console.print(table)
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@channel_app.command("history")
def channel_history(channel_id: str = typer.Argument(help="Channel ID")):
    """View what has been said in a channel."""
    base = _get_base_url()
    resp = httpx.get(f"{base}/channels/{channel_id}/history")
    if resp.status_code == 200:
        messages = resp.json()
        if not messages:
            rprint("[dim]No messages yet[/dim]")
            return

        for msg in messages:
            ts = msg.get("timestamp", "")
            # Trim to just time if it's a full ISO timestamp
            if "T" in ts:
                ts = ts.split("T")[1][:8]
            handle = msg.get("handle", "?")
            text = msg.get("text", "")
            rprint(f"[dim]{ts}[/dim] [bold cyan]{handle}[/bold cyan]: {text}")
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


# ---------------------------------------------------------------------------
# Join / Leave / Say
# ---------------------------------------------------------------------------


@app.command("join")
def join(
    channel_id: str = typer.Argument(help="Channel ID to join"),
    handle: str = typer.Option(..., "--handle", "-h", help="Your display name"),
    voice: str = typer.Option(
        None, "--voice", "-v", help="Voice ID for TTS (omit for an auto-designed voice)"
    ),
):
    """Join a channel with a handle and voice."""
    base = _get_base_url()
    # Generous timeout: joining without a voice may load the designer LLM
    resp = httpx.post(
        f"{base}/channels/{channel_id}/join",
        json={"handle": handle, "voice_id": voice},
        timeout=120.0,
    )
    if resp.status_code == 200:
        data = resp.json()
        # Save session
        session = _load_session()
        session["key_id"] = data["key_id"]
        session["channel_id"] = channel_id
        session["handle"] = handle
        session["voice"] = data.get("voice_id") or voice
        _save_session(session)

        rprint(f"[bold green]✅ Joined as [{handle}][/bold green]")
        if designed := data.get("designed_voice"):
            rprint(
                f"   Voice: [magenta]auto-designed[/magenta] "
                f"{escape(json.dumps(designed.get('settings', {})))}"
            )
        rprint(f"   Key: [cyan]{data['key_id']}[/cyan]")
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@app.command("leave")
def leave():
    """Leave the current channel."""
    session = _load_session()
    key_id = session.get("key_id")
    channel_id = session.get("channel_id")

    if not key_id or not channel_id:
        rprint("[yellow]Not in any channel[/yellow]")
        return

    base = _get_base_url()
    resp = httpx.post(
        f"{base}/channels/{channel_id}/leave",
        params={"key_id": key_id},
    )
    if resp.status_code == 200:
        session.pop("key_id", None)
        session.pop("channel_id", None)
        _save_session(session)
        rprint("[bold green]✅ Left channel[/bold green]")
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@app.command("say")
def say(text: str = typer.Argument(help="Message to send")):
    """Send a message to the current channel."""
    session = _load_session()
    key_id = session.get("key_id")
    channel_id = session.get("channel_id")

    if not key_id or not channel_id:
        rprint("[yellow]Not in any channel. Use 'agent-ptt join' first.[/yellow]")
        return

    base = _get_base_url()

    # Send via REST (for simplicity — WebSocket mode is for live agent connections)
    # We'll post a message via the WebSocket protocol using httpx
    import asyncio

    import websockets

    async def _send():
        uri = f"ws://{base.replace('http://', '')}/channels/{channel_id}/ws?key={key_id}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "message", "text": text}))
            # Wait briefly for the broadcast echo
            try:
                resp = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(resp)
                if data.get("type") == "message":
                    rprint(
                        f"[dim]{data.get('timestamp', '')}[/dim] "
                        f"[bold cyan]{data.get('handle', '?')}[/bold cyan]: "
                        f"{data.get('text', '')}"
                    )
            except TimeoutError:
                rprint(f"[bold green]✅ Sent:[/bold green] {text}")

    asyncio.run(_send())


# ---------------------------------------------------------------------------
# Listen (spectator mode)
# ---------------------------------------------------------------------------


@app.command("listen")
def listen(
    channel_id: str = typer.Argument(help="Channel ID to listen to"),
):
    """Listen to a channel's audio stream (spectator mode)."""
    import asyncio

    base = _get_base_url()

    async def _listen():
        import websockets

        uri = f"ws://{base.replace('http://', '')}/channels/{channel_id}/audio"
        rprint("[bold green]🎧 Listening to channel...[/bold green] (Ctrl+C to stop)")

        try:
            async with websockets.connect(uri) as ws:
                try:
                    import io
                    import wave

                    import numpy as np
                    import sounddevice as sd

                    while True:
                        audio_bytes = await ws.recv()
                        # Try to play the audio
                        try:
                            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                                frames = wf.readframes(wf.getnframes())
                                sr = wf.getframerate()
                                audio = np.frombuffer(frames, dtype=np.int16)
                                audio_float = audio.astype(np.float32) / 32768.0
                                sd.play(audio_float, samplerate=sr, blocking=True)
                        except Exception:
                            rprint(f"[dim]Received {len(audio_bytes)} bytes of audio[/dim]")
                except ImportError:
                    rprint("[yellow]sounddevice not available — showing raw data[/yellow]")
                    while True:
                        audio_bytes = await ws.recv()
                        rprint(f"[dim]🔊 Audio chunk: {len(audio_bytes)} bytes[/dim]")
        except Exception as e:
            rprint(f"[bold red]❌ Connection error:[/bold red] {e}")

    try:
        asyncio.run(_listen())
    except KeyboardInterrupt:
        rprint("\n[dim]Stopped listening[/dim]")


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


@app.command("voices")
def voices(
    engine: str = typer.Option("edge-tts", help="TTS engine to list voices for"),
):
    """List available TTS voices."""
    base = _get_base_url()
    resp = httpx.get(f"{base}/voices", params={"engine": engine})
    if resp.status_code == 200:
        voice_list = resp.json()
        if not voice_list:
            rprint("[dim]No voices available[/dim]")
            return

        table = Table(title=f"Available Voices ({engine})")
        table.add_column("Voice ID", style="cyan")
        table.add_column("Name")
        table.add_column("Details", style="dim")

        for v in voice_list[:50]:  # Limit display
            details = ""
            settings = v.get("settings", {})
            if "locale" in settings:
                details += settings["locale"]
            if "gender" in settings:
                details += f" · {settings['gender']}"
            table.add_row(v["voice_id"], v["display_name"], details)

        console.print(table)
        if len(voice_list) > 50:
            rprint(f"[dim]... and {len(voice_list) - 50} more[/dim]")
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


# ---------------------------------------------------------------------------
# Voice profiles
# ---------------------------------------------------------------------------

voice_app = typer.Typer(help="Voice profile management")
app.add_typer(voice_app, name="voice")


@voice_app.command("list")
def voice_list(
    engine: str = typer.Option(None, "--engine", "-e", help="Filter by TTS engine"),
):
    """List stored voice profiles."""
    base = _get_base_url()
    params = {"engine": engine} if engine else {}
    resp = httpx.get(f"{base}/voices/profiles", params=params)
    if resp.status_code == 200:
        profiles = resp.json()
        if not profiles:
            rprint("[dim]No stored voice profiles[/dim]")
            return

        table = Table(title="Stored Voice Profiles")
        table.add_column("Voice ID", style="cyan")
        table.add_column("Name")
        table.add_column("Engine")
        table.add_column("Settings", style="dim")

        for p in profiles:
            table.add_row(
                p["voice_id"],
                p["display_name"],
                p["engine"],
                escape(json.dumps(p.get("settings", {}))),
            )
        console.print(table)
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@voice_app.command("show")
def voice_show(voice_id: str = typer.Argument(help="Voice profile ID")):
    """Show a stored voice profile."""
    base = _get_base_url()
    resp = httpx.get(f"{base}/voices/profiles/{voice_id}")
    if resp.status_code == 200:
        rprint(escape(json.dumps(resp.json(), indent=2, default=str)))
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@voice_app.command("save")
def voice_save(
    voice_id: str = typer.Option(..., "--id", help="Voice profile ID"),
    name: str = typer.Option(..., "--name", "-n", help="Display name"),
    engine: str = typer.Option("edge-tts", "--engine", "-e", help="TTS engine"),
    settings: str = typer.Option("{}", "--settings", "-s", help="Engine settings as JSON"),
):
    """Create or update a voice profile."""
    try:
        settings_dict = json.loads(settings)
    except json.JSONDecodeError as e:
        rprint(f"[bold red]❌ Invalid --settings JSON:[/bold red] {e}")
        raise typer.Exit(1) from e

    base = _get_base_url()
    resp = httpx.post(
        f"{base}/voices/profiles",
        json={
            "voice_id": voice_id,
            "display_name": name,
            "engine": engine,
            "settings": settings_dict,
        },
    )
    if resp.status_code == 200:
        rprint(f"[bold green]✅ Voice profile saved:[/bold green] {voice_id}")
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@voice_app.command("delete")
def voice_delete(voice_id: str = typer.Argument(help="Voice profile ID")):
    """Delete a stored voice profile."""
    base = _get_base_url()
    resp = httpx.delete(f"{base}/voices/profiles/{voice_id}")
    if resp.status_code == 200:
        rprint(f"[bold green]✅ Voice profile deleted:[/bold green] {voice_id}")
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


def _normalize_instruct_item(value: str, valid: list[str], suffix: str = "") -> str:
    """Validate one instruct item against the model vocabulary.

    Accepts shorthand ("british" for "british accent") via the suffix.
    """
    item = value.strip().lower()
    if suffix and not item.endswith(suffix):
        item = f"{item} {suffix}"
    if item not in valid:
        rprint(f"[bold red]❌ Invalid value:[/bold red] {escape(value)}")
        rprint(f"   Valid options: {', '.join(v.removesuffix(f' {suffix}') for v in valid)}")
        raise typer.Exit(1)
    return item


@voice_app.command("design")
def voice_design(
    name: str = typer.Option(..., "--name", "-n", help="Display name for the voice"),
    voice_id: str = typer.Option(None, "--id", help="Profile ID (default: slug of the name)"),
    gender: str = typer.Option(None, "--gender", "-g", help="male | female"),
    age: str = typer.Option(
        None, "--age", "-a", help="teenager | young adult | middle-aged | elderly"
    ),
    accent: str = typer.Option(None, "--accent", help="e.g. british, american, australian"),
    pitch: str = typer.Option(
        None, "--pitch", "-p", help="very low | low | moderate | high | very high"
    ),
    whisper: bool = typer.Option(False, "--whisper", help="Whispering voice"),
):
    """Design an OmniVoice voice from attributes and save it as a profile."""
    from agent_ptt.voicedesign import ACCENTS, AGES, GENDERS, PITCHES

    items = []
    if gender:
        items.append(_normalize_instruct_item(gender, GENDERS))
    if age:
        items.append(_normalize_instruct_item(age, AGES))
    if accent:
        items.append(_normalize_instruct_item(accent, ACCENTS, suffix="accent"))
    if pitch:
        items.append(_normalize_instruct_item(pitch, PITCHES, suffix="pitch"))
    if whisper:
        items.append("whisper")

    if not items:
        rprint(
            "[bold red]❌ Nothing to design.[/bold red] "
            "Provide at least one of --gender/--age/--accent/--pitch/--whisper"
        )
        raise typer.Exit(1)

    instruct = ", ".join(items)
    profile_id = voice_id or name.strip().lower().replace(" ", "-")

    base = _get_base_url()
    resp = httpx.post(
        f"{base}/voices/profiles",
        json={
            "voice_id": profile_id,
            "display_name": name,
            "engine": "omnivoice",
            "settings": {"instruct": instruct},
        },
    )
    if resp.status_code == 200:
        rprint(f"[bold green]🎨 Voice designed:[/bold green] {profile_id}")
        rprint(f"   Instruct: [cyan]{escape(instruct)}[/cyan]")
        rprint(f"   Preview:  [dim]agent-ptt voice preview {profile_id}[/dim]")
        rprint(
            f"   Use it:   [dim]agent-ptt join <channel-id> --handle You --voice {profile_id}[/dim]"
        )
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


@voice_app.command("pinned")
def voice_pinned():
    """List handles with auto-designed pinned voices."""
    base = _get_base_url()
    resp = httpx.get(f"{base}/voices/pinned")
    if resp.status_code != 200:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")
        return

    pins = resp.json()
    if not pins:
        rprint("[dim]No pinned voices — join a channel without --voice to get one[/dim]")
        return

    table = Table(title="Pinned Voices")
    table.add_column("Handle", style="cyan")
    table.add_column("Voice ID", style="dim")
    table.add_column("Source")
    table.add_column("Settings", style="dim")

    for pin in pins:
        table.add_row(
            pin["handle"],
            pin["voice_id"],
            pin["source"],
            escape(json.dumps(pin.get("settings", {}))),
        )
    console.print(table)


@voice_app.command("redesign")
def voice_redesign(handle: str = typer.Argument(help="Handle to redesign the voice for")):
    """Design a fresh voice for a handle, replacing the pinned one."""
    base = _get_base_url()

    old_resp = httpx.get(f"{base}/voices/pinned")
    old_pins = old_resp.json() if old_resp.status_code == 200 else []
    old_settings = next(
        (p.get("settings") for p in old_pins if p["handle"] == handle.lower()), None
    )

    resp = httpx.post(f"{base}/voices/pinned/{handle}/redesign")
    if resp.status_code != 200:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")
        raise typer.Exit(1)

    profile = resp.json()
    rprint(f"[bold green]🎨 Redesigned voice for [{handle}][/bold green]")
    if old_settings is not None:
        rprint(f"   Old: [dim]{escape(json.dumps(old_settings))}[/dim]")
    rprint(f"   New: [cyan]{escape(json.dumps(profile.get('settings', {})))}[/cyan]")
    rprint(f"   Preview: [dim]agent-ptt voice preview {profile['voice_id']}[/dim]")


@voice_app.command("clone")
def voice_clone(
    reference: Path = typer.Option(
        ..., "--reference", "-r", help="Reference audio clip (5-30s WAV)"
    ),
    transcript: str = typer.Option(
        ...,
        "--transcript",
        "-t",
        help="Exact transcript of the reference clip (required — avoids the 1.6 GB ASR model)",
    ),
    name: str = typer.Option(..., "--name", "-n", help="Display name for the cloned voice"),
    voice_id: str = typer.Option(None, "--id", help="Profile ID (default: slug of the name)"),
):
    """Clone a voice from a reference audio clip and save it as a profile."""
    ref_path = reference.expanduser().resolve()
    if not ref_path.is_file():
        rprint(f"[bold red]❌ Reference file not found:[/bold red] {ref_path}")
        raise typer.Exit(1)
    if not transcript.strip():
        rprint("[bold red]❌ Transcript must not be empty[/bold red]")
        raise typer.Exit(1)

    profile_id = voice_id or name.strip().lower().replace(" ", "-")

    base = _get_base_url()
    resp = httpx.post(
        f"{base}/voices/profiles",
        json={
            "voice_id": profile_id,
            "display_name": name,
            "engine": "omnivoice",
            "settings": {"ref_audio": str(ref_path), "ref_text": transcript},
        },
    )
    if resp.status_code == 200:
        rprint(f"[bold green]🧬 Voice cloned:[/bold green] {profile_id}")
        rprint(f"   Reference: [dim]{ref_path}[/dim]")
        rprint(f"   Preview:   [dim]agent-ptt voice preview {profile_id}[/dim]")
        rprint(
            "   [yellow]Note:[/yellow] the reference file is read at synthesis time — "
            "keep it at this path."
        )
    else:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")


def _play_audio_bytes(audio_bytes: bytes) -> None:
    """Play WAV (or MP3-fallback) audio bytes through the local speakers."""
    import io
    import wave

    import numpy as np
    import sounddevice as sd

    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            sr = wf.getframerate()
            audio = np.frombuffer(frames, dtype=np.int16)
            sd.play(audio.astype(np.float32) / 32768.0, samplerate=sr, blocking=True)
    except wave.Error:
        import soundfile as sf

        data, sr = sf.read(io.BytesIO(audio_bytes))
        sd.play(data, samplerate=sr, blocking=True)


@voice_app.command("preview")
def voice_preview(
    voice_id: str = typer.Argument(help="Stored voice profile ID"),
    text: str = typer.Option(
        "Hello! This is a preview of my voice.", "--text", "-t", help="Text to speak"
    ),
):
    """Synthesize a test clip with a stored profile and play it locally."""
    import asyncio

    from agent_ptt.models import VoiceProfile
    from agent_ptt.tts import get_backend

    base = _get_base_url()
    resp = httpx.get(f"{base}/voices/profiles/{voice_id}")
    if resp.status_code != 200:
        rprint(f"[bold red]❌ Error:[/bold red] {resp.text}")
        raise typer.Exit(1)

    profile = VoiceProfile(**resp.json())
    try:
        backend = get_backend(profile.engine)
    except ValueError:
        rprint(
            f"[bold red]❌ Engine '{profile.engine}' not available.[/bold red] "
            "Install it first: [cyan]uv sync --extra omnivoice[/cyan]"
        )
        raise typer.Exit(1) from None

    rprint(f"[bold green]🎧 Synthesizing preview[/bold green] ({profile.engine})...")
    audio_bytes = asyncio.run(backend.synthesize(text, profile))
    _play_audio_bytes(audio_bytes)
    rprint("[bold green]✅ Done[/bold green]")


# ---------------------------------------------------------------------------
# Model management (local neural TTS)
# ---------------------------------------------------------------------------

model_app = typer.Typer(help="Local TTS model management (omnivoice extra)")
app.add_typer(model_app, name="model")

_EXTRA_HINT = "Install the omnivoice extra first: [cyan]uv sync --extra omnivoice[/cyan]"


@model_app.command("status")
def model_status():
    """Show whether the OmniVoice engine and model checkpoint are ready."""
    from agent_ptt.modelcache import (
        DEFAULT_CHECKPOINT,
        format_size,
        get_cached_model,
        hub_available,
    )
    from agent_ptt.tts import has_backend

    engine_ready = has_backend("omnivoice")
    engine_state = "[green]installed[/green]" if engine_ready else "[red]not installed[/red]"
    rprint(f"Engine:     omnivoice {engine_state}")

    if not hub_available():
        rprint(f"Model:      [red]unknown[/red] — {_EXTRA_HINT}")
        raise typer.Exit(1)

    cached = get_cached_model(DEFAULT_CHECKPOINT)
    if cached:
        rprint(f"Model:      [green]cached[/green] {DEFAULT_CHECKPOINT}")
        rprint(f"Size:       {format_size(cached.size_bytes)} ({cached.nb_files} files)")
        rprint(f"Path:       [dim]{cached.path}[/dim]")
    else:
        rprint(f"Model:      [yellow]not downloaded[/yellow] {DEFAULT_CHECKPOINT}")
        rprint("Run [cyan]agent-ptt model download[/cyan] to fetch it (~2.4 GB),")
        rprint("or it will download automatically on first synthesis.")


@model_app.command("download")
def model_download(
    checkpoint: str = typer.Option(None, "--checkpoint", "-c", help="HF repo ID to download"),
):
    """Pre-download the OmniVoice model so first synthesis doesn't block."""
    from agent_ptt.modelcache import DEFAULT_CHECKPOINT, download_model, hub_available

    if not hub_available():
        rprint(f"[bold red]❌ huggingface_hub not available.[/bold red] {_EXTRA_HINT}")
        raise typer.Exit(1)

    repo_id = checkpoint or DEFAULT_CHECKPOINT
    rprint(f"[bold green]⬇️  Downloading[/bold green] {repo_id} (resumes if partial)...")
    path = download_model(repo_id)
    rprint(f"[bold green]✅ Model ready:[/bold green] [dim]{path}[/dim]")


@model_app.command("list")
def model_list():
    """List models in the local HuggingFace cache."""
    from agent_ptt.modelcache import format_size, hub_available, list_cached_models

    if not hub_available():
        rprint(f"[bold red]❌ huggingface_hub not available.[/bold red] {_EXTRA_HINT}")
        raise typer.Exit(1)

    models = list_cached_models()
    if not models:
        rprint("[dim]No models in the local HuggingFace cache[/dim]")
        return

    table = Table(title="Cached Models (~/.cache/huggingface)")
    table.add_column("Repo ID", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Files", justify="right")

    for m in models:
        table.add_row(m.repo_id, format_size(m.size_bytes), str(m.nb_files))
    console.print(table)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@app.command("config")
def config(
    server_url: str = typer.Option(None, "--server", "-s", help="Set server URL"),
):
    """View or update configuration."""
    session = _load_session()

    if server_url:
        session["base_url"] = server_url.rstrip("/")
        _save_session(session)
        rprint(f"[bold green]✅ Server URL set to:[/bold green] {server_url}")
    else:
        rprint(f"Server URL: [cyan]{session.get('base_url', DEFAULT_BASE_URL)}[/cyan]")
        if "key_id" in session:
            rprint(f"Active key: [cyan]{session['key_id']}[/cyan]")
            rprint(f"Channel:    [cyan]{session.get('channel_id', 'N/A')}[/cyan]")
            rprint(f"Handle:     [cyan]{session.get('handle', 'N/A')}[/cyan]")
        else:
            rprint("[dim]Not connected to any channel[/dim]")


if __name__ == "__main__":
    app()
