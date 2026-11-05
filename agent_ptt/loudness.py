"""Per-utterance loudness normalization (ITU-R BS.1770 integrated loudness).

Every synthesized clip is measured and scaled by one constant gain so all
voices — built-in, bundled, cloned or saved profiles — reach the same target
loudness. Speech peaks sit up to ~21 dB above its loudness, so reaching the
target can push a few transients over the peak ceiling; a short lookahead
limiter pulls those down (at most LIMITER_MAX_DB), and beyond that the gain is
capped instead. Near-silence is never boosted into audible noise. Anything
that isn't a parsable WAV passes through unchanged.
"""

from __future__ import annotations

import io
import logging
import math
import os

logger = logging.getLogger(__name__)

DEFAULT_TARGET_LUFS = -18.0
PEAK_CEILING_DBFS = -1.0
MAX_GAIN_DB = 30.0
LIMITER_MAX_DB = 6.0
TARGET_ENV = "AGENT_PTT_TARGET_LUFS"

_BLOCK_SECONDS = 0.4
_STEP_SECONDS = 0.1  # 75% overlap between gating blocks
_LIMITER_WINDOW_SECONDS = 0.01  # attack/release ramps of about 20 ms
_ABSOLUTE_GATE_LUFS = -70.0
_RELATIVE_GATE_LU = -10.0


def target_lufs() -> float | None:
    """The configured target, or None when normalization is turned off."""
    raw = os.environ.get(TARGET_ENV, "").strip()
    if not raw:
        return DEFAULT_TARGET_LUFS
    if raw.lower() in {"off", "none", "disabled"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"Ignoring invalid {TARGET_ENV}={raw!r}; using {DEFAULT_TARGET_LUFS}")
        return DEFAULT_TARGET_LUFS
    if not math.isfinite(value) or value >= 0:
        logger.warning(f"Ignoring invalid {TARGET_ENV}={raw!r}; using {DEFAULT_TARGET_LUFS}")
        return DEFAULT_TARGET_LUFS
    return value


