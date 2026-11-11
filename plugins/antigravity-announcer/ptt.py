#!/usr/bin/env python3
"""ptt: talk to the other agents in this session's Agent PTT channel and project.

Stdlib only; runs on macOS, Linux and Windows next to the plugin's hooks. It
finds this session's channel key and project membership in the hook state under
``~/.agent-ptt`` (written by session_start.py and receive.py), so no keys or IDs
need to be pasted. The session is ``--session``, else ``AGENT_PTT_SESSION_ID`` /
``CODEX_THREAD_ID`` / ``CLAUDE_SESSION_ID`` / ``CLAUDE_CODE_SESSION_ID``, else the
most recently active hooked session whose folder contains (or is inside) the
current directory.

  ptt.py say "<text with @Theo>" [--quiet] [--fyi]   speak (--quiet: text only) in the channel
  ptt.py who                                 who is here: harness, machine, doing, presence
  ptt.py inbox                               pending mentions and work items (not acked)
  ptt.py wait [--once|--first]               print one line per waking mention/work item
  ptt.py tell <to> "<text>" [--kind K] [--fyi]   send a work item: Handle, role:<r> or project

--fyi marks status, acknowledgements and "done": delivered at the recipient's next
hook, never waking an idle agent.
  ptt.py ack <id> | done <id> [--note N]     acknowledge / finish a mention or work item
  ptt.py codex-rules                         print a Codex rule so ptt can reach the server
  ptt.py agy-rules [--install]               Antigravity grants so ptt and its MCP tools never ask
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
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
TROUBLE_SECONDS = 300.0  # a poll failing this long gets one stdout line (and keeps retrying)
KINDS = ("instruction", "question", "answer", "note", "handoff", "claim", "release")


class Failure(Exception):
    pass


class SessionEnded(Failure):
    """The session's SessionEnd hook ran: nothing more is delivered to it."""


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

    def ended(self) -> bool:
        """True once SessionEnd marked the pointer (a resumed session clears it)."""
        return bool(link.load_session(self.base, self.session_id).get("ended"))

    def channel_name(self, state: dict) -> str | None:
        """The channel's name: the state file, else the session pointer.

        SessionEnd deletes the state file, and an old one may lack the name; the
        pointer keeps ``channel_name`` (and ``channel_id``, looked up as a last resort).
        """
        if state.get("name"):
            return state["name"]
        pointer = link.load_session(self.base, self.session_id) or self.pointer
        if pointer.get("channel_name"):
            return pointer["channel_name"]
        cid = pointer.get("channel_id") or state.get("channel_id")
        if not cid:
            return None
        try:
            channel = link.request(self.base, "GET", f"/channels/{cid}", timeout=5)
        except Exception:
            return None
        return (channel or {}).get("name") or None

    def reconnect(self, state: dict | None = None) -> dict:
        with self.lock:
            if self.ended():
                raise SessionEnded(f"session {self.session_id} ended")
            state = state or link._read(self.channel_path)
            name = self.channel_name(state)
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


def _text(args) -> str:
    """The message: the argument, or the contents of --file (for text with quotes,
    backticks or newlines, which Codex's command parser won't let past its sandbox)."""
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise Failure(f"can't read {args.file}: {exc}") from None
    else:
        text = args.text or ""
    if not text.strip():
        raise Failure("nothing to send: give the text or --file <path>")
    return text.strip()


def cmd_say(session: Session, args) -> None:
    text = _text(args)

    def payload(state):
        body = {"key_id": state["key_id"], "text": text}
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
    version = link.plugin_version()
    if version:
        print(f"Your Agent PTT plugin: {version}.")
    stale = link.stale_plugin_line(version, people)
    if stale:
        print(stale)
    for person in people:
        print(link.person_line(person, version=True))


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
    payload = {"kind": args.kind, "to": link.address(args.to), "body": _text(args)}
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


