#!/usr/bin/env python3
"""ptt: talk to the other agents in this session's Agent PTT channel and project.

Stdlib only; runs on macOS, Linux and Windows next to the plugin's hooks. It
finds this session's channel key and project membership in the hook state under
``~/.agent-ptt`` (written by session_start.py and receive.py), so no keys or IDs
need to be pasted. The session is ``--session``, else ``AGENT_PTT_SESSION_ID`` /
``CODEX_THREAD_ID`` / ``CLAUDE_SESSION_ID``, else the most recently active
hooked session whose folder contains (or is inside) the current directory.

  ptt.py say "<text with @Theo>" [--quiet] [--fyi]   speak (--quiet: text only) in the channel
  ptt.py who                                 who is here: harness, machine, doing, presence
  ptt.py inbox                               pending mentions and work items (not acked)
  ptt.py wait [--once]                       print one line per waking mention/work item
  ptt.py tell <to> "<text>" [--kind K] [--fyi]   send a work item: Handle, role:<r> or project

--fyi marks status, acknowledgements and "done": delivered at the recipient's next
hook, never waking an idle agent.
  ptt.py ack <id> | done <id> [--note N]     acknowledge / finish a mention or work item
  ptt.py codex-rules                         print a Codex rule so ptt can reach the server
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import signal
import sys
import threading
import time
import urllib.error
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "project_link", Path(__file__).with_name("project_link.py")
)
link = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(link)

WAIT_SECONDS = 30
MIN_CYCLE = 10.0  # never poll faster than this when a poll returns nothing new
MAX_BACKOFF = 60.0
LOCK_STALE = 120.0
KINDS = ("instruction", "question", "answer", "note", "handoff", "claim", "release")


class Failure(Exception):
    pass


class Session:
    """This session's channel key and project membership, re-registered on demand."""

    def __init__(self, session_id: str | None = None, cwd: str | None = None):
        self.base = link.base_url()
        self.session_id = session_id or link.find_session(self.base, cwd)
        if not self.session_id:
            raise Failure(
                "No Agent PTT session found for this folder. The plugin's SessionStart hook "
                "creates one; pass --session <id> (the command in your session context has it)."
            )
        self.pointer = link.load_session(self.base, self.session_id)
        if not self.pointer:
            raise Failure(f"Session {self.session_id} has no Agent PTT state on this machine.")
        self.agent = self.pointer.get("agent") or "Claude"
        self.channel_path = link.channel_state_path(self.base, self.agent, self.session_id)
        self.member_path, self.member = link.load_membership(self.base, self.session_id)
        self.lock = threading.Lock()

    # --- channel ---------------------------------------------------------

    def channel(self) -> dict:
        state = link._read(self.channel_path)
        if not state.get("channel_id") or not state.get("key_id"):
            state = self.reconnect(state)
        return state

    def reconnect(self, state: dict | None = None) -> dict:
        with self.lock:
            state = state or link._read(self.channel_path)
            name = state.get("name")
            if not name:
                raise Failure("This session has no channel yet; send a prompt first.")
            fresh = link.connect_channel(self.base, name, self.agent, self.session_id)
            fresh.pop("handle", None)
            fresh.pop("mention_policy", None)
            link.update_channel_state(self.channel_path, **fresh)
            return link._read(self.channel_path)

    def channel_call(self, method: str, path: str, payload=None, timeout=link.HTTP_TIMEOUT):
        """A channel request with the participation key; re-registers once on 403/404."""
        state = self.channel()
        for attempt in (0, 1):
            try:
                return state, link.request(
                    self.base,
                    method,
                    path.format(cid=state["channel_id"]),
                    payload(state) if callable(payload) else payload,
                    participant_key=state["key_id"],
                    timeout=timeout,
                )
            except urllib.error.HTTPError as exc:
                if exc.code not in (403, 404) or attempt:
                    raise
                state = self.reconnect(state)
        raise AssertionError("unreachable")

    # --- project ---------------------------------------------------------

    def has_project(self) -> bool:
        return bool(self.member.get("member_key") and self.member.get("project_id"))

    def project_call(self, method: str, path: str, payload=None, timeout=link.HTTP_TIMEOUT):
        if not self.has_project():
            raise Failure("This session is not in a project (the server may not support them).")
        for attempt in (0, 1):
            try:
                return link.request(
                    self.base,
                    method,
                    path.format(pid=self.member["project_id"]),
                    payload,
                    member_key=self.member["member_key"],
                    timeout=timeout,
                )
            except urllib.error.HTTPError as exc:
                if exc.code not in (401, 403, 404) or attempt or not self.member.get("join"):
                    raise
                self.rejoin()
        raise AssertionError("unreachable")

    def rejoin(self) -> None:
        with self.lock:
            link.join_project(self.base, self.member)
            if self.member_path:
                link.save_membership(self.member_path, self.member)


