#!/usr/bin/env python3
"""Private Agent PTT team launcher. Requires Python 3; keep this file out of Git.

List agents: python3 agent-ptt-team.py --list
Check access: python3 agent-ptt-team.py agent-1 --check
Launch: python3 agent-ptt-team.py agent-1 -- codex
Herdr: use one such launch command per configured agent.
Install the matching Agent PTT hooks before launching Claude Code or Codex.
Other CLIs need a compatible integration that reads these environment variables.
"""

import argparse
import json
import os
import sys
import urllib.request
from urllib.parse import urlsplit

BUNDLE = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def environment(bundle, agent):
    origin = bundle["url"]
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
        or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ValueError("Use an HTTPS server origin (HTTP only for localhost)")
    return {
        "AGENT_PTT_MODE": "workspace",
        "AGENT_PTT_URL": origin,
        "AGENT_PTT_WORKSPACE_ORG": bundle["organization"],
        "AGENT_PTT_WORKSPACE_CHANNEL": bundle["channel"],
        "AGENT_PTT_WORKSPACE_TOKEN": agent["token"],
    }


def check(bundle, agent):
    env = environment(bundle, agent)
    opener = urllib.request.build_opener(NoRedirect)
    headers = {"Authorization": "Bearer " + env["AGENT_PTT_WORKSPACE_TOKEN"]}
    with opener.open(
        urllib.request.Request(bundle["url"] + "/api/workspace/me", headers=headers), timeout=10
    ) as response:
        identity = json.load(response)
    if (
        identity.get("kind") != "agent"
        or identity.get("id") != agent["id"]
        or bundle["organization"] not in {o["id"] for o in identity["organizations"]}
    ):
        raise ValueError("The credential does not match this agent and organization")
    path = (
        f"/api/workspace/organizations/{bundle['organization']}"
        f"/channels/{bundle['channel']}/messages?limit=1"
    )
    with opener.open(urllib.request.Request(bundle["url"] + path, headers=headers), timeout=10):
        pass
    print(f"Connected: {agent['name']}. Credential and channel access verified.")


def main(bundle=None):
    bundle = BUNDLE if bundle is None else bundle
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument("agent", nargs="?")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not bundle:
        parser.error("Download your personal launcher after accepting an invitation")
    if args.list:
        for agent in bundle["agents"]:
            print(f"{agent['key']}: {agent['name']} ({agent['harness']})")
        return
    agent = next((a for a in bundle["agents"] if a["key"] == args.agent), None)
    if not agent:
        parser.error("Choose an agent key from --list")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        if command == ["--check"]:
            check(bundle, agent)
            return
        if not command:
            command = [agent["harness"]] if agent["harness"] != "custom" else []
        if not command:
            parser.error("Supply a CLI command after --")
        env = os.environ.copy()
        env.update(environment(bundle, agent))
        os.execvpe(command[0], command, env)
    except Exception as exc:
        # Never print credential-bearing requests, environments, or bundle data.
        print(
            f"Setup failed ({type(exc).__name__}). Check connectivity, credentials, "
            "and that the CLI is installed.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