def _resolve(session: Session, prefix: str) -> tuple[str, str, bool]:
    """(kind, full id, still pending) for a message or work-item id or unique prefix.

    Looks at what is pending, then at what the hooks and `ptt wait` already
    delivered to this session. Raises Failure when nothing or several match.
    """
    prefix = prefix.strip()
    if not prefix:
        raise Failure("give a message or work-item id (a unique prefix is enough)")
    mentions, items = _pending(session)
    found: dict[str, tuple[str, str, bool]] = {}
    for message in mentions:
        if message["message_id"].startswith(prefix):
            found[message["message_id"]] = ("message", message["message_id"], True)
    for item in items:
        if item["item_id"].startswith(prefix):
            found[item["item_id"]] = ("item", item["item_id"], True)
    if not found:
        pointer = link.load_session(session.base, session.session_id)
        for kind, entry_id in pointer.get("delivered") or []:
            if str(entry_id).startswith(prefix):
                found[entry_id] = (kind, entry_id, False)
    if len(found) == 1:
        return next(iter(found.values()))
    if found:
        listed = ", ".join(f"{kind} {entry_id}" for kind, entry_id, _ in found.values())
        raise Failure(f"{prefix} matches several: {listed}; give more of the id")
    if len(prefix) >= 32:
        return "item", prefix, False  # a full id we never saw: try it as a work item
    raise Failure(f"no pending or delivered mention or work item starts with {prefix}")


def _ack_message(session: Session, message_id: str) -> None:
    session.channel_call("POST", "/channels/{cid}/inbox/ack", {"message_ids": [message_id]})


def cmd_ack(session: Session, args) -> None:
    kind, entry_id, pending = _resolve(session, args.id)
    if kind == "message":
        if pending:
            _ack_message(session, entry_id)
            print(f"[ptt] acked mention {entry_id}")
        else:
            print(f"[ptt] mention {entry_id} was already delivered; mentions need no ack")
        return
    try:
        session.project_call("POST", f"/projects/{{pid}}/items/{entry_id}/ack")
    except urllib.error.HTTPError as exc:
        raise Failure(f"ack of work item {entry_id} failed: {_detail(exc)}") from None
    print(f"[ptt] acked work item {entry_id}")


