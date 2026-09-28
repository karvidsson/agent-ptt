"""Agent enrollment and channel selection with independent local profiles."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import typer

app = typer.Typer(help="Join an organization and work in its channels")
PROFILE_DIR = Path.home() / ".agent-ptt" / "workspaces"


@app.callback()
def profile(ctx: typer.Context, profile: str = typer.Option("default", "--profile")):
    """Use a separate profile for each agent identity."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
        raise typer.BadParameter(
            "Profile must contain 1-64 letters, numbers, underscores or hyphens"
        )
    ctx.obj = PROFILE_DIR / f"{profile}.json"


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        json.dump(data, out)
    os.replace(out.name, path)  # Temporary files are mode 0600, including on replacement.


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def request(config, method, path, payload=None, *, enrollment=False):
    headers = (
        {"Origin": config["url"]} if enrollment else {"Authorization": "Bearer " + config["token"]}
    )
    try:
        response = httpx.request(
            method,
            config["url"] + "/api/workspace" + path,
            json=payload,
            headers=headers,
            timeout=20,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise typer.BadParameter("Workspace unavailable; retry the same command") from None
    if not 200 <= response.status_code < 300:
        # Never echo a request body, URL fragment, or credential on failure.
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise typer.BadParameter(
            detail
            if isinstance(detail, str)
            else f"Workspace request failed ({response.status_code})"
        )
    return response.json() if response.status_code != 204 else None


def config(ctx):
    data = read(ctx.obj)
    if not data.get("organization"):
        raise typer.BadParameter("Enroll this profile in an organization first")
    return data


def org_path(data):
    return "/organizations/" + data["organization"]


def room_path(data):
    if not data.get("channel"):
        raise typer.BadParameter("Join a channel first")
    return org_path(data) + "/channels/" + data["channel"]


@app.command()
def enroll(ctx: typer.Context, link: str, name: str = typer.Option(..., "--name")):
    """Join using the organization's general link; retries keep the same agent identity."""
    parsed = urlsplit(link)
    token = parse_qs(parsed.fragment).get("join", [""])[0]
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.path != "/workspace/"
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise typer.BadParameter("Use an HTTPS organization join link (HTTP only on localhost)")
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise typer.BadParameter("Agent name must contain 1-80 characters")
    url = f"{parsed.scheme}://{parsed.netloc}"
    fingerprint = hashlib.sha256(link.encode()).hexdigest()
    with locked(ctx.obj):
        data = read(ctx.obj)
        if data and (data.get("url") != url or data.get("name") != name):
            raise typer.BadParameter(
                "This profile belongs to another agent; use a different --profile"
            )
        if data and data.get("enrollment") != fingerprint:
            raise typer.BadParameter("Retry with the original link or use a different --profile")
        if data.get("organization"):
            me = request(data, "GET", "/me")
            typer.echo(
                f"Already registered as {me['name']} ({me['id']}). Use channels and join next."
            )
            return
        if not data:
            data = {
                "url": url,
                "name": name,
                "token": secrets.token_urlsafe(32),
                "enrollment": fingerprint,
            }
            save(ctx.obj, data)  # Save before HTTP, so even a lost response is recoverable.
        result = request(
            data,
            "POST",
            "/join/agent",
            {"token": token, "name": name, "credential": data["token"]},
            enrollment=True,
        )
        data.update(organization=result["org_id"], agent_id=result["id"])
        save(ctx.obj, data)
    typer.echo(
        f"Registered {name} in organization {data['organization']}. "
        "List channels, then join one or create one for your work."
    )


@app.command()
def channels(ctx: typer.Context):
    """List organization channels to find one that fits the current work."""
    data = config(ctx)
    typer.echo(json.dumps(request(data, "GET", org_path(data) + "/channels"), indent=2))


@app.command()
def join(ctx: typer.Context, channel: str, create: bool = typer.Option(False, "--create")):
    """Join by channel name or ID; --create creates that name only if missing."""
    with locked(ctx.obj):
        data = config(ctx)
        path = org_path(data) + "/channels"
        rooms = request(data, "GET", path)
        room = next((r for r in rooms if channel in (r["id"], r["name"])), None)
        if room is None and create:
            try:
                room = request(data, "POST", path, {"name": channel})
            except typer.BadParameter:
                # A competing agent may have just created the same channel.
                room = next((r for r in request(data, "GET", path) if r["name"] == channel), None)
                if room is None:
                    raise
        if room is None:
            raise typer.BadParameter(
                "Channel not found. List channels or use --create when none fits"
            )
        data["channel"] = room["id"]
        save(ctx.obj, data)
    typer.echo(f"Joined #{room['name']} ({room['id']}).")


@app.command()
def inbox(ctx: typer.Context):
    """Read pending mentions in the selected channel; replies mark them answered."""
    data = config(ctx)
    typer.echo(json.dumps(request(data, "GET", room_path(data) + "/inbox"), indent=2))


@app.command()
def say(ctx: typer.Context, text: str, reply_to: int | None = typer.Option(None, "--reply-to")):
    """Send a message or reply to a mention in the selected channel."""
    data = config(ctx)
    payload = {"text": text, "client_id": str(uuid.uuid4()), "reply_to": reply_to}
    result = request(data, "POST", room_path(data) + "/messages", payload)
    typer.echo(f"Sent message {result['id']}.")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(ctx: typer.Context):
    """Run a CLI with this agent's configuration, e.g. run -- claude."""
    data = config(ctx)
    room_path(data)
    if not ctx.args:
        raise typer.BadParameter("Specify a command after --")
    env = os.environ.copy()
    env.update(
        AGENT_PTT_WORKSPACE_PROFILE=str(ctx.obj.resolve()),
        AGENT_PTT_MODE="workspace",
        AGENT_PTT_URL=data["url"],
        AGENT_PTT_WORKSPACE_ORG=data["organization"],
        AGENT_PTT_WORKSPACE_CHANNEL=data["channel"],
        AGENT_PTT_WORKSPACE_TOKEN=data["token"],
        AGENT_PTT_RECEIVE="1",
    )
    raise typer.Exit(subprocess.call(ctx.args, env=env))
