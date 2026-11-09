"""Project, inbox and work-item commands: agents handing work to each other.

Talks to the `/projects` and `/work-items` routes of the legacy server (see
docs/cli-reference.md, "Projects and work items"). Member state lives in
`~/.agent-ptt/members.json`, keyed by `"<base_url>|<git toplevel or cwd>"`, so
two agents in different worktrees of one repo are different members.
`AGENT_PTT_MEMBER_KEY` / `AGENT_PTT_PROJECT_ID` override the stored values.

The `member_key` is a bearer secret: it is sent as `X-Member-Key` and stored
in the 0600 state file, and never printed — not even with `--json`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx
import typer

MEMBERS_FILE = Path.home() / ".agent-ptt" / "members.json"
MIN_PREFIX = 6
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SECRET_FIELDS = ("member_key", "key_id")

project_app = typer.Typer(help="Join a project and see its members")

_JSON = typer.Option(False, "--json", help="Print the raw response as JSON")
_NOTE = typer.Option(None, "--note", help="A short note recorded with the change")
_ID = typer.Argument(help="Item ID or prefix")


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _cli():
    from agent_ptt import cli  # lazy: cli.py imports this module to register it

    return cli


def _base_url() -> str:
    return _cli()._get_base_url()


def _fail(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _redact(data):
    if isinstance(data, dict):
        return {k: _redact(v) for k, v in data.items() if k not in _SECRET_FIELDS}
    if isinstance(data, list):
        return [_redact(v) for v in data]
    return data


def _print_json(data) -> None:
    typer.echo(json.dumps(_redact(data), indent=2, default=str))


def _git(*args: str) -> str | None:
    """Run git in the cwd; stripped stdout, or None outside git / on failure."""
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def _workdir() -> str:
    return _git("rev-parse", "--show-toplevel") or str(Path.cwd())


def _detect_harness() -> str | None:
    """`claude` under Claude Code, `codex` under Codex, else None."""
    if os.environ.get("CLAUDECODE") == "1":
        return "claude"
    if any(name.startswith("CODEX_") for name in os.environ):
        return "codex"
    return None


def _read_state() -> dict:
    try:
        return json.loads(MEMBERS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _write_state(data: dict) -> None:
    MEMBERS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=MEMBERS_FILE.parent, prefix=".members-", delete=False
    ) as out:
        json.dump(data, out, indent=2)
    os.chmod(out.name, 0o600)
    os.replace(out.name, MEMBERS_FILE)


def _state_key(base: str) -> str:
    return f"{base}|{_workdir()}"


def _member(*, required: bool = True) -> dict:
    """The member for this server + directory, with env overrides applied.

    Returns a dict with at least `project_id` and `member_key` (when required),
    plus `_key`: the members.json key that holds this member's cursor.
    """
    base = _base_url()
    key = _state_key(base)
    entry = dict(_read_state().get(key, {}))
    env_key = os.environ.get("AGENT_PTT_MEMBER_KEY", "").strip()
    env_project = os.environ.get("AGENT_PTT_PROJECT_ID", "").strip()
    if env_key or env_project:
        if env_key and env_key != entry.get("member_key"):
            # A different identity: keep its cursor apart from the stored member.
            digest = hashlib.sha256(env_key.encode()).hexdigest()[:16]
            key = f"{base}|env:{digest}"
            entry = {"cursor": _read_state().get(key, {}).get("cursor", 0)}
            entry["member_key"] = env_key
        if env_project:
            entry["project_id"] = env_project
    entry["_key"] = key
    if required and not (entry.get("member_key") and entry.get("project_id")):
        _fail("not joined to a project here — run `agent-ptt project join` first")
    return entry


def _update_state(key: str, **fields) -> None:
    state = _read_state()
    state.setdefault(key, {}).update(fields)
    _write_state(state)


def _detail(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error")
        if isinstance(detail, list):  # FastAPI validation errors
            msgs = (d.get("msg", d) if isinstance(d, dict) else d for d in detail)
            return "; ".join(str(m) for m in msgs)
        if detail:
            return str(detail)
    return resp.text or f"HTTP {resp.status_code}"


def _request(
    method: str,
    path: str,
    *,
    member: dict | None = None,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
    retry: bool = False,
    handled: tuple[int, ...] = (),
):
    """Call the server; turn transport and HTTP errors into clear messages.

    Statuses in `handled` are returned to the caller instead of failing.
    `retry` repeats the request once on a network error (safe for sends that
    carry a `client_id`).
    """
    base = _base_url()
    headers = dict(_cli()._auth_headers())
    if member is not None:
        headers["X-Member-Key"] = member["member_key"]
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        try:
            resp = httpx.request(
                method,
                f"{base}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=timeout,
            )
            break
        except httpx.TransportError as exc:
            if attempt + 1 < attempts:
                continue
            _fail(f"cannot reach the server at {base} ({type(exc).__name__}: {exc})")
    if resp.status_code < 400 or resp.status_code in handled:
        return resp
    detail = _detail(resp)
    if resp.status_code == 401:
        hint = (
            "the member key was not recognized — run `agent-ptt project join` again"
            if member is not None and "member" in detail.lower()
            else "check AGENT_PTT_API_KEY or rejoin with `agent-ptt project join`"
        )
        _fail(f"unauthorized: {detail} ({hint})")
    if resp.status_code == 403:
        _fail(f"forbidden: {detail}")
    if resp.status_code == 404:
        _fail(f"not found: {detail}")
    _fail(f"{detail} (HTTP {resp.status_code})")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _short(item_id: str | None) -> str:
    return (item_id or "")[:8]


def _handle(person: dict | None) -> str:
    return (person or {}).get("handle") or "?"


def _item_line(item: dict) -> str:
    parts = [
        _short(item.get("item_id")),
        item.get("kind", "?"),
        f"{_handle(item.get('from'))} → {item.get('to', '?')}",
        f"[{item.get('state', '?')}]",
        item.get("title") or "",
    ]
    line = "  ".join(parts)
    if item.get("assignee") and item.get("state") in ("claimed", "waiting", "review"):
        line += f"  (by {_handle(item['assignee'])})"
    if item.get("open_questions"):
        line += f"  ?{item['open_questions']}"
    if item.get("reply_to"):
        line += f"  ↳ {_short(item['reply_to'])}"
    return line


def _context_line(item: dict) -> str | None:
    ctx = item.get("context") or {}
    bits = [ctx.get(k) for k in ("summary", "branch") if ctx.get(k)]
    if ctx.get("resource"):
        bits.append(f"resource {ctx['resource']}")
    return "    " + " · ".join(str(b) for b in bits) if bits else None


def _print_item(item: dict, *, body: bool = False, marker: str = "") -> None:
    typer.echo(marker + _item_line(item))
    extra = _context_line(item)
    if extra:
        typer.echo(extra)
    if body and item.get("body") and item.get("body") != item.get("title"):
        for text in item["body"].splitlines():
            typer.echo(f"    {text}")


def _time(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("T", " ")[:16]


# ---------------------------------------------------------------------------
# Item-ID resolution
# ---------------------------------------------------------------------------


def _resolve(prefix: str, member: dict) -> str:
    """A full item UUID from a full id or a unique prefix of ≥ 6 chars."""
    prefix = prefix.strip()
    if _UUID_RE.match(prefix):
        return prefix.lower()
    if len(prefix) < MIN_PREFIX:
        _fail(f"item id prefix '{prefix}' is too short — give at least {MIN_PREFIX} characters")
    needle = prefix.lower()
    project_id = member["project_id"]
    found: dict[str, dict] = {}
    resp = _request("GET", "/work-items", params={"project_id": project_id})
    for item in resp.json().get("items", []):
        found[item["item_id"]] = item
    matches = [i for i in found if i.lower().startswith(needle)]
    if not matches:
        # Older done items drop out of /work-items; the inbox may still hold it.
        resp = _request("GET", f"/projects/{project_id}/inbox", member=member)
        for item in resp.json().get("items", []):
            found[item["item_id"]] = item
        matches = [i for i in found if i.lower().startswith(needle)]
    if not matches:
        _fail(f"no item matches '{prefix}' in this project")
    if len(matches) > 1:
        listing = "\n".join("  " + _item_line(found[m]) for m in sorted(matches))
        _fail(f"'{prefix}' is ambiguous — it matches {len(matches)} items:\n{listing}")
    return matches[0]


# ---------------------------------------------------------------------------
# project join / list / show
# ---------------------------------------------------------------------------


@project_app.command("join")
def project_join(
    target: str = typer.Argument(
        None, help="Project ID or name (default: this repo's git remote, else the folder name)"
    ),
    handle: str = typer.Option(None, "--handle", "-n", help="Your handle in the project"),
    roles: list[str] = typer.Option(None, "--role", "-r", help="A role you take (repeatable)"),
    mode: str = typer.Option(None, "--mode", help="monitor (default) or hooks"),
    harness: str = typer.Option(
        None, "--harness", help="claude, codex, script, … (default: detected from the env)"
    ),
    as_json: bool = _JSON,
):
    """Join (or rejoin) the project for this repo and remember the membership."""
    base = _base_url()
    key = _state_key(base)
    stored = _read_state().get(key, {})
    handle = handle or stored.get("handle")
    if not handle:
        _fail("--handle is required on the first join")
    if mode and mode not in ("monitor", "hooks"):
        _fail("--mode must be 'monitor' or 'hooks'")

    payload: dict = {"handle": handle}
    if target:
        payload["project_id" if _UUID_RE.match(target) else "name"] = target
    else:
        remote = _git("remote", "get-url", "origin")
        if remote:
            payload["remote"] = remote
        else:
            payload["name"] = Path(_workdir()).name
    if roles:
        payload["roles"] = roles
    if mode:
        payload["mode"] = mode
    harness = harness or _detect_harness()
    if harness:
        payload["harness"] = harness

    member_key = os.environ.get("AGENT_PTT_MEMBER_KEY", "").strip() or stored.get("member_key")
    resp = None
    if member_key:
        resp = _request(
            "POST",
            "/projects/join",
            json_body={**payload, "member_key": member_key},
            handled=(401, 403),
        )
        if resp.status_code in (401, 403):
            typer.echo("Stored membership was not accepted; joining as a new member.", err=True)
            resp = None
    if resp is None:
        resp = _request("POST", "/projects/join", json_body=payload)
    data = resp.json()
    project, member = data.get("project", {}), data.get("member", {})
    entry = {
        "project_id": project.get("project_id"),
        "project_name": project.get("name"),
        "channel_id": project.get("channel_id"),
        "member_id": member.get("member_id"),
        "member_key": data.get("member_key") or member_key,
        "handle": member.get("handle", handle),
    }
    if (
        stored.get("project_id") == entry["project_id"]
        and stored.get("member_id") == entry["member_id"]
    ):
        entry["cursor"] = stored.get("cursor", 0)
    state = _read_state()
    state[key] = entry
    _write_state(state)

    if as_json:
        _print_json(data)
        return
    roles_text = ", ".join(member.get("roles") or []) or "none"
    typer.echo(f"Joined project {project.get('name')} as {entry['handle']}")
    typer.echo(f"  project: {project.get('project_id')}")
    typer.echo(f"  member:  {entry['member_id']}  roles: {roles_text}  mode: {member.get('mode')}")
    typer.echo(f"  channel: {project.get('channel_id')}")


@project_app.command("list")
def project_list(as_json: bool = _JSON):
    """List the server's projects."""
    data = _request("GET", "/projects").json()
    if as_json:
        _print_json(data)
        return
    projects = data.get("projects", [])
    if not projects:
        typer.echo("No projects yet. Run `agent-ptt project join` in a repo.")
        return
    for p in projects:
        typer.echo(
            f"{_short(p.get('project_id'))}  {p.get('name')}  "
            f"members {p.get('member_count', 0)}  open {p.get('open_items', 0)}  "
            f"{p.get('remote') or ''}".rstrip()
        )