# --- commands ---------------------------------------------------------------


def cmd_say(session: Session, args) -> None:
    def payload(state):
        body = {"key_id": state["key_id"], "text": args.text}
        if args.quiet:
            body["speak"] = False  # text only; servers without the flag speak it
        if args.fyi:
            body["fyi"] = True  # mentions are delivered but wake no one
        return body

    try:
        _, message = session.channel_call("POST", "/channels/{cid}/say", payload)
    except urllib.error.HTTPError as exc:
        raise Failure(f"say failed: {_detail(exc)}") from None
    message = message or {}
    note = " as FYI" if args.fyi else ""
    print(f"[ptt] said{note} (message {message.get('message_id', '?')})")
    if args.fyi and not session.channel().get("fyi_supported"):
        print("[ptt] note: this server has no FYI support; mentions in it may wake people")
    for queued in message.get("queued_items") or []:
        to = queued.get("to_label") or queued.get("to")
        print(f"[ptt] queued for {to} (item {queued.get('item_id')})")


def cmd_who(session: Session, args) -> None:
    people = link.fetch_people(
        session.base, session.member or {}, session.channel().get("channel_id")
    )
    me = session.member.get("handle") or "this session"
    where = session.member.get("project_name") or session.channel().get("name")
    print(f"You are {link.mention_text(me)} in {where}.")
    if not people:
        print("Nobody else is here yet.")
    for person in people:
        print(link.person_line(person))


def _pending(session: Session) -> tuple[list[dict], list[dict]]:
    mentions, items = [], []
    try:
        _, mentions = session.channel_call("GET", "/channels/{cid}/inbox?limit=20")
    except (urllib.error.HTTPError, Failure):
        mentions = []
    if session.has_project():
        items = session.project_call("GET", "/projects/{pid}/inbox?limit=50").get("items") or []
    return mentions or [], items


def cmd_inbox(session: Session, args) -> None:
    mentions, items = _pending(session)
    if not mentions and not items:
        print("[ptt] inbox empty")
    for message in mentions:
        print(link.mention_line(message))
    for item in items:
        print(link.item_line(item))
        body = (item.get("body") or "").strip()
        if body and body != item.get("title"):
            for line in body.splitlines()[:20]:
                print("    " + line)


def cmd_tell(session: Session, args) -> None:
    payload = {"kind": args.kind, "to": link.address(args.to), "body": args.text}
    if args.reply_to:
        payload["reply_to"] = args.reply_to
    if args.fyi:
        payload["fyi"] = True  # delivered at their next hook, never wakes them
    try:
        item = session.project_call("POST", "/projects/{pid}/items", payload)
    except urllib.error.HTTPError as exc:
        raise Failure(f"tell failed: {_detail(exc)}") from None
    to = item.get("to_label") or item.get("to")
    note = " FYI" if args.fyi else ""
    print(f"[ptt] sent{note} {item.get('kind')} to {to} (item {item.get('item_id')})")


def _ack_mention(session: Session, message_id: str) -> bool:
    try:
        _, pending = session.channel_call("GET", "/channels/{cid}/inbox?limit=20")
    except (urllib.error.HTTPError, Failure):
        return False
    if not any(m.get("message_id") == message_id for m in pending or []):
        return False
    session.channel_call("POST", "/channels/{cid}/inbox/ack", {"message_ids": [message_id]})
    return True


def cmd_ack(session: Session, args) -> None:
    if _ack_mention(session, args.id):
        print(f"[ptt] acked message {args.id}")
        return
    try:
        session.project_call("POST", f"/projects/{{pid}}/items/{args.id}/ack")
    except urllib.error.HTTPError as exc:
        raise Failure(f"ack failed: {_detail(exc)}") from None
    print(f"[ptt] acked item {args.id}")


