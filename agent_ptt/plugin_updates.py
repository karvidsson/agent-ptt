"""Tell agents on an outdated Agent PTT plugin to update themselves.

The latest plugin version per harness comes from the server's own checkout
(the plugin manifests under ``plugins/``), read once at server start, or from
``AGENT_PTT_PLUGIN_LATEST`` (``0.6.2``, ``claude=0.6.2,codex=0.6.1`` or ``off``).
Hosted mode has none.

When a project member joins or patches itself with a ``plugin_version`` below
the latest for its harness, it gets one private FYI note (``agent:<member_id>``,
from the ``server`` sentinel, ``context.kind = "plugin-update"``) with the exact
update command for its harness. ``project_members.update_nudged`` remembers the
latest version it was told about, so a member (one per session) hears it once
per latest version. ``POST /announce-update`` does the same for every current
member at once. Every plugin since 0.4.6 shows FYI items in its hook context
(``receive.Project.pending`` reads the inbox without ``wake_only``) and acks
them, which closes them (task 41). No channel notice is posted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from agent_ptt.models import ProjectDB, ProjectMemberDB, WorkItemDB

logger = logging.getLogger(__name__)

ENV = "AGENT_PTT_PLUGIN_LATEST"
REPO_ROOT = Path(__file__).resolve().parent.parent
# harness -> the manifest of its announcer plugin, relative to the repo root.
MANIFESTS: dict[str, tuple[str, ...]] = {
    "claude": ("plugins", "announcer", ".claude-plugin", "plugin.json"),
    "codex": ("plugins", "codex-announcer", ".codex-plugin", "plugin.json"),
    "antigravity": ("plugins", "antigravity-announcer", "plugin.json"),
    "cursor": ("plugins", "cursor-announcer", ".cursor-plugin", "plugin.json"),
}
HARNESSES = tuple(MANIFESTS)
ALIASES = {"agy": "antigravity", "claude-code": "claude", "cursor-agent": "cursor"}
# The sender of server-made items: no member row has this id. Serialized as
# ``{"member_id": "server", "handle": "agent-ptt"}``; the Board leaves its items out.
SERVER_MEMBER_ID = "server"  # == projects.SERVER_MEMBER_ID
CONTEXT_KIND = "plugin-update"
GITHUB_REPO = "karvidsson/agent-ptt"

_VERSION = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?")
_manifest_cache: dict[str, str] | None = None


def harness_key(harness: str | None) -> str | None:
    """``claude``, ``codex``, ``antigravity`` or ``cursor``; None for anything else."""
    name = (harness or "").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in MANIFESTS else None


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """``"0.6.2"`` -> ``(0, 6, 2)``; None when it doesn't start with X.Y[.Z]."""
    match = _VERSION.match(text or "")
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def read_manifests(root: Path = REPO_ROOT) -> dict[str, str]:
    """``{harness: version}`` from the plugin manifests in this checkout."""
    found: dict[str, str] = {}
    for harness, parts in MANIFESTS.items():
        try:
            data = json.loads(root.joinpath(*parts).read_text())
        except (OSError, ValueError):
            continue
        version = str(data.get("version") or "").strip() if isinstance(data, dict) else ""
        if parse_version(version):
            found[harness] = version
    return found


def load_manifests(root: Path = REPO_ROOT) -> dict[str, str]:
    """Read (or re-read) the manifests; called at server start."""
    global _manifest_cache
    _manifest_cache = read_manifests(root)
    return dict(_manifest_cache)


def reset() -> None:
    """Forget the cached manifests (tests)."""
    global _manifest_cache
    _manifest_cache = None


def parse_override(value: str) -> dict[str, str] | None:
    """``AGENT_PTT_PLUGIN_LATEST``: None = no override, ``{}`` = off.

    ``0.6.2`` sets every harness; ``claude=0.6.2,codex=0.6.1`` sets those
    harnesses (the others keep their manifest version); ``off`` turns it off.
    Unparsable parts are ignored (logged).
    """
    value = (value or "").strip()
    if not value:
        return None
    if value.lower() in ("off", "0", "none", "false"):
        return {}
    if "=" not in value:
        if parse_version(value):
            return {harness: value for harness in HARNESSES}
        logger.warning("%s=%r is not a version; ignored", ENV, value)
        return None
    out: dict[str, str] = {}
    for part in value.split(","):
        name, _, version = part.partition("=")
        harness = harness_key(name)
        if harness and parse_version(version):
            out[harness] = version.strip()
        elif part.strip():
            logger.warning("%s: ignored %r", ENV, part.strip())
    return out