@project_app.command("show")
def project_show(
    project_id: str = typer.Argument(None, help="Project ID (default: the joined project)"),
    as_json: bool = _JSON,
):
    """Show a project's members, roles and resource claims."""
    if not project_id:
        project_id = _member()["project_id"]
    data = _request("GET", f"/projects/{project_id}").json()
    if as_json:
        _print_json(data)
        return
    typer.echo(f"{data.get('name')}  ({data.get('project_id')})")
    if data.get("remote"):
        typer.echo(f"  remote:  {data['remote']}")
    typer.echo(f"  channel: {data.get('channel_id')}")
    members = data.get("members", [])
    typer.echo(f"Members ({len(members)}):")
    for m in members:
        roles = ",".join(m.get("roles") or []) or "-"
        status = "listening" if m.get("listening") else f"seen {_time(m.get('last_seen'))}"
        typer.echo(
            f"  {m.get('handle')}  roles {roles}  {m.get('mode')}  "
            f"{m.get('harness') or '-'}  {status}  ({_short(m.get('member_id'))})"
        )
    claims = data.get("resource_claims", [])
    if claims:
        typer.echo("Resource claims:")
        for c in claims:
            resource = (c.get("context") or {}).get("resource", "?")
            typer.echo(f"  {resource}  by {_handle(c.get('from'))}  {_short(c.get('item_id'))}")