def cmd_done(session: Session, args) -> None:
    kind, entry_id, pending = _resolve(session, args.id)
    if kind == "message":
        if pending:
            _ack_message(session, entry_id)
        print(f"[ptt] {entry_id} is a mention, not a work item: nothing to finish")
        return
    try:
        item = session.project_call(
            "POST", f"/projects/{{pid}}/items/{entry_id}/done", {"note": args.note}
        )
    except urllib.error.HTTPError as exc:
        raise Failure(f"done for work item {entry_id} failed: {_detail(exc)}") from None
    print(f"[ptt] work item {entry_id} {item.get('state', 'done')}: {item.get('title', '')}")


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
        self.capped: set[str] = set()  # held back by the wake cap: the next hook delivers
        self.cap = True  # `wait --once` prints on request: not a wake
        self.first = False  # `wait --first`: exit after the first delivery (background tasks)
        self.stop_event: threading.Event | None = None
        self.stopped: str | None = None  # why a poll loop gave up: the whole wait ends
        self.ended = False  # the session ended (no restart advice)
        self.trouble_after = TROUBLE_SECONDS  # failing this long: say so once, keep trying

    def wake_only(self) -> str:
        return "&wake_only=1" if self.session.channel().get("fyi_supported") else ""

    def _known(self, entry_id: str) -> bool:
        return (
            entry_id in self.printed
            or entry_id in self.capped
            or any(held[2] == entry_id for held in self.held)
        )

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
            if self.held and self.cap and not take_wake(self.session):
                # Loop guard: leave them pending for the next hook; don't print again.
                self.capped |= {entry_id for _, _, entry_id in self.held}
                self.held, self.flush_at = [], None
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
                if self.first and self.stop_event:
                    self.stop_event.set()  # the exit is the wake (an Antigravity background task)
            self.printed |= {entry_id for _, _, entry_id in entries}
            self.held, self.flush_at = [], None
        if entries:
            with contextlib.suppress(Exception):
                link.remember_delivered(
                    self.session.base, self.session.session_id, [(k, i) for _, k, i in entries]
                )
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

    def say(self, line: str) -> None:
        """One stdout line (a Monitor event), outside the batched delivery."""
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()

    def give_up(self, reason: str, stop: threading.Event, ended: bool = False) -> None:
        """End the whole wait: never leave the other loop running alone."""
        with self.lock:
            if self.stopped is None:
                self.stopped, self.ended = reason, ended
        stop.set()

    def loop(self, poll, stop: threading.Event, sleep=time.sleep, name: str = "inbox") -> None:
        failures, failing_since, told = 0, None, False
        while not stop.is_set():
            started = time.monotonic()
            try:
                fresh = poll(self.wait)
                failures, failing_since, told = 0, None, False
            except SessionEnded as exc:
                self.give_up(str(exc), stop, ended=True)
                return
            except Failure as exc:
                self.give_up(f"the {name} poll failed: {exc}", stop)
                return
            except Exception as exc:
                failures += 1
                now = self.clock()
                failing_since = now if failing_since is None else failing_since
                if not told and now - failing_since >= self.trouble_after:
                    told = True
                    minutes = round((now - failing_since) / 60)
                    self.say(
                        f"[ptt] wait: the {name} poll has failed for {minutes} min"
                        f" ({type(exc).__name__}: {link.one_line(str(exc), 200)});"
                        " still retrying, mentions may be late"
                    )
                sleep(min(MAX_BACKOFF, 2.0 ** min(failures, 6)))
                continue
            elapsed = time.monotonic() - started
            if not fresh and elapsed < MIN_CYCLE:
                sleep(MIN_CYCLE - elapsed)  # older servers answer at once: don't spin

    def restart_command(self) -> str:
        return link.script_command("ptt.py", "--session", self.session.session_id, "wait")

    def stop_line(self) -> str | None:
        if self.stopped is None:
            return None
        if self.ended:
            return (
                f"[ptt] wait stopped: {self.stopped}; nothing more is delivered to it."
                " Don't restart this wait."
            )
        return f"[ptt] wait stopped: {self.stopped}. Start it again with: {self.restart_command()}"

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.stop_event = stop
        polls = [("channel", self.channel_once)]
        if self.session.has_project():
            polls.append(("project", self.project_once))
        threads = [
            threading.Thread(
                target=self.loop, args=(poll, stop), kwargs={"name": name}, daemon=True
            )
            for name, poll in polls
        ]
        for thread in threads:
            thread.start()
        beat = self.clock()
        while not stop.is_set():
            if not all(t.is_alive() for t in threads):
                self.give_up("a poll loop ended unexpectedly", stop)
                break
            with self.lock:
                flush_at = self.flush_at
            timeout = 15.0 if flush_at is None else max(0.0, min(15.0, flush_at - self.clock()))
            stop.wait(timeout)
            if self.due():
                self.flush()
            if self.clock() - beat >= 15:
                beat = self.clock()
                if self.heartbeat:
                    self.heartbeat()
                with contextlib.suppress(Exception):
                    if self.session.ended():  # SessionEnd ran: this wait is a zombie
                        self.give_up(f"session {self.session.session_id} ended", stop, True)
        if self.flush_at is not None:
            self.flush()  # never drop a held batch
        line = self.stop_line()
        if line:
            self.say(line)


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


def _wait_lock(session: Session, path: Path | None = None) -> Path | None:
    """A portable single-instance lock: an O_EXCL file kept fresh by a heartbeat."""
    path = path or link.wait_lock_path(session.base, session.session_id)
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
        waiter.cap = False
        waiter.flush_at = waiter.clock()  # one read of everything pending, FYIs included
        count = waiter.flush()
        if not count:
            print("[ptt] nothing new")
        return
    waiter = Waiter(session)
    waiter.first = bool(getattr(args, "first", False))
    if session.ended():
        waiter.give_up(f"session {session.session_id} ended", threading.Event(), ended=True)
        print(waiter.stop_line(), flush=True)
        return
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
    version = link.plugin_version()
    note = f" (plugin {version})" if version else ""
    print(f"[ptt] waiting for mentions and work items as {me}{note}", file=sys.stderr, flush=True)
    with contextlib.suppress(Exception):
        people = link.fetch_people(session.base, session.member or {}, None)
        stale = link.stale_plugin_line(version, people)
        if stale:
            print(f"[ptt] {stale}", file=sys.stderr, flush=True)
    try:
        waiter.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        lock.unlink(missing_ok=True)
        if session.member_path and session.member:
            session.member["cursor"] = waiter.cursor
            link.save_membership(session.member_path, session.member)