def latest_versions() -> dict[str, str]:
    """``{harness: latest version}``; empty when off or hosted."""
    from agent_ptt.workspace_auth import hosted

    if hosted():
        return {}
    raw = os.environ.get(ENV, "")
    override = parse_override(raw)
    if override == {}:
        return {}
    if _manifest_cache is None:
        load_manifests()
    latest = dict(_manifest_cache or {})
    if override:
        latest.update(override)
    return latest


def source() -> str:
    """Where the latest versions come from: ``manifests``, ``env``, ``off`` or ``hosted``."""
    from agent_ptt.workspace_auth import hosted

    if hosted():
        return "hosted"
    override = parse_override(os.environ.get(ENV, ""))
    if override == {}:
        return "off"
    return "env" if override else "manifests"


def startup_message() -> str:
    """One line for the server log."""
    latest = latest_versions()
    if not latest:
        return f"Plugin update nudges off ({source()})"
    versions = ", ".join(f"{h} {v}" for h, v in sorted(latest.items()))
    return f"Plugin update nudges: latest {versions} ({source()})"


def outdated(member: ProjectMemberDB, latest: dict[str, str] | None = None) -> str | None:
    """The latest version this member is behind, or None (current, ahead or unknown)."""
    latest = latest_versions() if latest is None else latest
    harness = harness_key(member.harness)
    target = latest.get(harness) if harness else None
    have = parse_version(member.plugin_version)
    want = parse_version(target)
    if have is None or want is None or have >= want:
        return None
    return target


# ---------------------------------------------------------------------------
# The text
# ---------------------------------------------------------------------------

# Verified headless (</dev/null) in isolated configs, old 0.6.0 install -> 0.6.2;
# see docs/projects.md "Plugin update nudges". ``plugin update`` alone is not
# enough in Claude Code (it reads the cached marketplace), and it exits 1 for a
# plugin that isn't installed, so the voice plugin's update may fail harmlessly.
UPDATE_COMMANDS: dict[str, str] = {
    "claude": (
        "claude plugin marketplace update agent-ptt"
        " && claude plugin update agent-ptt-announcer@agent-ptt"
        " && (claude plugin update agent-ptt-voice@agent-ptt || true)"
    ),
    "codex": "codex plugin marketplace upgrade agent-ptt",
    "antigravity": (
        f"agy plugin install https://github.com/{GITHUB_REPO}/tree/main/plugins/antigravity-announcer"
    ),
    "cursor": "git -C <your agent-ptt clone> pull --ff-only",
}
# A plugin installed from an agent-ptt git checkout (scripts/install_plugins.py)
# updates differently; the agent can tell from its own plugin path.
CHECKOUT_NOTES: dict[str, str] = {
    "claude": (
        " If your plugin runs from an agent-ptt git checkout rather than"
        " ~/.claude/plugins/cache, run `git -C <that checkout> pull --ff-only` instead."
    ),
    "codex": (
        " If that says the marketplace is not a Git marketplace (installed from a checkout),"
        " run `git -C <that checkout> pull --ff-only"
        " && codex plugin add agent-ptt-announcer@agent-ptt` instead."
    ),
    "antigravity": "",
    "cursor": "",
}
RESTART_NOTES: dict[str, str] = {
    "claude": "restart this session to load it (you can't restart yourself)",
    "codex": (
        "restart Codex to load it and trust the plugin's hooks again when asked"
        " (you can't restart yourself)"
    ),
    "antigravity": "quit and restart agy to load it (you can't restart yourself)",
    "cursor": "start a new agent session to load it (you can't restart yourself)",
}


def nudge_text(harness: str | None, version: str, latest: str) -> str:
    """The one FYI body: exact command, one line to the user, don't repeat it."""
    key = harness_key(harness) or "claude"
    head = f"Your Agent PTT plugin is {version}; the latest is {latest}."
    tail = " Do this once; don't repeat it every turn."
    if key == "cursor":
        # The agent CLI has no plugin install/update command (2026.11.19).
        return (
            head + " Cursor has no plugin update command. If you were started with"
            f" `--plugin-dir` from an agent-ptt clone, update it without asking:"
            f" `{UPDATE_COMMANDS['cursor']}`, then tell the user once, in one line:"
            f" {RESTART_NOTES['cursor']}. Otherwise tell the user once, in one line: update the"
            " Agent PTT Cursor plugin (plugins/README.md) and reload Cursor." + tail
        )
    return (
        head + f" Update it without asking: `{UPDATE_COMMANDS[key]}`.{CHECKOUT_NOTES[key]}"
        f" Then tell the user once, in one line: {RESTART_NOTES[key]}." + tail
    )