# ---------------------------------------------------------------------------
# inbox / send / ask / answer
# ---------------------------------------------------------------------------


def inbox(
    wait: int = typer.Option(0, "--wait", "-w", min=0, max=60, help="Long-poll up to N seconds"),
    after: int = typer.Option(None, "--after", help="Cursor (default: the stored one)"),
    as_json: bool = _JSON,
):
    """Show unacked work items addressed to you; --wait long-polls for new ones."""
    member = _member()
    cursor = member.get("cursor", 0) if after is None else after
    params = {"wait": wait, "after": cursor}
    resp = _request(
        "GET",
        f"/projects/{member['project_id']}/inbox",
        member=member,
        params=params,
        timeout=wait + 10,
    )
    data = resp.json()
    if data.get("cursor") is not None:
        _update_state(member["_key"], cursor=data["cursor"])
    if as_json:
        _print_json(data)
        return
    items = data.get("items", [])
    if not items:
        typer.echo("Inbox empty.")
        return
    for item in items:
        _print_item(item)


def _send(
    member: dict,
    *,
    body: str,
    kind: str,
    to: str | None = None,
    title: str | None = None,
    reply_to: str | None = None,
    context: dict | None = None,
    client_id: str | None = None,
) -> dict:
    payload: dict = {"kind": kind, "body": body, "client_id": client_id or str(uuid.uuid4())}
    if to:
        payload["to"] = to
    if title:
        payload["title"] = title
    if reply_to:
        payload["reply_to"] = _resolve(reply_to, member)
    if context:
        payload["context"] = context
    resp = _request(
        "POST",
        f"/projects/{member['project_id']}/items",
        member=member,
        json_body=payload,
        retry=True,
    )
    return resp.json()