# --- wake cap -------------------------------------------------------------------


def cap_notice(session: Session, until: float, now: float | None = None) -> None:
    """One text-only FYI per cap window, so senders know why nothing happens."""
    now = time.time() if now is None else now
    base, sid = session.base, session.session_id
    if now - float(link.load_session(base, sid).get("cap_noticed_at") or 0) < 3600:
        return
    link.save_session(base, sid, cap_noticed_at=now)
    me = session.member.get("handle") or session.agent
    when = time.strftime("%H:%M", time.localtime(until))
    text = (
        f"{me} reached its wake cap ({link.wake_cap()} per hour): new mentions wait"
        f" until {when} or its next prompt."
    )
    with contextlib.suppress(Exception):
        session.channel_call(
            "POST",
            "/channels/{cid}/say",
            lambda state: {"key_id": state["key_id"], "text": text, "speak": False, "fyi": True},
        )


def take_wake(session: Session, now: float | None = None) -> bool:
    """Count one wake against AGENT_PTT_WAKE_MAX_PER_HOUR; False (and a notice) if capped."""
    now = time.time() if now is None else now
    base, sid = session.base, session.session_id
    wakes = link.recent_wakes(link.load_session(base, sid), now)
    if len(wakes) >= link.wake_cap():
        cap_notice(session, min(wakes) + 3600, now)
        return False
    link.save_session(base, sid, wakes=[*wakes, now])
    return True


# --- codex-watch ----------------------------------------------------------------

POKE = (
    "[ptt] Messages for you from other agents arrived while you were idle. The"
    " Agent PTT hook attaches them to this prompt; handle them as its context says (if"
    " nothing is attached, they were handled already: just end the turn)."
)
# One poke per wake: Codex keeps queued input until the thread can take it, so a second
# poke would only run later as a duplicate turn.
MAX_POKES = 1
STUCK_SECONDS = 90.0  # a poke no hook reacted to by then: the session is paused or closed
WATCH_LIFETIME = 24 * 3600.0
GIVE_UP_SECONDS = 210.0


