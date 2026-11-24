"""Propose compound names for review: every FIRST x SECOND pair, with flags.

Usage: uv run python scripts/propose_compounds.py [--all]

Prints each candidate from ``agent_ptt.compound_names.FIRST`` and ``SECOND``
with automatic flags (too long, doubled or slurring seam, blocklisted
substring, near-duplicate of a plain name or another candidate, plain-name
prefix) and whether it is already in ``COMPOUNDS``. The flags only help the
review: a person reads every candidate and copies the ones to keep into the
literal ``COMPOUNDS`` tuple by hand. Nothing here is used at runtime.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_ptt.compound_names import COMPOUNDS, FIRST, SECOND  # noqa: E402
from agent_ptt.session_identity import NAMES  # noqa: E402

MAX_LENGTH = 9
# The blocklist lives in the test that enforces it.
_spec = importlib.util.spec_from_file_location(
    "compound_test", ROOT / "tests/test_compound_names.py"
)
_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_test)
BLOCKLIST = _test.BLOCKLIST
# Seams where the second part's first letters run into the first part's last.
SLURS = ("ch+h", "t+h", "p+h", "s+h", "c+h", "k+c", "c+k", "k+k")


def distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def flags(first: str, second: str, everyone: list[str]) -> list[str]:
    name = first + second
    low = name.lower()
    out = []
    if len(name) > MAX_LENGTH:
        out.append(f"length {len(name)}")
    if first[-1].lower() == second[0].lower():
        out.append("doubled seam")
    for slur in SLURS:
        end, start = slur.split("+")
        if first.lower().endswith(end) and second.lower().startswith(start):
            out.append(f"seam {slur}")
    out += [f"contains {bad!r}" for bad in BLOCKLIST if bad in low]
    out += [f"near {plain}" for plain in NAMES if distance(low, plain.lower()) <= 1]
    out += [f"starts with {plain}" for plain in NAMES if low.startswith(plain.lower())]
    out += [
        f"near {other}" for other in everyone if other != name and distance(low, other.lower()) <= 1
    ]
    return out


def main() -> None:
    show_all = "--all" in sys.argv
    candidates = [(f, s) for f in FIRST for s in SECOND]
    everyone = [f + s for f, s in candidates]
    kept = set(COMPOUNDS)
    for first, second in candidates:
        name = first + second
        found = flags(first, second, everyone)
        mark = "KEPT   " if name in kept else "dropped"
        if show_all or found or name not in kept:
            print(f"{mark} {name:<10} {'; '.join(found)}")
    print(f"\n{len(candidates)} candidates, {len(kept)} kept", file=sys.stderr)


if __name__ == "__main__":
    main()
