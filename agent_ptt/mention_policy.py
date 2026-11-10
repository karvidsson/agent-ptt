"""Versioned receive contract advertised to agent plugins at connection time.

Still version 1: older plugins accept only version 1 with ``idle_wakeup: false``
(see the announcer's ``receive_policy``), so the long-poll is advertised with
additive keys they ignore. ``wait_supported`` means ``GET /channels/{id}/inbox``
takes ``?wait=<0..wait_max_seconds>``; a monitor (``ptt.py wait``,
``agent-ptt inbox --follow``) uses it to wake an idle session.
"""


def mention_policy():
    return {
        "version": 1,
        "transport": "hooks",
        "tool_poll_interval_seconds": 2,
        "check_on": ["UserPromptSubmit", "PostToolUse", "Stop"],
        "idle_wakeup": False,
        "wait_supported": True,
        "wait_max_seconds": 60,
    }