class CodexWatch:
    """Wakes an idle Codex session: on a waking mention or work item, queue a short
    prompt into its thread with `codex queue` (the TUI starts a turn within ~10 s and
    the UserPromptSubmit hook delivers the real messages, with the trust rules).

    Never acks anything: delivery stays with the hooks. Skips busy turns (the hooks
    mark them; PostToolUse and Stop deliver there), batches close mentions into one
    poke, re-pokes at most once if nothing picked it up, and counts every poke
    against the wake cap. Exits with the Codex process, the session, or after 24 h.
    """

    def __init__(self, session: Session, pid: int = 0, clock=time.time, runner=None):
        self.session = session
        self.pid = pid
        self.clock = clock
        self.started = clock()
        self.run_queue = runner or self._codex_queue
        self.poked: dict[str, tuple[float, int]] = {}
        self.batch = link.wake_batch_seconds()
        self.heartbeat = None
        self.unanswered_since: float | None = None  # first poke no hook has reacted to
        self.stuck_noticed = False

    def _codex_queue(self, message: str) -> bool:
        binary = link.codex_binary()
        if not binary:
            return False
        try:
            result = subprocess.run(
                [binary, "queue", "--thread", self.session.session_id, "--message", message],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode:
            print(f"[ptt] codex queue failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return result.returncode == 0

    def alive(self) -> bool:
        if self.clock() - self.started > WATCH_LIFETIME or not link.alive(self.pid):
            return False
        pointer = link.load_session(self.session.base, self.session.session_id)
        if not pointer or pointer.get("ended"):
            return False
        if self.unanswered_since is not None:
            waited = self.clock() - self.unanswered_since
            if float(pointer.get("turn_at") or 0) > self.unanswered_since:
                self.unanswered_since = None  # a hook ran after our poke: the TUI is there
                self.stuck_noticed = False
            elif waited > STUCK_SECONDS and not self.stuck_noticed:
                # Codex holds queued input after Esc (AgentStatus::Interrupted) until the
                # user's next prompt; a closed window never takes it. Say so once.
                self.stuck_noticed = True
                self.notice_stuck()
            if self.unanswered_since is not None and waited > GIVE_UP_SECONDS:
                # In daemon mode we follow Codex's shared app-server, not the TUI: pokes
                # nobody picks up mean the window was closed. The next session restarts us.
                print("[ptt] codex-watch: pokes unanswered, the TUI is gone", file=sys.stderr)
                return False
        return True

    def waking(self, wait: int) -> list[str]:
        """Ids of pending mentions and work items that should wake this session."""
        state = self.session.channel()
        wake_only = "&wake_only=1" if state.get("fyi_supported") else ""
        wait = (
            min(wait, int(state.get("wait_max_seconds") or 0)) if state.get("wait_supported") else 0
        )
        _, messages = self.session.channel_call(
            "GET", f"/channels/{{cid}}/inbox?limit=20&wait={wait}{wake_only}", timeout=wait + 15
        )
        ids = [m["message_id"] for m in messages or [] if link.wakes(m)]
        if self.session.has_project():
            with contextlib.suppress(Exception):
                data = self.session.project_call(
                    "GET", f"/projects/{{pid}}/inbox?limit=50{wake_only}"
                )
                ids += [i["item_id"] for i in (data or {}).get("items") or [] if link.wakes(i)]
        return ids

    def due(self, ids: list[str]) -> list[str]:
        if self.unanswered_since is not None:
            return []  # a queued poke is still waiting: it delivers everything pending
        pointer = link.load_session(self.session.base, self.session.session_id)
        if link.is_busy(pointer, self.clock()):
            return []  # the hooks deliver during a turn and at its Stop
        return [i for i in ids if self.poked.get(i, (0.0, 0))[1] < MAX_POKES]

    def poke(self, ids: list[str]) -> bool:
        base, sid = self.session.base, self.session.session_id
        pointer = link.load_session(base, sid)
        now = self.clock()
        wakes = link.recent_wakes(pointer, now)
        if len(wakes) >= link.wake_cap():
            for entry_id in ids:
                self.poked[entry_id] = (now, MAX_POKES)  # wait for the next hook
            self.notice_cap(min(wakes) + 3600)
            return False
        if not self.run_queue(POKE):
            return False
        link.save_session(base, sid, wakes=[*wakes, now])
        if self.unanswered_since is None:
            self.unanswered_since = now
        for entry_id in ids:
            self.poked[entry_id] = (now, self.poked.get(entry_id, (0.0, 0))[1] + 1)
        print(f"[ptt] poked Codex for {len(ids)} pending", file=sys.stderr, flush=True)
        return True

    def notice_cap(self, until: float) -> None:
        cap_notice(self.session, until, self.clock())

    def notice_stuck(self) -> None:
        me = self.session.member.get("handle") or self.session.agent
        text = (
            f"{me} isn't picking up wake-ups (its Codex window is paused after Esc, or"
            " closed): new mentions wait for its next prompt."
        )
        with contextlib.suppress(Exception):
            self.session.channel_call(
                "POST",
                "/channels/{cid}/say",
                lambda state: {
                    "key_id": state["key_id"],
                    "text": text,
                    "speak": False,
                    "fyi": True,
                },
            )

    def step(self, wait: int, sleep) -> None:
        ids = self.waking(wait)
        if not self.due(ids):
            return
        if self.batch > 0:
            sleep(self.batch)  # let close mentions arrive: one poke for the burst
            ids = self.waking(0)
        due = self.due(ids)
        if due:
            self.poke(due)

    def run(self, stop: threading.Event | None = None, sleep=None) -> None:
        stop = stop or threading.Event()
        sleep = sleep or stop.wait
        failures = 0
        while not stop.is_set() and self.alive():
            started = time.monotonic()
            try:
                self.step(WAIT_SECONDS, sleep)
                failures = 0
            except Exception as exc:
                failures += 1
                print(f"[ptt] codex-watch: {exc}", file=sys.stderr, flush=True)
                sleep(min(MAX_BACKOFF, 2.0 ** min(failures, 6)))
            if self.heartbeat:
                self.heartbeat()
            elapsed = time.monotonic() - started
            if elapsed < MIN_CYCLE:
                sleep(MIN_CYCLE - elapsed)  # pending-but-not-due returns at once: don't spin


def cmd_codex_watch(session: Session, args) -> None:
    lock = _wait_lock(session, link.watch_lock_path(session.base, session.session_id))
    if lock is None:
        print("[ptt] a codex-watch already runs for this session", file=sys.stderr)
        return
    with contextlib.suppress(OSError):
        lock.write_text(str(os.getpid()))
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(getattr(signal, name), _exit_on_signal)
    watch = CodexWatch(session, pid=args.pid)
    watch.heartbeat = lambda: os.utime(lock)
    print(f"[ptt] codex-watch for {session.session_id} (pid {args.pid})", file=sys.stderr)
    try:
        watch.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        lock.unlink(missing_ok=True)


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


CODEX_SANDBOX_HINT = (
    "ptt: this ran inside Codex's sandbox, which has no network. The allow rule from"
    " `ptt codex-rules` only covers plain commands: no backslashes, $, backticks,"
    " heredocs or pipes. Write the text to a file first and run"
    " `ptt say --file <path>` (or `ptt tell <to> --file <path>`)."
)


def cmd_codex_rules(args) -> None:
    sys.stdout.write(codex_rules())


# --- Antigravity permission grants ---------------------------------------------

AGY_MCP_SERVER = "agent-ptt-announcer_agent-ptt"  # <plugin name>_<server name>


def agy_settings_path() -> Path:
    """The Antigravity CLI's user settings (``AGENT_PTT_AGY_SETTINGS`` overrides)."""
    override = os.environ.get("AGENT_PTT_AGY_SETTINGS", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def agy_grants() -> list[str]:
    """``permissions.allow`` grants for this ptt.py and the plugin's MCP tools.

    agy's safety reviewer (a model) can flag a ptt call, e.g. a ``done`` that
    changes shared state, and then asks the user even when the plugin's
    PreToolUse hook approved it in a session whose hooks didn't load. A grant
    skips the review. ``command(<prefix>)`` matches the start of the command
    line, so the grant names this machine's python and this copy of ptt.py.
    """
    here = str(Path(__file__).resolve())
    pythons = list(dict.fromkeys([sys.executable or "python3", "python3"]))
    return [f"command({python} {here})" for python in pythons] + [f"mcp({AGY_MCP_SERVER}/*)"]


def _is_ptt_grant(grant: str) -> bool:
    """An Agent PTT grant from an earlier install (e.g. another python or plugin copy)."""
    return grant.startswith(f"mcp({AGY_MCP_SERVER}/") or (
        grant.startswith("command(")
        and grant.rstrip(")").endswith(("/ptt.py", "\\ptt.py"))
        and "agent-ptt" in grant
    )


def install_agy_grants(path: Path) -> tuple[list[str], list[str]]:
    """Merge the grants into agy's settings.json; returns (added, removed)."""
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise Failure(
                f"{path} is not valid JSON ({exc}); fix it or add the grants by hand"
            ) from None
        if not isinstance(settings, dict):
            raise Failure(f"{path} is not a JSON object; add the grants by hand")
    permissions = settings.setdefault("permissions", {})
    allow = permissions.setdefault("allow", []) if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        raise Failure(f"{path}: permissions.allow is not a list; add the grants by hand")
    wanted = agy_grants()
    removed = [g for g in allow if isinstance(g, str) and _is_ptt_grant(g) and g not in wanted]
    added = [g for g in wanted if g not in allow]
    permissions["allow"] = [g for g in allow if g not in removed] + added
    if added or removed:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".agent-ptt.tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return added, removed


def cmd_agy_rules(args) -> None:
    path = agy_settings_path()
    if not args.install:
        print(json.dumps({"permissions": {"allow": agy_grants()}}, indent=2))
        print(
            f"Add these to permissions.allow in {path}, or run this command again with"
            " --install, then restart agy.",
            file=sys.stderr,
        )
        return
    added, removed = install_agy_grants(path)
    if not added and not removed:
        print(f"[ptt] {path} already allows ptt and the agent-ptt MCP tools")
        return
    for grant in removed:
        print(f"[ptt] removed old grant {grant}")
    for grant in added:
        print(f"[ptt] added {grant}")
    print(f"[ptt] saved {path}; restart agy to use it")


# --- entry point --------------------------------------------------------------


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read() or b"{}")
        return str(body.get("detail") or body.get("error") or exc.code)
    except (ValueError, AttributeError, OSError):
        return str(exc.code)


def error_text(exc: BaseException) -> str | None:
    """What the CLI prints for a failed command, or None for an unexpected error."""
    if isinstance(exc, Failure):
        return f"ptt: {exc}"
    if isinstance(exc, urllib.error.HTTPError):
        return f"ptt: server said {exc.code}: {_detail(exc)}"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        text = f"ptt: server unreachable at {link.base_url()}: {exc}"
        if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"):
            text += "\n" + CODEX_SANDBOX_HINT
        return text
    return None


def run_captured(session: Session, args) -> tuple[str, str | None]:
    """Run a command for a caller that isn't a terminal (ptt_mcp.py).

    Returns (what it printed, the CLI's error message or None). The output is the
    CLI's, line for line; an unexpected exception still raises.
    """
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            COMMANDS[args.command](session, args)
    except (Failure, urllib.error.URLError, OSError) as exc:
        return out.getvalue(), error_text(exc)
    return out.getvalue(), None


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ptt", description="Agent PTT for this agent session")
    p.add_argument("--session", help="session ID (default: found from the env or this folder)")
    sub = p.add_subparsers(dest="command", required=True)
    say = sub.add_parser("say", help="speak in the channel; @Handle mentions resolve server-side")
    say.add_argument("text", nargs="?")
    say.add_argument("--file", help="read the text from this file (Codex: for quotes/newlines)")
    say.add_argument("--quiet", action="store_true", help="post as text only, not spoken")
    say.add_argument("--fyi", action="store_true", help="informational: mentions wake no one")
    sub.add_parser("who", help="who is in the project and channel")
    sub.add_parser("inbox", help="pending mentions and work items")
    wait = sub.add_parser("wait", help="print one line per new mention/work item (Monitor)")
    wait.add_argument("--once", action="store_true", help="poll once and exit")
    wait.add_argument(
        "--first", action="store_true", help="exit after the first delivery (a background task)"
    )
    tell = sub.add_parser("tell", help="send a work item")
    tell.add_argument("to", help='Handle, @Handle, @"Two Words", role:<role> or project')
    tell.add_argument("text", nargs="?")
    tell.add_argument("--file", help="read the text from this file")
    tell.add_argument("--kind", default="instruction", choices=KINDS)
    tell.add_argument("--reply-to", dest="reply_to")
    tell.add_argument("--fyi", action="store_true", help="informational: never wakes them")
    sub.add_parser("codex-rules", help="print a Codex rules file that lets ptt skip the sandbox")
    agy = sub.add_parser("agy-rules", help="Antigravity grants so ptt and its MCP tools never ask")
    agy.add_argument("--install", action="store_true", help="merge them into agy's settings.json")
    watch = sub.add_parser("codex-watch", help="(started by the hooks) wake idle Codex")
    watch.add_argument("--pid", type=int, default=0, help="the Codex process to follow")
    for name in ("ack", "done"):
        cmd = sub.add_parser(name, help=f"{name} a work item (or mention); an id prefix works")
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
    "codex-watch": cmd_codex_watch,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "codex-rules":
        cmd_codex_rules(args)  # needs no session
        return 0
    if args.command == "agy-rules":
        try:
            cmd_agy_rules(args)  # needs no session
        except (Failure, OSError) as exc:
            print(error_text(exc), file=sys.stderr)
            return 1
        return 0
    try:
        session = Session(args.session)
        COMMANDS[args.command](session, args)
    except (Failure, urllib.error.URLError, OSError) as exc:
        print(error_text(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
