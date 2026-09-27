"""Versioned receive contract advertised to agent plugins at connection time."""


def mention_policy():
    return {
        "version": 1,
        "transport": "hooks",
        "tool_poll_interval_seconds": 2,
        "check_on": ["UserPromptSubmit", "PostToolUse", "Stop"],
        "idle_wakeup": False,
    }