def cmd_done(session: Session, args) -> None:
    if _ack_mention(session, args.id):
        print(f"[ptt] acked message {args.id} (mentions have no done state)")
        return
    try:
        item = session.project_call(
            "POST", f"/projects/{{pid}}/items/{args.id}/done", {"note": args.note}
        )
    except urllib.error.HTTPError as exc:
        raise Failure(f"done failed: {_detail(exc)}") from None
    print(f"[ptt] {item.get('state', 'done')}: {item.get('title', args.id)}")


# --- wait -------------------------------------------------------------------


class Waiter:
    """Two long-poll loops (channel mentions, project inbox) that print one line per item.

    Only waking entries end a wait: with a server that advertises ``fyi_supported``
    both polls send ``wake_only=1``, and notes, claims and FYIs never start a wake.
    The first waking entry opens a batch window (``AGENT_PTT_WAKE_BATCH_SECONDS``,
    default 4): whatever else arrives meanwhile, plus pending FYIs, is printed in
    one burst at its end (a Monitor turns lines within 200 ms into one turn) and
    only then acknowledged.
    """

    def __init__(
        self,
        session: Session,
        out=None,
        wait: int = WAIT_SECONDS,
        batch: float | None = None,
        clock=time.monotonic,
    ):
        self.session = session
        self.out = out or sys.stdout
        self.wait = wait
        self.batch = link.wake_batch_seconds() if batch is None else max(0.0, batch)
        self.clock = clock
        self.lock = threading.RLock()
        self.printed: set[str] = set()
        self.held: list[tuple[str, str, str]] = []  # (line, "message"|"item", id)
        self.flush_at: float | None = None
        self.cursor = int(session.member.get("cursor") or 0)
        self.heartbeat = None  # called every 15 s while running (keeps the lock fresh)

    def wake_only(self) -> str:
        return "&wake_only=1" if self.session.channel().get("fyi_supported") else ""

    def _known(self, entry_id: str) -> bool:
        return entry_id in self.printed or any(held[2] == entry_id for held in self.held)

    def hold(self, entries: list[tuple[str, str, str]]) -> None:
        """Queue waking lines; the first opens the batch window."""
        if not entries:
            return
        with self.lock:
            self.held += [e for e in entries if not self._known(e[2])]
            if self.flush_at is None:
                self.flush_at = self.clock() + self.batch
        if self.batch <= 0:
            self.flush()

    def due(self) -> bool:
        with self.lock:
            return self.flush_at is not None and self.clock() >= self.flush_at

    def flush(self) -> int:
        """Print the held lines plus anything else pending (FYIs, notes) at once, then ack."""
        with self.lock:
            if self.flush_at is None:
                return 0
            entries = list(self.held)
            for message in self._read_channel():
                if not self._known(message["message_id"]):
                    entries.append((link.mention_line(message), "message", message["message_id"]))
            for item in self._read_project():
                if not self._known(item["item_id"]):
                    entries.append((link.item_line(item), "item", item["item_id"]))
            if entries:
                self.out.write("".join(line + "\n" for line, _, _ in entries))
                self.out.flush()
            self.printed |= {entry_id for _, _, entry_id in entries}
            self.held, self.flush_at = [], None
        self._ack(
            [e[2] for e in entries if e[1] == "message"], [e[2] for e in entries if e[1] == "item"]
        )
        return len(entries)

    def _read_channel(self) -> list[dict]:
        try:
            _, messages = self.session.channel_call("GET", "/channels/{cid}/inbox?limit=20")
        except Exception:
            return []
        return messages or []

    def _read_project(self) -> list[dict]:
        if not self.session.has_project():
            return []
        try:
            data = self.session.project_call("GET", "/projects/{pid}/inbox?limit=50")
        except Exception:
            return []
        return (data or {}).get("items") or []

    def _ack(self, message_ids: list[str], item_ids: list[str]) -> None:
        if message_ids:  # printed: acked so receive.py doesn't inject them again
            with contextlib.suppress(Exception):
                self.session.channel_call(
                    "POST", "/channels/{cid}/inbox/ack", {"message_ids": message_ids}
                )
        for item_id in item_ids:
            # An item finished meanwhile can't be acked; it was printed, not repeated.
            with contextlib.suppress(Exception):
                self.session.project_call("POST", f"/projects/{{pid}}/items/{item_id}/ack")

    def channel_once(self, wait: int) -> int:
        state = self.session.channel()
        # Only servers advertising wait_supported long-poll; others answer at once.
        wait = (
            min(wait, int(state.get("wait_max_seconds") or 0)) if state.get("wait_supported") else 0
        )
        _, messages = self.session.channel_call(
            "GET",
            f"/channels/{{cid}}/inbox?limit=20&wait={wait}{self.wake_only()}",
            timeout=wait + 15,
        )
        messages = [m for m in messages or [] if link.wakes(m)]
        stale = [m["message_id"] for m in messages if m["message_id"] in self.printed]
        if stale:  # printed earlier but the ack failed: ack again, don't repeat
            self._ack(stale, [])
        fresh = [m for m in messages if not self._known(m["message_id"])]
        self.hold([(link.mention_line(m), "message", m["message_id"]) for m in fresh])
        return len(fresh)

    def project_once(self, wait: int) -> int:
        data = self.session.project_call(
            "GET",
            f"/projects/{{pid}}/inbox?limit=50&wait={wait}&after={self.cursor}{self.wake_only()}",
            timeout=wait + 15,
        )
        self.cursor = max(self.cursor, int(data.get("cursor") or 0))
        items = [i for i in data.get("items") or [] if link.wakes(i)]
        stale = [i["item_id"] for i in items if i["item_id"] in self.printed]
        if stale:
            self._ack([], stale)
        fresh = [i for i in items if not self._known(i["item_id"])]
        self.hold([(link.item_line(i), "item", i["item_id"]) for i in fresh])
        return len(fresh)

    def loop(self, poll, stop: threading.Event, sleep=time.sleep) -> None:
        failures = 0
        while not stop.is_set():
            started = time.monotonic()
            try:
                fresh = poll(self.wait)
                failures = 0
            except Failure:
                return  # nothing to poll (no project): this loop is done
            except Exception:
                failures += 1
                sleep(min(MAX_BACKOFF, 2.0 ** min(failures, 6)))
                continue
            elapsed = time.monotonic() - started
            if not fresh and elapsed < MIN_CYCLE:
                sleep(MIN_CYCLE - elapsed)  # older servers answer at once: don't spin

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        threads = [threading.Thread(target=self.loop, args=(self.channel_once, stop), daemon=True)]
        if self.session.has_project():
            threads.append(
                threading.Thread(target=self.loop, args=(self.project_once, stop), daemon=True)
            )
        for thread in threads:
            thread.start()
        beat = self.clock()
        while not stop.is_set() and any(t.is_alive() for t in threads):
            with self.lock:
                flush_at = self.flush_at
            timeout = 15.0 if flush_at is None else max(0.0, min(15.0, flush_at - self.clock()))
            stop.wait(timeout)
            if self.due():
                self.flush()
            if self.heartbeat and self.clock() - beat >= 15:
                self.heartbeat()
                beat = self.clock()
        if self.flush_at is not None:
            self.flush()  # never drop a held batch