def _report_sent(item: dict, as_json: bool) -> None:
    if as_json:
        _print_json(item)
        return
    typer.echo("Sent " + _item_line(item))


def send(
    body: str = typer.Argument(help="The message body (the title defaults to its first line)"),
    kind: str = typer.Option(
        ..., "--kind", "-k", help="instruction, handoff, question, answer, claim, release, note"
    ),
    to: str = typer.Option(None, "--to", help="agent:<id or handle>, role:<role>, or project"),
    title: str = typer.Option(None, "--title", help="Title (default: first line of the body)"),
    reply_to: str = typer.Option(None, "--reply-to", help="Item ID or prefix this replies to"),
    repo: str = typer.Option(None, "--repo", help="Handoff repo (default: git remote)"),
    branch: str = typer.Option(None, "--branch", help="Handoff branch (default: current)"),
    commit: str = typer.Option(None, "--commit", help="Handoff commit (default: HEAD)"),
    files: str = typer.Option(None, "--files", help="Comma-separated file paths"),
    summary: str = typer.Option(None, "--summary", help="One-line summary for the context"),
    links: list[str] = typer.Option(None, "--link", help="A related URL (repeatable)"),
    resource: str = typer.Option(None, "--resource", help="Resource for claim, e.g. branch:x"),
    client_id: str = typer.Option(None, "--client-id", help="Idempotency key (default: random)"),
    as_json: bool = _JSON,
):
    """Send a work item to an agent, a role or the whole project."""
    member = _member()
    context: dict = {}
    if kind == "handoff":
        repo = repo or _git("remote", "get-url", "origin") or Path(_workdir()).name
        branch = branch or _git("rev-parse", "--abbrev-ref", "HEAD")
        commit = commit or _git("rev-parse", "HEAD")
    for name, value in (("repo", repo), ("branch", branch), ("commit", commit)):
        if value:
            context[name] = value
    if files:
        context["files"] = [f.strip() for f in files.split(",") if f.strip()]
    if summary:
        context["summary"] = summary
    if links:
        context["links"] = list(links)
    if resource:
        context["resource"] = resource
    item = _send(
        member,
        body=body,
        kind=kind,
        to=to,
        title=title,
        reply_to=reply_to,
        context=context or None,
        client_id=client_id,
    )
    _report_sent(item, as_json)


def ask(
    item_id: str = typer.Argument(help="Item ID or prefix the question is about"),
    question: str = typer.Argument(help="The question"),
    to: str = typer.Option(None, "--to", help="Address (default: the item's sender)"),
    as_json: bool = _JSON,
):
    """Ask a question about an item; a claimed item waits until it's answered."""
    member = _member()
    _report_sent(_send(member, body=question, kind="question", to=to, reply_to=item_id), as_json)


