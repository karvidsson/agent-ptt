# Voice Profiles

## Overview

Voice profiles define how an agent's text messages sound when synthesized to speech. Pocket TTS is the only supported built-in engine.

## Schema

```json
{
  "voice_id": "alba",
  "display_name": "Alba",
  "engine": "pocket-tts",
  "settings": {
    "voice": "alba"
  },
  "created_at": "2026-07-02T07:19:57.000Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `voice_id` | string | Unique identifier (maps to engine-specific voice name) |
| `display_name` | string | Human-readable name |
| `engine` | string | TTS engine identifier (`pocket-tts`) |
| `settings` | object | Engine-specific parameters |
| `created_at` | datetime | When the profile was created |

## Stored Profiles & Auto-Designed Voices

Profiles can be persisted in the database and referenced by `voice_id` when joining. Manage them with the `voice` CLI group (`agent-ptt voice list|show|save|delete`) or the `/voices/profiles` REST endpoints.

Joining **without** `--voice` auto-designs a deterministic voice from your handle and pins it in the `pinned_voices` table — the same handle always gets the same voice across sessions:

```bash
agent-ptt join <channel-id> --handle "Claude"
# ✅ Joined as [Claude]
#    Voice: auto-designed {"voice": "cosette"}
```

Automatic assignment always selects a Pocket TTS catalog voice deterministically. Handles can share a catalog voice.

On server startup, saved legacy engine profiles (including removed OmniVoice profiles) are migrated to Pocket TTS while preserving their IDs and pin references. Their sound changes; old instruction, rate, and pitch settings are discarded. For an old raw voice name not saved as a profile, choose a Pocket voice when rejoining.

## Engine-Specific Settings

### pocket-tts (default)

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `voice` | string | `alba` | Catalog name, reference audio path, or exported voice state path |

[Pocket TTS](https://github.com/kyutai-labs/pocket-tts) runs locally on CPU. Model and voice downloads happen on first use; subsequent synthesis uses the cache. Model loading and inference run off the event loop, serialized across channels, with a bounded cache of voice states. Output is mono PCM16 WAV.

```bash
uv sync
agent-ptt voices
agent-ptt voice clone --reference ./sample.wav --name "My voice"
```

Cloning needs no transcript. Reference files must remain available on the synthesis host. Description-based voice design is no longer supported; use catalog selection or reference-audio cloning.

## Available Voices

The curated catalog is: `alba`, `marius`, `javert`, `jean`, `fantine`, `cosette`, `eponine`, `azelma`.
Run `agent-ptt voices` to list it without loading the model.

## Using Voices

### When joining a channel

```bash
agent-ptt join <channel-id> --handle "Claude" --voice "marius"
```

### Via the API

```bash
curl -X POST http://localhost:8770/channels/<id>/join \
  -H "Content-Type: application/json" \
  -d '{"handle": "Claude", "voice_id": "marius"}'
```

## Adding Custom TTS Engines

Subclass `TTSBackend` and register it:

```python
from agent_ptt.tts import TTSBackend, register_backend
from agent_ptt.models import VoiceProfile

class MyTTSBackend(TTSBackend):
    @property
    def engine_name(self) -> str:
        return "my-engine"

    async def synthesize(self, text: str, voice_profile: VoiceProfile) -> bytes:
        # Your TTS logic here — return WAV or MP3 bytes
        ...

    async def list_voices(self) -> list[VoiceProfile]:
        # Return available voices
        ...

register_backend("my-engine", MyTTSBackend())
```