def _holder_alive(path: Path) -> bool:
    """False when the pid in a wait lock is gone (a waiter killed before its cleanup).

    Windows has no ``kill(pid, 0)`` probe, so there only the heartbeat age counts.
    """
    if os.name == "nt":
        return True
    try:
        pid = int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return True  # being written right now: trust the heartbeat
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # alive, owned by someone else
    return True


def _exit_on_signal(signum, frame) -> None:
    raise SystemExit(128 + signum)


def _wait_lock(session: Session) -> Path | None:
    """A portable single-instance lock: an O_EXCL file kept fresh by a heartbeat."""
    path = link.PROJECTS_DIR / f"wait-{link._digest(session.base, session.session_id)[:32]}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age < LOCK_STALE and _holder_alive(path):
                return None
            path.unlink(missing_ok=True)
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path
    return None


def cmd_wait(session: Session, args) -> None:
    if args.once:
        waiter = Waiter(session, batch=0)
        waiter.flush_at = waiter.clock()  # one read of everything pending, FYIs included
        count = waiter.flush()
        if not count:
            print("[ptt] nothing new")
        return
    waiter = Waiter(session)
    lock = _wait_lock(session)
    if lock is None:
        print("[ptt] already waiting for this session in another process", flush=True)
        return
    waiter.heartbeat = lambda: os.utime(lock)
    # A Monitor that expires ends us with SIGTERM: exit through the finally below,
    # so the lock is gone and a re-armed Monitor can wait straight away.
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(getattr(signal, name), _exit_on_signal)
    me = session.member.get("handle") or session.agent
    # stderr: a Monitor turns each stdout line into a turn; this one carries nothing to act on.
    print(f"[ptt] waiting for mentions and work items as {me}", file=sys.stderr, flush=True)
    try:
        waiter.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        lock.unlink(missing_ok=True)
        if session.member_path and session.member:
            session.member["cursor"] = waiter.cursor
            link.save_membership(session.member_path, session.member)


