"""List-price table for the optional "≈ $" usage estimate. EDIT FREELY.

This is an ESTIMATE at public API list prices. Agents on a subscription
(Claude Max/Pro, ChatGPT plans) do not pay this; it only helps compare agents.
The API returns ``est_usd`` only when asked (``?estimate=1``) and the web UI
keeps it off by default.

Prices are US dollars per million tokens: ``input`` (uncached), ``cache_read``,
``cache_write`` (Claude: the 5-minute write, 1.25x input) and ``output``
(thinking / reasoning tokens are billed as output and already counted there).
A model missing from the table gets no estimate. Names match exactly, or with
a variant suffix (``[1m]``, ``@…``, a ``-2026…`` date), so ``claude-opus-5-5[1m]``
uses ``claude-opus-5-5`` but ``claude-opus-5-6`` stays unpriced.

Checked 2026-11-19. Claude rates come from Anthropic's model table. The GPT
rates come from third-party price lists, not OpenAI's own page, so they may be
off; gpt-5.6-luna had conflicting figures and is left out on purpose.
"""

from __future__ import annotations

import re

PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    # Anthropic (first-party API list prices)
    "claude-fable-5-1": {"input": 10.0, "cache_read": 0.25, "cache_write": 12.5, "output": 50.0},
    "claude-fable-5": {"input": 10.0, "cache_read": 1.0, "cache_write": 12.5, "output": 50.0},
    "claude-opus-5-5": {"input": 4.0, "cache_read": 0.20, "cache_write": 5.0, "output": 20.0},
    "claude-opus-5": {"input": 5.0, "cache_read": 0.50, "cache_write": 6.25, "output": 25.0},
    "claude-sonnet-5-5": {"input": 2.0, "cache_read": 0.20, "cache_write": 2.5, "output": 10.0},
    "claude-sonnet-5": {"input": 2.0, "cache_read": 0.20, "cache_write": 2.5, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "cache_read": 0.01, "cache_write": 0.125, "output": 0.50},
    # OpenAI models seen in Codex rollouts (no separate cache-write charge).
    "gpt-6-astra": {"input": 10.0, "cache_read": 1.0, "cache_write": 10.0, "output": 50.0},
    "gpt-6.1-sol": {"input": 2.0, "cache_read": 0.20, "cache_write": 2.0, "output": 10.0},
}

PARTS = ("input", "cache_read", "cache_write", "output")
_VARIANT = re.compile(r"^(.+?)(?:\[[^\]]*\]|@.*|-20\d{6})$")


def price_for(model: str) -> dict[str, float] | None:
    """The price row for ``model``: an exact name, or one plus a variant suffix."""
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    match = _VARIANT.match(model)
    return PRICES_PER_MTOK.get(match.group(1)) if match else None


def estimate_usd(model: str, counts: dict[str, int]) -> float | None:
    """List-price dollars for one model's counts; ``None`` for an unknown model."""
    price = price_for(model)
    if price is None:
        return None
    total = sum(counts.get(part, 0) * price[part] for part in PARTS) / 1_000_000
    return round(total, 4)
