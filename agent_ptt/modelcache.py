"""Local model cache management — inspect and pre-download HF checkpoints.

huggingface_hub arrives with Pocket TTS, so it's imported
lazily; every entry point degrades gracefully on the base install.
"""

from __future__ import annotations

import importlib.util

from pydantic import BaseModel

DEFAULT_CHECKPOINT = "kyutai/pocket-tts"

__all__ = [
    "DEFAULT_CHECKPOINT",
    "CachedModel",
    "download_model",
    "format_size",
    "get_cached_model",
    "hub_available",
    "list_cached_models",
]


class CachedModel(BaseModel):
    """A model repo present in the local HuggingFace cache."""

    repo_id: str
    size_bytes: int
    nb_files: int
    path: str


def hub_available() -> bool:
    """True when huggingface_hub is installed (comes with Pocket TTS)."""
    return importlib.util.find_spec("huggingface_hub") is not None


def format_size(size_bytes: int) -> str:
    """Human-readable size, e.g. 2.4 GB."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def list_cached_models() -> list[CachedModel]:
    """List model repos in the local HF cache, largest first."""
    from huggingface_hub import scan_cache_dir

    try:
        cache = scan_cache_dir()
    except Exception:  # cache dir doesn't exist yet
        return []

    models = [
        CachedModel(
            repo_id=repo.repo_id,
            size_bytes=repo.size_on_disk,
            nb_files=repo.nb_files,
            path=str(repo.repo_path),
        )
        for repo in cache.repos
        if repo.repo_type == "model"
    ]
    return sorted(models, key=lambda m: m.size_bytes, reverse=True)


def get_cached_model(repo_id: str) -> CachedModel | None:
    """Look up one repo in the local HF cache."""
    return next((m for m in list_cached_models() if m.repo_id == repo_id), None)


def download_model(repo_id: str = DEFAULT_CHECKPOINT) -> str:
    """Download (or resume) a checkpoint into the local HF cache.

    Returns the snapshot path. Shows huggingface_hub's own progress bars.
    """
    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=repo_id)