# --- Codex sandbox rule ----------------------------------------------------------


def codex_rules() -> str:
    """An execpolicy rule letting Codex run this ptt.py outside its sandbox.

    Codex's workspace-write sandbox has no network, so every ``ptt`` call
    otherwise needs an escalation. A ``prefix_rule`` with ``decision = "allow"``
    makes Codex run matching commands without the sandbox and without a prompt.
    Tokens match exactly, so the rule names this machine's python and this
    copy of ptt.py (re-run after a plugin update moves the cache folder).
    """
    here = str(Path(__file__).resolve())
    pythons = list(dict.fromkeys([sys.executable or "python3", "python3"]))
    return (
        "# Agent PTT: let the ptt command reach the server without a sandbox prompt.\n"
        "# Save as ~/.codex/rules/agent-ptt.rules, then restart Codex.\n"
        "prefix_rule(\n"
        f"    pattern = [{json.dumps(pythons)}, {json.dumps(here)}],\n"
        '    decision = "allow",\n'
        f'    justification = "Agent PTT: ptt posts to the PTT server at {link.base_url()}",\n'
        ")\n"
    )


def cmd_codex_rules(args) -> None:
    sys.stdout.write(codex_rules())


# --- entry point --------------------------------------------------------------


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read() or b"{}")
        return str(body.get("detail") or body.get("error") or exc.code)
    except (ValueError, AttributeError, OSError):
        return str(exc.code)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ptt", description="Agent PTT for this agent session")
    p.add_argument("--session", help="session ID (default: found from the env or this folder)")
    sub = p.add_subparsers(dest="command", required=True)
    say = sub.add_parser("say", help="speak in the channel; @Handle mentions resolve server-side")
    say.add_argument("text")
    say.add_argument("--quiet", action="store_true", help="post as text only, not spoken")
    say.add_argument("--fyi", action="store_true", help="informational: mentions wake no one")
    sub.add_parser("who", help="who is in the project and channel")
    sub.add_parser("inbox", help="pending mentions and work items")
    wait = sub.add_parser("wait", help="print one line per new mention/work item (Monitor)")
    wait.add_argument("--once", action="store_true", help="poll once and exit")
    tell = sub.add_parser("tell", help="send a work item")
    tell.add_argument("to", help='Handle, @Handle, @"Two Words", role:<role> or project')
    tell.add_argument("text")
    tell.add_argument("--kind", default="instruction", choices=KINDS)
    tell.add_argument("--reply-to", dest="reply_to")
    tell.add_argument("--fyi", action="store_true", help="informational: never wakes them")
    sub.add_parser("codex-rules", help="print a Codex rules file that lets ptt skip the sandbox")
    for name in ("ack", "done"):
        cmd = sub.add_parser(name, help=f"{name} a mention (message ID) or work item")
        cmd.add_argument("id")
        if name == "done":
            cmd.add_argument("--note")
    return p


COMMANDS = {
    "say": cmd_say,
    "who": cmd_who,
    "inbox": cmd_inbox,
    "wait": cmd_wait,
    "tell": cmd_tell,
    "ack": cmd_ack,
    "done": cmd_done,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "codex-rules":
        cmd_codex_rules(args)  # needs no session
        return 0
    try:
        session = Session(args.session)
        COMMANDS[args.command](session, args)
    except Failure as exc:
        print(f"ptt: {exc}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(f"ptt: server said {exc.code}: {_detail(exc)}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"ptt: server unreachable at {link.base_url()}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