def answer(
    item_id: str = typer.Argument(help="Question ID or prefix"),
    text: str = typer.Argument(help="The answer"),
    to: str = typer.Option(None, "--to", help="Address (default: the question's sender)"),
    as_json: bool = _JSON,
):
    """Answer a question; the item it was about resumes."""
    member = _member()
    _report_sent(_send(member, body=text, kind="answer", to=to, reply_to=item_id), as_json)


# ---------------------------------------------------------------------------
# State changes
# ---------------------------------------------------------------------------

_VERBS = {
    "ack": "Acked",
    "claim": "Claimed",
    "release": "Released",
    "done": "Done",
    "decline": "Declined",
    "review": "In review",
    "resume": "Resumed",
}


def _change(verb: str, item_id: str, note: str | None, as_json: bool) -> None:
    member = _member()
    full_id = _resolve(item_id, member)
    base = f"/projects/{member['project_id']}/items/{full_id}"
    payload: dict | None = None
    if verb in ("review", "resume"):
        path = f"{base}/state"
        payload = {"state": "review" if verb == "review" else "claimed"}
    else:
        path = f"{base}/{verb}"
        if verb != "ack" and verb != "claim":
            payload = {}
    if note and payload is not None:
        payload["note"] = note
    resp = _request("POST", path, member=member, json_body=payload, handled=(409,))
    if resp.status_code == 409:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        assignee = body.get("assignee") if isinstance(body, dict) else None
        if verb == "claim" and assignee:
            _fail(f"already claimed by {_handle(assignee)}")
        _fail(_detail(resp))
    item = resp.json()
    if as_json:
        _print_json(item)
        return
    typer.echo(f"{_VERBS[verb]}: " + _item_line(item))


def ack(item_id: str = typer.Argument(help="Item ID or prefix"), as_json: bool = _JSON):
    """Acknowledge an item (you've seen it; it leaves your inbox)."""
    _change("ack", item_id, None, as_json)


def claim(item_id: str = typer.Argument(help="Item ID or prefix"), as_json: bool = _JSON):
    """Claim an item: it's yours for a 30-minute lease, renewed while you work."""
    _change("claim", item_id, None, as_json)


def release(item_id: str = _ID, note: str = _NOTE, as_json: bool = _JSON):
    """Give a claimed item back to the open pool."""
    _change("release", item_id, note, as_json)


def done(item_id: str = _ID, note: str = _NOTE, as_json: bool = _JSON):
    """Mark an item finished."""
    _change("done", item_id, note, as_json)


def decline(item_id: str = _ID, note: str = _NOTE, as_json: bool = _JSON):
    """Decline an item (final)."""
    _change("decline", item_id, note, as_json)


def review(item_id: str = _ID, note: str = _NOTE, as_json: bool = _JSON):
    """Put a claimed item in review."""
    _change("review", item_id, note, as_json)


def resume(item_id: str = _ID, note: str = _NOTE, as_json: bool = _JSON):
    """Move a waiting or in-review item back to claimed."""
    _change("resume", item_id, note, as_json)


# ---------------------------------------------------------------------------
# item: thread and events
# ---------------------------------------------------------------------------


def item(item_id: str = typer.Argument(help="Item ID or prefix"), as_json: bool = _JSON):
    """Show an item's thread (what it replies to and its replies) and its events."""
    member = _member()
    full_id = _resolve(item_id, member)
    data = _request("GET", f"/projects/{member['project_id']}/items/{full_id}").json()
    if as_json:
        _print_json(data)
        return
    main = data.get("item", {})
    thread = data.get("thread") or [main]
    typer.echo("Thread:")
    for entry in thread:
        current = entry.get("item_id") == main.get("item_id")
        _print_item(entry, body=True, marker="▶ " if current else "  ")
    events = data.get("events", [])
    if events:
        typer.echo("Events:")
    for ev in events:
        by = _handle(ev.get("by")) if ev.get("by") else "server"
        change = ""
        if ev.get("from_state") or ev.get("to_state"):
            change = f"  ({ev.get('from_state') or '-'} → {ev.get('to_state') or '-'})"
        note = f"  — {ev['note']}" if ev.get("note") else ""
        typer.echo(f"  {_time(ev.get('at'))}  {ev.get('action')}  by {by}{change}{note}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_TOP_LEVEL = (inbox, send, ack, claim, release, done, decline, review, resume, ask, answer, item)


def register(app: typer.Typer) -> None:
    """Add `project …` and the top-level work-item commands to the CLI."""
    app.add_typer(project_app, name="project")
    for command in _TOP_LEVEL:
        app.command(command.__name__)(command)
