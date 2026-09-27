"""The roster inspector works without joining and reports recorded metadata."""

from agent_ptt import channel as ch
from agent_ptt.models import MessageContext


def test_participant_details_includes_session_harness_voice_and_activity(client):
    cid = client.post("/channels", json={"name": "inspector"}).json()["channel_id"]
    profile = {"voice_id": "builder", "display_name": "Builder", "settings": {"voice": "alba"}}
    client.post("/voices/profiles", json=profile)
    participant = client.post(
        f"/channels/{cid}/join",
        json={"handle": "original", "session_id": "cli-session-1", "agent": "Codex"},
    ).json()
    key = participant["key_id"]
    message = client.post(
        f"/channels/{cid}/say", json={"key_id": key, "text": "Building the UI"}
    ).json()
    # Context is optional and can arrive independently of the stored session identity.
    ch.get_channel(cid).messages[-1].context = MessageContext(repo="agent-ptt", branch="main")
    response = client.get(f"/channels/{cid}/participants/{key}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["harness"] == "Codex"
    assert detail["session"]["session_id"] == "cli-session-1"
    assert detail["voice"]["settings"] == {"voice": "alba"}
    assert detail["participant"]["handle"] == participant["handle"]
    assert detail["connections"][0]["key_id"] == key
    assert detail["activity"]["message_count"] == 1
    assert detail["activity"]["last_message"]["message_id"] == message["message_id"]
    assert detail["context"]["repo"] == "agent-ptt"


def test_manual_participant_has_no_invented_harness_and_is_channel_scoped(client):
    cid = client.post("/channels", json={"name": "first"}).json()["channel_id"]
    other = client.post("/channels", json={"name": "second"}).json()["channel_id"]
    key = client.post(f"/channels/{cid}/join", json={"handle": "Human", "voice_id": "alba"}).json()[
        "key_id"
    ]
    detail = client.get(f"/channels/{cid}/participants/{key}").json()
    assert detail["session"] is None
    assert detail["harness"] is None
    assert detail["voice"] is None
    assert detail["activity"] == {"message_count": 0, "last_message": None}
    assert client.get(f"/channels/{other}/participants/{key}").status_code == 404
    client.post(f"/channels/{cid}/kick/{key}")
    assert client.get(f"/channels/{cid}/participants/{key}").status_code == 404