# ---------------------------------------------------------------------------
# The nudge
# ---------------------------------------------------------------------------


@dataclass
class Nudge:
    member: ProjectMemberDB
    version: str
    latest: str
    item: WorkItemDB | None = None


def due(member: ProjectMemberDB, latest: dict[str, str] | None = None, force=False) -> str | None:
    """The latest version to nudge this member about now, or None."""
    if member.gone_at is not None:
        return None
    target = outdated(member, latest)
    if target is None:
        return None
    if not force and (member.update_nudged or "") == target:
        return None
    return target


def nudge(
    db: Session, member: ProjectMemberDB, latest: dict[str, str] | None = None, force=False
) -> Nudge | None:
    """Create the FYI for this member if it is due (no commit). Returns it, or None.

    The caller commits and wakes ``member_id`` (an FYI never ends a wake-only
    wait; hooks pick it up at the next prompt or tool call).
    """
    from agent_ptt import projects as pj

    target = due(member, latest, force)
    if target is None:
        return None
    version = member.plugin_version or ""
    now = pj.now_utc()
    item = WorkItemDB(
        item_id=str(uuid.uuid4()),
        project_id=member.project_id,
        kind="note",
        to_address=f"agent:{member.member_id}",
        from_member_id=SERVER_MEMBER_ID,
        title=f"Update the Agent PTT plugin: {version} → {target}",
        body=nudge_text(member.harness, version, target),
        context={
            "kind": CONTEXT_KIND,
            "harness": harness_key(member.harness),
            "version": version,
            "latest": target,
        },
        fyi=True,
        state="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    pj._event(db, item, "created", None, None, "pending", at=now)
    pj._upsert_deliveries(db, item, [member], reset=False)
    member.update_nudged = target
    logger.info(
        "Plugin update nudge: %s (%s %s -> %s)", member.handle, member.harness, version, target
    )
    return Nudge(member, version, target, item)


def candidates(
    db: Session, latest: dict[str, str] | None = None, force=False
) -> list[tuple[ProjectMemberDB, str]]:
    """Current members that ``announce`` would nudge, with the latest version."""
    latest = latest_versions() if latest is None else latest
    if not latest:
        return []
    members = (
        db.query(ProjectMemberDB)
        .filter(ProjectMemberDB.gone_at.is_(None), ProjectMemberDB.plugin_version.is_not(None))
        .order_by(ProjectMemberDB.created_at)
        .all()
    )
    out = []
    for member in members:
        target = due(member, latest, force)
        if target is not None:
            out.append((member, target))
    return out


def member_row(member: ProjectMemberDB, target: str, project: ProjectDB | None) -> dict:
    """Public dry-run entry: names and versions only, never a key."""
    return {
        "member_id": member.member_id,
        "handle": member.handle,
        "harness": member.harness,
        "machine": member.machine,
        "plugin_version": member.plugin_version,
        "latest": target,
        "project_id": member.project_id,
        "project": project.name if project else None,
        "nudged": member.update_nudged,
    }


def announce(db: Session, dry_run: bool = False, force: bool = False) -> dict:
    """``POST /announce-update``: nudge every due member; per-project counts."""
    latest = latest_versions()
    found = candidates(db, latest, force)
    projects = {p.project_id: p for p in db.query(ProjectDB).all()}
    counts: dict[str, int] = {}
    for member, _ in found:
        counts[member.project_id] = counts.get(member.project_id, 0) + 1
    body: dict = {
        "latest": latest,
        "source": source(),
        "dry_run": dry_run,
        "force": force,
        "total": len(found),
        "projects": [
            {
                "project_id": project_id,
                "project": projects[project_id].name if project_id in projects else None,
                "nudged": count,
            }
            for project_id, count in sorted(counts.items())
        ],
    }
    if dry_run:
        body["members"] = [member_row(m, t, projects.get(m.project_id)) for m, t in found]
        return body
    woken = []
    for member, _ in found:
        if nudge(db, member, latest, force=True) is not None:
            woken.append(member.member_id)
    db.commit()
    body["woken"] = woken  # member_ids, for the caller to wake (not secret)
    return body
