"""Measure the loudness of every built-in voice with the real Pocket TTS model.

Synthesizes one fixed sentence per voice and prints integrated loudness
(LUFS, ITU-R BS.1770) and sample peak (dBFS) as a Markdown table — the raw
model output by default, or the normalized clip the server plays with
--normalized. Run offline once the model is cached:

    HF_HUB_OFFLINE=1 uv run python scripts/measure_voice_loudness.py [--normalized]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import statistics

import numpy as np
import soundfile as sf

from agent_ptt.loudness import integrated_loudness, target_lufs
from agent_ptt.models import VoiceProfile
from agent_ptt.tts import PocketTTSBackend, synthesize_speech
from agent_ptt.voice_catalog import BUILTIN_VOICES

SENTENCE = "The build finished and all the tests passed, so I'm deploying to staging now."


async def measure(normalized: bool) -> list[tuple[str, float, float]]:
    backend = PocketTTSBackend()
    rows = []
    for voice in BUILTIN_VOICES:
        profile = VoiceProfile(
            voice_id=voice, display_name=voice, engine="pocket-tts", settings={"voice": voice}
        )
        if normalized:
            wav = await synthesize_speech(backend, SENTENCE, profile)
        else:
            wav = await backend.synthesize(SENTENCE, profile)
        samples, rate = sf.read(io.BytesIO(wav), dtype="float64", always_2d=True)
        peak = float(np.max(np.abs(samples)))
        peak_db = 20 * math.log10(peak) if peak > 0 else -math.inf
        rows.append((voice, integrated_loudness(samples, rate), peak_db))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--normalized", action="store_true", help="measure the normalized clip")
    args = parser.parse_args()

    rows = asyncio.run(measure(args.normalized))
    target = target_lufs()
    print(f"Sentence: {SENTENCE!r}  target: {target} LUFS\n")
    print("| voice | LUFS | peak dBFS | off target (dB) |")
    print("|---|---:|---:|---:|")
    for voice, lufs, peak in rows:
        delta = f"{lufs - target:+.2f}" if target is not None else "-"
        print(f"| {voice} | {lufs:.2f} | {peak:.2f} | {delta} |")
    values = [lufs for _, lufs, _ in rows]
    print(
        f"\nmin {min(values):.2f}  max {max(values):.2f}  spread {max(values) - min(values):.2f} dB"
        f"  mean {statistics.mean(values):.2f}  stdev {statistics.pstdev(values):.2f}"
    )


if __name__ == "__main__":
    main()
