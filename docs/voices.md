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

The built-in catalog has 28 voices, available on fresh installs and in the local
app voice picker. Hosted agents receive a saved voice from the same catalog when registered.
Assignment prefers unused voices in the organization, then the least-used voices.

- Pocket defaults: `alba`, `marius`, `javert`, `jean`, `fantine`, `cosette`, `eponine`, `azelma`.
- App voices: `ruler`, `cruncher`, `builder`, `rocky`, `slate`, `titan`, `ranger`,
  `jolt`, `ripley`, `marshall`, `dash`, `walker`, `maverick`, `ivan`, `sarah`,
  `striker`, `fender`, `ledger`, `manager`, `insecure`.

The app voices ship as voice-state files in `agent_ptt/voice_states/`; source
recordings and preview audio are not bundled. No local database or Downloads
folder is needed to use them. Catalog responses contain voice IDs, never file paths.
Agent voice choices persist across reconnects, channels, and catalog additions.
Owners and admins can change an agent's voice in its participant profile, or under
organization management using the agent's **Voice** button. Members can view the
choice. Changes apply to subsequent speech requests. Human speech still uses a
deterministic catalog assignment.

The hosted API exposes the catalog at `GET /api/workspace/organizations/{org_id}/voices`
and accepts `PATCH /api/workspace/organizations/{org_id}/agents/{agent_id}/voice`
with `{"voice_id": "ledger"}`. Only built-in IDs are accepted.

Run `agent-ptt voices` to list it without loading the model.

## Loudness Normalization

Raw Pocket TTS output varies a lot between voices: one fixed sentence measured
from about -44 LUFS (`jean`) to -16 LUFS (`cruncher`), a 27 dB spread. So every
synthesized clip is normalized to the same base loudness before anyone hears it
(`agent_ptt/loudness.py`, applied by `tts.synthesize_speech()`):

- **Target:** -18 LUFS integrated loudness (ITU-R BS.1770: K-weighting, 400 ms
  blocks, absolute and relative gating). Set `AGENT_PTT_TARGET_LUFS` to another
  negative number (e.g. `-23`) to change it, or to `off` to disable
  normalization. Invalid values fall back to -18 with a warning.
- **One gain per utterance.** Synthesis returns one complete WAV per message,
  so the whole clip is measured and scaled by a single constant gain — no
  pumping within a sentence, and it adapts to cloned voices and saved profiles
  without a calibration table.
- **Peak ceiling -1 dBFS** (sample peak). Speech peaks sit up to ~21 dB above
  its loudness, so a short lookahead limiter (~20 ms ramps) pulls the few
  transients that would cross the ceiling back under it. It may take at most
  6 dB off any peak; a clip that would need more gets less gain instead and
  ends up quieter than the target.
- **Gain is capped at +30 dB**, and clips below -70 LUFS (silence, near-silence)
  are left untouched, so noise is never boosted to speech level. Clips shorter
  than one 400 ms block are measured ungated.
- Output stays a WAV with the input's sample rate, channel count and subtype
  (mono PCM_16 from Pocket TTS). Anything that isn't a parsable WAV — including
  MP3 from a custom engine — passes through unchanged.

To re-measure all built-in voices with the real model (raw, or as the server
plays them):

```bash
HF_HUB_OFFLINE=1 uv run python scripts/measure_voice_loudness.py [--normalized]
```

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
        # Your TTS logic here — return WAV bytes (MP3 skips loudness normalization)
        ...

    async def list_voices(self) -> list[VoiceProfile]:
        # Return available voices
        ...

register_backend("my-engine", MyTTSBackend())
```
