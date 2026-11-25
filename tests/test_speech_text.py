"""Speech-only text normalization: paths, links and IDs become short stand-ins."""

import asyncio

import pytest

from agent_ptt.models import VoiceProfile
from agent_ptt.speech_text import spoken_text
from agent_ptt.tts import synthesize_speech
from tests.conftest import FakeTTSBackend


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        (
            "Read /Users/krille/.claude/orchestra/projects/litium-codex/tasks/new-website/"
            "components/newsletter-brief.md now.",
            "Read a Markdown file now.",
        ),
        ("see ./scripts/install_plugins.py", "see a Python file"),
        ("in ../docs/report.pdf", "in a PDF"),
        ("fixed agent_ptt/tts.py:96 and docs/voices.md", "fixed a Python file and a Markdown file"),
        ("open C:\\Users\\k\\report.pdf", "open a PDF"),
        ("C:\\Users\\k\\Projects\\", "a folder"),
        ("file:///home/k/pic.png", "an image"),
        ("see https://github.com/karvidsson/agent-ptt/pull/3, ok?", "see a link, ok?"),
        ("the ~/.agent-ptt/announcer.env file", "a config file"),
        ("look in ~/Projects/agent-ptt/", "look in a folder"),
        ("worktree /home/k/Projects/agent-ptt-worktrees/task45", "worktree a folder"),
        ("data at /tmp/x.weird", "data at a file"),
        ("src/a/", "a folder"),
        ("edit `plugins/announcer/hooks/ptt.py` first", "edit a Python file first"),
        ("item 64e6dac9-d9f1-463d-aad7-709f8323ebd8 is done", "item an ID is done"),
        ("digest 52e68b6c0ffee1234abcd5", "digest a hash"),
        ("chart.svg in assets/img/chart.svg", "chart.svg in an image"),
    ],
)
def test_paths_links_and_ids_become_stand_ins(text, spoken):
    assert spoken_text(text) == spoken


@pytest.mark.parametrize(
    "text",
    [
        "and/or, 1/2 done, TCP/IP, yes/no",
        "POST /reap and GET /projects/x/work-items",
        "GET /projects/{p}/usage and shared/automation/** stay",
        "merged 79a8483 at 10:30, version 0.6.2",
        "brief.md and tts.py stay as bare names",
        "src/x.unknownext stays",
        "the cat sat on the mat.",
        "`ptt wait --first`",
        "",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert spoken_text(text) == text


def test_synthesize_speech_speaks_the_stand_in_only():
    backend = FakeTTSBackend()
    profile = VoiceProfile(voice_id="v", display_name="V", engine="fake", settings={})
    text = "Wrote /Users/k/notes/plan.md"
    asyncio.run(synthesize_speech(backend, text, profile))
    assert backend.calls[0][0] == "Wrote a Markdown file"
