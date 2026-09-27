"""TTS backend registry."""

import pytest

from agent_ptt.tts import PocketTTSBackend, get_backend, register_backend
from tests.conftest import FakeTTSBackend


def test_get_default_backend():
    backend = get_backend()
    assert isinstance(backend, PocketTTSBackend)
    assert backend.engine_name == "pocket-tts"


@pytest.mark.parametrize("engine", ["edge-tts", "system", "omnivoice"])
def test_removed_backends(engine):
    with pytest.raises(ValueError, match="Unknown TTS engine"):
        get_backend(engine)


def test_get_unknown_backend():
    with pytest.raises(ValueError, match="Unknown TTS engine 'nope'"):
        get_backend("nope")


def test_register_custom_backend():
    custom = FakeTTSBackend()
    register_backend("fake", custom)
    assert get_backend("fake") is custom