def _k_weighting(sample_rate: int):
    """BS.1770 pre-filter (high shelf + RLB high-pass) for any sample rate."""
    # Analog prototype parameters as derived for libebur128 / pyloudnorm.
    k = math.tan(math.pi * 1681.974450955533 / sample_rate)
    vh = 10 ** (3.999843853973347 / 20)
    vb = vh**0.4996667741545416
    q = 0.7071752369554196
    a0 = 1 + k / q + k * k
    shelf_b = [
        (vh + vb * k / q + k * k) / a0,
        2 * (k * k - vh) / a0,
        (vh - vb * k / q + k * k) / a0,
    ]
    shelf_a = [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]

    k = math.tan(math.pi * 38.13547087602444 / sample_rate)
    q = 0.5003270373238773
    a0 = 1 + k / q + k * k
    hp_b = [1.0, -2.0, 1.0]
    hp_a = [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    return (shelf_b, shelf_a), (hp_b, hp_a)


def _to_lufs(power: float) -> float:
    return -0.691 + 10 * math.log10(power) if power > 0 else -math.inf


def integrated_loudness(samples, sample_rate: int) -> float:
    """Integrated loudness in LUFS of float samples shaped (frames,) or (frames, channels).

    Clips shorter than one 400 ms gating block are measured as ungated
    K-weighted mean square instead. Returns -inf for digital silence.
    """
    import numpy as np
    from scipy.signal import lfilter

    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    if data.shape[0] == 0:
        return -math.inf
    (sb, sa), (hb, ha) = _k_weighting(sample_rate)
    weighted = lfilter(hb, ha, lfilter(sb, sa, data, axis=0), axis=0)
    squared = weighted**2  # mono/stereo channel weights are all 1.0

    block = round(_BLOCK_SECONDS * sample_rate)
    step = round(_STEP_SECONDS * sample_rate)
    if squared.shape[0] < block:
        return _to_lufs(float(squared.mean(axis=0).sum()))

    starts = range(0, squared.shape[0] - block + 1, step)
    cumulative = np.concatenate([np.zeros((1, squared.shape[1])), np.cumsum(squared, axis=0)])
    powers = np.array([((cumulative[s + block] - cumulative[s]) / block).sum() for s in starts])
    with np.errstate(divide="ignore"):
        loudness = -0.691 + 10 * np.log10(powers)
    gated = powers[loudness > _ABSOLUTE_GATE_LUFS]
    if gated.size == 0:
        return -math.inf
    relative_gate = _to_lufs(float(gated.mean())) + _RELATIVE_GATE_LU
    gated = powers[(loudness > _ABSOLUTE_GATE_LUFS) & (loudness > relative_gate)]
    return _to_lufs(float(gated.mean()))


def normalization_gain_db(loudness: float, peak: float, target: float) -> float:
    """Gain toward `target`, capped by MAX_GAIN_DB and by what the limiter may absorb."""
    if not math.isfinite(loudness) or loudness < _ABSOLUTE_GATE_LUFS or peak <= 0:
        return 0.0  # silence or near-silence: leave it alone
    gain = min(target - loudness, MAX_GAIN_DB)
    return min(gain, PEAK_CEILING_DBFS + LIMITER_MAX_DB - 20 * math.log10(peak))


def limit_peaks(samples, sample_rate: int, ceiling: float):
    """Smooth lookahead limiter: keep every |sample| <= ceiling (linear).

    The per-sample gain needed is min-filtered and then box-averaged over the
    same window, which keeps the envelope at or under the required gain at
    every peak while ramping in and out over ~2 windows instead of clipping.
    """
    import numpy as np
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    level = np.max(np.abs(samples), axis=1)
    if level.max() <= ceiling:
        return samples
    needed = np.minimum(1.0, ceiling / np.maximum(level, 1e-12))
    size = 2 * max(1, round(_LIMITER_WINDOW_SECONDS * sample_rate)) + 1
    floor = minimum_filter1d(needed, size, mode="nearest")
    envelope = uniform_filter1d(floor, size, mode="nearest")
    # Float rounding in the average can leave a hair over the ceiling.
    return np.clip(samples * envelope[:, None], -ceiling, ceiling)


def normalize_loudness(wav_bytes: bytes, target: float | None = None) -> bytes:
    """Scale a WAV clip to the target loudness; unparsable input is returned as-is."""
    if target is None:
        target = target_lufs()
        if target is None:
            return wav_bytes
    try:
        import numpy as np
        import soundfile as sf

        with sf.SoundFile(io.BytesIO(wav_bytes)) as clip:
            if clip.format != "WAV":
                raise ValueError(f"not a WAV clip ({clip.format})")
            subtype = clip.subtype
            sample_rate = clip.samplerate
            samples = clip.read(dtype="float64", always_2d=True)
    except Exception as e:
        logger.debug(f"Loudness normalization skipped: {e}")
        return wav_bytes

    if samples.size == 0:
        return wav_bytes
    peak = float(np.max(np.abs(samples)))
    loudness = integrated_loudness(samples, sample_rate)
    gain_db = normalization_gain_db(loudness, peak, target)
    if abs(gain_db) < 0.01:
        return wav_bytes

    ceiling = 10 ** (PEAK_CEILING_DBFS / 20)
    max_gain_db = normalization_gain_db(loudness, peak, math.inf)
    for _ in range(3):
        scaled = limit_peaks(samples * 10 ** (gain_db / 20), sample_rate, ceiling)
        # Limiting shaves a little loudness off; make that up within the budget.
        shortfall = target - integrated_loudness(scaled, sample_rate)
        if shortfall < 0.05 or gain_db >= max_gain_db:
            break
        gain_db = min(gain_db + shortfall, max_gain_db)
    if scaled.shape[1] == 1:
        scaled = scaled[:, 0]
    output = io.BytesIO()
    sf.write(output, scaled, sample_rate, format="WAV", subtype=subtype)
    return output.getvalue()
