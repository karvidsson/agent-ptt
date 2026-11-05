"""Portable built-in voices, including bundled voice states."""

from pathlib import Path

POCKET_DEFAULTS = ("alba", "marius", "javert", "jean", "fantine", "cosette", "eponine", "azelma")
BUNDLED_VOICES = (
    "ruler",
    "cruncher",
    "builder",
    "rocky",
    "slate",
    "titan",
    "ranger",
    "jolt",
    "ripley",
    "marshall",
    "dash",
    "walker",
    "maverick",
    "ivan",
    "sarah",
    "striker",
    "fender",
    "ledger",
    "manager",
    "insecure",
)
BUILTIN_VOICES = (*POCKET_DEFAULTS, *BUNDLED_VOICES)
STATE_DIR = Path(__file__).resolve().parent / "voice_states"


def resolve_voice(voice: str) -> str:
    """Resolve only known catalog IDs; local custom paths retain their behavior."""
    if voice in BUNDLED_VOICES:
        return str(STATE_DIR / f"{voice}.safetensors")
    return voice
