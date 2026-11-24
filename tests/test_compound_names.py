"""The reviewed compound names, and where they sit in the allocation order."""

from datetime import UTC, datetime, timedelta

from agent_ptt import handles
from agent_ptt.compound_names import COMPOUNDS, FIRST, SECOND
from agent_ptt.db import SessionLocal
from agent_ptt.mentions import _MENTION
from agent_ptt.models import HandleClaimDB
from agent_ptt.session_identity import NAMES

# Substrings no compound may contain anywhere (scripts/propose_compounds.py
# flags candidates with this list too).
BLOCKLIST = (
    "anal", "anus", "arse", "ass", "bitch", "boob", "bum", "butt", "cock", "coke", "crap",
    "cum", "cunt", "damn", "dead", "dick", "die", "dyke", "fag", "fart", "fuck", "gay",
    "hell", "homo", "jap", "jew", "jizz", "kike", "kill", "kkk", "meth", "nazi", "nig",
    "nob", "pedo", "pee", "pimp", "piss", "poo", "porn", "pube", "puke", "pus", "rape",
    "sex", "shit", "slut", "snot", "spic", "spunk", "tard", "tit", "turd", "twat", "vomit",
    "wank", "weed", "whore",
)  # fmt: skip


def distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


# --- the list itself ----------------------------------------------------------


def test_enough_compounds():
    assert len(COMPOUNDS) >= 200


def test_parts_are_short_and_plain():
    for part in FIRST + SECOND:
        assert 3 <= len(part) <= 5, part
        assert part.isascii() and part.isalpha(), part
    assert all(part[0].isupper() and part[1:].islower() for part in FIRST)
    assert all(part.islower() for part in SECOND)
    assert len(set(FIRST)) == len(FIRST) and len(set(SECOND)) == len(SECOND)


def test_every_compound_is_a_first_and_a_second_part():
    pairs = {first + second for first in FIRST for second in SECOND}
    for name in COMPOUNDS:
        assert name in pairs, name


def test_compounds_are_one_capitalized_word():
    for name in COMPOUNDS:
        assert len(name) <= 9, name
        assert name.isascii() and name.isalpha(), name
        assert name[0].isupper() and name[1:].islower(), name


def test_no_doubled_letters_at_the_seam():
    for first in FIRST:
        for second in SECOND:
            if first + second in COMPOUNDS:
                assert first[-1].lower() != second[0], first + second


def test_no_blocklisted_substrings():
    for name in COMPOUNDS:
        low = name.lower()
        assert not [bad for bad in BLOCKLIST if bad in low], name


def test_no_duplicates_or_near_duplicates():
    assert len({name.casefold() for name in COMPOUNDS}) == len(COMPOUNDS)
    plain = [name.casefold() for name in NAMES]
    compounds = [name.casefold() for name in COMPOUNDS]
    for i, name in enumerate(compounds):
        for other in plain:
            assert distance(name, other) > 1, (name, other)
        for other in compounds[i + 1 :]:
            assert distance(name, other) > 1, (name, other)


def test_no_compound_starts_with_a_plain_name():
    for name in COMPOUNDS:
        assert not [p for p in NAMES if name.casefold().startswith(p.casefold())], name


# --- allocation order ---------------------------------------------------------


def hold(db, names, *, age=timedelta(0)):
    when = datetime.now(UTC) - age
    for index, name in enumerate(names):
        owner = handles.Owner(handles.session_owner("Codex", f"held-{name}-{index}"))
        handles.take(db, name, owner).last_seen = when
    db.commit()


def test_all_plain_names_live_gives_a_compound():
    with SessionLocal() as db:
        hold(db, NAMES)
        assert handles.allocate(db) in COMPOUNDS


def test_all_plain_names_and_compounds_live_gives_a_numbered_name():
    with SessionLocal() as db:
        hold(db, NAMES + COMPOUNDS)
        name = handles.allocate(db)
        assert name.endswith("2") and name.removesuffix("2") in NAMES


def test_a_freed_plain_name_is_preferred_over_compounds_again():
    with SessionLocal() as db:
        hold(db, NAMES)
        hold(db, COMPOUNDS[:10])
        assert handles.allocate(db) in COMPOUNDS[10:]
        db.get(HandleClaimDB, "theo").last_seen = datetime.now(UTC) - timedelta(hours=25)
        db.commit()
        assert handles.allocate(db) == "Theo"


def test_compounds_follow_the_within_tier_rules():
    with SessionLocal() as db:
        hold(db, NAMES)
        hold(db, COMPOUNDS, age=timedelta(hours=30))
        db.get(HandleClaimDB, COMPOUNDS[7].casefold()).last_seen = datetime.now(UTC) - timedelta(
            days=9
        )
        db.commit()
        # All compounds expired: the one that expired longest ago wins.
        assert handles.allocate(db) == COMPOUNDS[7]
        db.delete(db.get(HandleClaimDB, COMPOUNDS[3].casefold()))
        db.commit()
        # A never-claimed compound beats every expired one.
        assert handles.allocate(db) == COMPOUNDS[3]


def test_a_compound_rename_target_is_itself():
    # Compounds have no digits, so the default admin rename target is the same
    # name (the rename is refused as a no-op rather than mangling it).
    assert all(handles.default_rename_target(name) == name for name in COMPOUNDS)


def test_compounds_are_plain_mentions():
    # No space or punctuation: "@Fernwick" needs no quotes.
    for name in COMPOUNDS:
        match = _MENTION.fullmatch(f"@{name}")
        assert match and match[3] == name, name
