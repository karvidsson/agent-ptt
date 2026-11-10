#!/usr/bin/env python3
"""ptt: talk to the other agents in this session's Agent PTT channel and project.

Stdlib only; runs on macOS, Linux and Windows next to the plugin's hooks. It
finds this session's channel key and project membership in the hook state under
``~/.agent-ptt`` (written by session_start.py and receive.py), so no keys or IDs
need to be pasted. The session is ``--session``, else ``AGENT_PTT_SESSION_ID`` /
``CODEX_THREAD_ID`` / ``CLAUDE_SESSION_ID``, else the most recently active
hooked session whose folder contains (or is inside) the current directory.

  ptt.py say "<text with @Theo>" [--quiet]   speak (or with --quiet, post text) in the channel
  ptt.py who                                 who is here: harness, machine, doing, presence
  ptt.py inbox                               pending mentions and work items (not acked)
  ptt.py wait [--once]                       print one line per new mention/work item, forever
  ptt.py tell <to> "<text>" [--kind K]       send a work item: Handle, role:<r> or project
  ptt.py ack <id> | done <id> [--note N]     acknowledge / finish a mention or work item
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
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
        return body

    try:
        _, message = session.channel_call("POST", "/channels/{cid}/say", payload)
    except urllib.error.HTTPError as exc:
        raise Failure(f"say failed: {_detail(exc)}") from None
    message = message or {}
    print(f"[ptt] said (message {message.get('message_id', '?')})")
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
    try:
        item = session.project_call("POST", "/projects/{pid}/items", payload)
    except urllib.error.HTTPError as exc:
        raise Failure(f"tell failed: {_detail(exc)}") from None
    to = item.get("to_label") or item.get("to")
    print(f"[ptt] sent {item.get('kind')} to {to} (item {item.get('item_id')})")


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
    """Two long-poll loops (channel mentions, project inbox) printing one line per item."""

    def __init__(self, session: Session, out=None, wait: int = WAIT_SECONDS):
        self.session = session
        self.out = out or sys.stdout
        self.wait = wait
        self.print_lock = threading.Lock()
        self.printed: set[str] = set()
        self.cursor = int(session.member.get("cursor") or 0)
        self.heartbeat = None  # called every 15 s while running (keeps the lock fresh)

    def emit(self, line: str) -> None:
        with self.print_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def channel_once(self, wait: int) -> int:
        state = self.session.channel()
        # Only servers advertising wait_supported long-poll; others answer at once.
        wait = (
            min(wait, int(state.get("wait_max_seconds") or 0)) if state.get("wait_supported") else 0
        )
        _, messages = self.session.channel_call(
            "GET", f"/channels/{{cid}}/inbox?limit=20&wait={wait}", timeout=wait + 15
        )
        fresh = [m for m in messages or [] if m.get("message_id") not in self.printed]
        for message in fresh:
            self.emit(link.mention_line(message))
            self.printed.add(message["message_id"])
        ids = [m["message_id"] for m in messages or []]
        if ids:  # printed (now or earlier): acked so receive.py doesn't inject them again
            self.session.channel_call("POST", "/channels/{cid}/inbox/ack", {"message_ids": ids})
        return len(fresh)

    def project_once(self, wait: int) -> int:
        data = self.session.project_call(
            "GET",
            f"/projects/{{pid}}/inbox?limit=50&wait={wait}&after={self.cursor}",
            timeout=wait + 15,
        )
        items = data.get("items") or []
        fresh = [i for i in items if i.get("item_id") not in self.printed]
        for item in fresh:
            self.emit(link.item_line(item))
            self.printed.add(item["item_id"])
        for item in items:
            # An item finished meanwhile can't be acked; it was printed, not repeated.
            with contextlib.suppress(urllib.error.HTTPError):
                self.session.project_call("POST", f"/projects/{{pid}}/items/{item['item_id']}/ack")
        self.cursor = max(self.cursor, int(data.get("cursor") or 0))
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
        while not stop.is_set() and any(t.is_alive() for t in threads):
            stop.wait(15)
            if self.heartbeat:
                self.heartbeat()


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
            if age < LOCK_STALE:
                return None
            path.unlink(missing_ok=True)
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path
    return None


def cmd_wait(session: Session, args) -> None:
    waiter = Waiter(session)
    if args.once:
        count = 0
        for poll in (waiter.channel_once, waiter.project_once):
            with contextlib.suppress(Failure):
                count += poll(0)
        if not count:
            print("[ptt] nothing new")
        return
    lock = _wait_lock(session)
    if lock is None:
        print("[ptt] already waiting for this session in another process", flush=True)
        return
    waiter.heartbeat = lambda: os.utime(lock)
    me = session.member.get("handle") or session.agent
    waiter.emit(f"[ptt] waiting for mentions and work items as {me}")
    try:
        waiter.run()
    except KeyboardInterrupt:
        pass
    finally:
        lock.unlink(missing_ok=True)
        if session.member_path and session.member:
            session.member["cursor"] = waiter.cursor
            link.save_membership(session.member_path, session.member)


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
    sub.add_parser("who", help="who is in the project and channel")
    sub.add_parser("inbox", help="pending mentions and work items")
    wait = sub.add_parser("wait", help="print one line per new mention/work item (Monitor)")
    wait.add_argument("--once", action="store_true", help="poll once and exit")
    tell = sub.add_parser("tell", help="send a work item")
    tell.add_argument("to", help='Handle, @Handle, @"Two Words", role:<role> or project')
    tell.add_argument("text")
    tell.add_argument("--kind", default="instruction", choices=KINDS)
    tell.add_argument("--reply-to", dest="reply_to")
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
