#!/usr/bin/env python3
"""Drive the shared AgentsLight status light from DeepSeek Harness (DSH).

DSH persists every session to a durable, zstd-compressed JSONL event log under
``$DSH_HOME/sessions/<workspace-slug>/<session-id>/session.jsonl.zstd``. This
watcher tails those logs, translates the type-tagged event stream into the
shared ``agents-light`` CLI, and lands status files in the same
``~/.agents-status-light/sessions`` directory that Codex, Claude Code, and
OpenCode drive. No modification to DSH itself is required.

Run as a long-lived process (like the menu-bar app) or ``--once`` for cron/tests:

    python3 ~/.agents-status-light/hooks/dsh_status_light.py
    DSH_HOME=~/.dsh python3 hooks/dsh_status_light.py --once

State machine (mirrors codex_status_hook.py / claude_status_hook.py):

    session, user/message, turn/start, step/start   -> running
    tool/result without error                       -> running
    assistant/chunk, text-chunks, tool-call-chunks  -> running (streaming)
    tool/call to ask_user_question / request_user_input
        while its result is still pending           -> waiting
    tool/result with an ``error`` field             -> error
    session/end-seed, or idle turn time elapsed     -> done

Session identity: each session id is the DSH session UUID (its directory name),
so concurrent DSH sessions become separate rows. Because DSH runs under generic
``node`` (there is no stable process name to pgrep), the AgentsLight app treats a
DSH session as live while its log is being amended and lets it age out when the
turn goes quiet; no modification to AgentsLight's persisted-session handling is
required beyond the DSH-aware live signal in StatusStore.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time

_MSG_STREAMS = ("assistant/chunk", "text-chunks", "tool-call-chunks")
_MSG_RUNNING = ("user/message", "turn/start", "step/start", "tool/call", "request/header", "request/context")
_MSG_DONE = ("session/end-seed",)
_WAITING_TOOLS = ("ask_user_question", "request_user_input", "AskUserQuestion")

# ``turn/end`` reasons that mean the turn is over (green) vs. that it failed
# (red). ``completed`` and ``aborted`` are normal terminations.
_TURN_END_OK = ("completed", "aborted")
_TURN_END_ERROR = ("error", "blocked", "max-tokens")

# How long a running turn must stay silent before we emit 'done'.
IDLE_DONE_SECONDS = 12.0

# A DSH session whose log has not been written for this long is considered
# "closed" and its status row is cleared, so the light only shows sessions that
# are still open/recently active. Override via DSH_ACTIVE_WINDOW_SECONDS.
ACTIVE_WINDOW_SECONDS = float(os.environ.get("DSH_ACTIVE_WINDOW_SECONDS", "600"))


def dsh_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DSH_HOME", str(pathlib.Path.home() / ".dsh"))).expanduser()


def _shared_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("AGENTS_STATUS_LIGHT_HOME", str(pathlib.Path.home() / ".agents-status-light"))
    ).expanduser()


def command() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("AGENTS_STATUS_LIGHT_CLI", str(_shared_root() / "bin" / "agents-light"))
    ).expanduser()


def sessions_root(base: pathlib.Path | None = None) -> pathlib.Path:
    return (base or dsh_home()) / "sessions"


def log_entries(root: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """Every ``(session.jsonl.zstd, session_id)`` under the DSH sessions tree.

    DSH nests one level of workspace slugs, then one level of session ids, each
    containing a single ``session.jsonl.zstd``. We use the session directory's
    name (the DSH session UUID) as the status-light session id so concurrent
    DSH sessions become distinct rows. Some setups write the log directly under
    the workspace level; there we fall back to the workspace-id path.
    """
    entries: list[tuple[pathlib.Path, str]] = []
    try:
        for first in root.iterdir():
            if not first.is_dir():
                continue
            direct = first / "session.jsonl.zstd"
            if direct.exists():
                entries.append((direct, _sanitize(first.name)))
                continue
            for second in first.iterdir():
                if second.is_dir():
                    log = second / "session.jsonl.zstd"
                    if log.exists():
                        entries.append((log, _sanitize(second.name)))
    except OSError:
        return []
    return entries


def _sanitize(value: str) -> str:
    import re as _re
    cleaned = _re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:160] or "default"


def parse_jsonl(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


_ZSTD_CANDIDATES = (
    ("zstdcat", ()),
    ("zstd", ("-dc",)),
    ("/opt/homebrew/bin/zstdcat", ()),
    ("/usr/local/bin/zstdcat", ()),
    ("/opt/homebrew/bin/zstd", ("-dc",)),
    ("/usr/local/bin/zstd", ("-dc",)),
)


def decode_log(path: pathlib.Path) -> list[dict]:
    """Decode a session.jsonl.zstd into a list of event dicts (empty on failure).

    ``zstdcat`` is often only on a Homebrew PATH that a Login LaunchAgent does
    not export, so we probe several candidate decompressors, including absolute
    Homebrew paths and ``zstd -dc``.
    """
    if not path.exists():
        return []
    last_error: str | None = None
    for cmd, flags in _ZSTD_CANDIDATES:
        try:
            result = subprocess.run(
                [cmd, *flags, str(path)], capture_output=True, text=True, timeout=30
            )
        except Exception as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0 and result.stdout:
            return parse_jsonl(result.stdout)
        last_error = result.stderr.strip() or f"zstd rc={result.returncode}"
    _log(f"decode_log: all zstd decompressors failed for {path}: {last_error}")
    return []


def event_epoch(e: dict) -> float:
    ts = e.get("time")
    return (ts / 1000.0) if isinstance(ts, (int, float)) and ts else 0.0


def _log(message: str) -> None:
    try:
        sys.stderr.write(f"{dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')} dsh_status_light: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _tool_result_call_ids(data) -> list[str]:
    ids = []
    if not isinstance(data, dict):
        return ids
    msg = data.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool-result" and block.get("toolCallId"):
            ids.append(block["toolCallId"])
    return ids


def _open_question_call_ids(events: list[dict]) -> set[str]:
    """Call ids of question-supplying tools that have not yet been answered.

    A wait is only surfaced for a question tool whose result has not landed;
    a resolved question tool is treated like any completed tool call.
    """
    open_ids = set()
    for e in events:
        data = e.get("data", {})
        if e.get("type") == "tool/call":
            if data.get("name") in _WAITING_TOOLS and data.get("callId"):
                open_ids.add(data["callId"])
        elif e.get("type") == "tool/result":
            for cid in _tool_result_call_ids(data):
                open_ids.discard(cid)
    return open_ids


def _pending_approval_ids(events: list[dict]) -> set[str]:
    """Approval ids that have been asked but not yet decided.

    DSH emits ``approval/asked`` when the model requests a sandbox/escalation
    approval and ``approval/decided`` when the user (or an automation) answers;
    the status light shows yellow while an approval id is still pending.
    """
    pending: set[str] = set()
    for e in events:
        data = e.get("data", {})
        if e.get("type") == "approval/asked":
            if isinstance(data, dict) and data.get("id"):
                pending.add(data["id"])
        elif e.get("type") == "approval/decided":
            if isinstance(data, dict) and data.get("id"):
                pending.discard(data["id"])
    return pending


def status_for(events: list[dict]) -> tuple[str, str, bool, float]:
    """Reduce a DSH event log to ``(state, message, is_streaming, last_ts)``.

    A single tool failure is *transient*: in real DSH sessions tools fail and
    the model keeps working, so we only surface red when a tool error is the
    latest meaningful event (i.e. the turn settled on a failure) or the turn
    itself ended in error. If any later running step occurs after an error, the
    light returns to (blue) running.
    """
    open_questions = _open_question_call_ids(events)

    state = "running"
    streaming = False
    last_ts = 0.0
    last_was_error = False

    for e in events:
        kind = e.get("type")
        ts = event_epoch(e)
        if ts:
            last_ts = ts
        data = e.get("data", e)

        if kind == "turn/end":
            # A real end marker is more reliable than the idle heuristic: once
            # the turn finishes and before the user's next prompt, the light
            # turns green (or red if the turn failed). A later user/message or
            # turn/start resets it to running.
            reason = (data.get("reason") or {}).get("kind") if isinstance(data, dict) else None
            if reason in _TURN_END_ERROR:
                state = "error"
                streaming = False
                last_was_error = True
            else:
                state = "done"
                streaming = False
                last_was_error = False
        elif kind in _MSG_DONE:
            state = "done"
            streaming = False
            last_was_error = False
        elif kind == "tool/result":
            for cid in _tool_result_call_ids(data):
                open_questions.discard(cid)
            if isinstance(data, dict) and data.get("error"):
                state = "error"
                streaming = False
                last_was_error = True
            else:
                state = "running"
                last_was_error = False
        elif kind in _MSG_STREAMS:
            state = "running"
            streaming = True
            last_was_error = False
        elif kind in _MSG_RUNNING:
            state = "running"
            last_was_error = False

    # If the turn ended on a tool error (no later activity), surface the error;
    # otherwise a transient tool error followed by more work stays running.
    if last_was_error and state == "error":
        return "error", _message_for("error"), False, last_ts

    # Waiting on the user: an unanswered question *or* a sandbox approval that
    # is still pending trumps running, turning the light yellow.
    pending_approvals = _pending_approval_ids(events)
    if state not in ("error", "done") and (open_questions or pending_approvals):
        state = "waiting"
        streaming = False
        message = _approval_header(events, pending_approvals) if pending_approvals else _question_header(events)
        return state, message, streaming, last_ts
    if state in ("error", "done", "waiting"):
        streaming = False
    return state, _message_for(state), streaming, last_ts


def _message_for(state: str) -> str:
    return {
        "waiting": "DSH needs input",
        "error": "DSH tool failed",
        "done": "DSH turn completed",
        "running": "DSH is working",
    }.get(state, "DSH is working")


def _question_header(events: list[dict]) -> str:
    for e in reversed(events):
        if e.get("type") != "tool/call":
            continue
        data = e.get("data", {})
        if data.get("name") not in _WAITING_TOOLS:
            continue
        try:
            args = json.loads(data.get("arguments") or "{}")
        except Exception:
            args = {}
        questions = args.get("questions") if isinstance(args.get("questions"), list) else []
        q0 = questions[0] if questions else {}
        text = ""
        for probe in (args.get("question"), args.get("header"), args.get("message"),
                      q0.get("question"), q0.get("header"), q0.get("message")):
            if isinstance(probe, str) and probe.strip():
                text = probe.strip()
                break
        header = text.splitlines()[0] if text else "DSH is asking something"
        if len(header) > 40:
            header = header[:37] + "..."
        return header
    return "DSH needs input"


def _approval_header(events: list[dict], pending_ids: set[str]) -> str:
    """Short label for a pending sandbox/approval request."""
    for e in reversed(events):
        data = e.get("data", {})
        if e.get("type") == "approval/asked" and isinstance(data, dict) and data.get("id") in pending_ids:
            reason = data.get("reason") or ""
            tool = data.get("toolName") or "tool"
            if reason:
                line = reason.splitlines()[0].strip()
                if len(line) > 34:
                    line = line[:31] + "..."
                return f"Approval: {line}"
            return f"Approval needed: {tool}"
    return "DSH needs approval"


def session_cwd(events: list[dict]) -> str:
    for e in events:
        if e.get("type") != "session":
            continue
        # DSH stores cwd at the top level of the session record, not under data.
        if isinstance(e.get("cwd"), str) and e["cwd"]:
            return e["cwd"]
        data = e.get("data", {})
        if isinstance(data, dict) and isinstance(data.get("cwd"), str) and data["cwd"]:
            return data["cwd"]
    return ""


def emit(state: str, session_id: str, message: str, is_streaming: bool = False, cwd: str = "") -> None:
    cli = command()
    if not cli.exists():
        _log(f"emit: CLI not found at {cli}")
        return
    args = [str(cli), state, "--session", session_id, "--message", message,
            "--source", "dsh", "--quiet"]
    if cwd:
        args += ["--cwd", cwd]
    if is_streaming:
        args.append("--streaming")
    try:
        # Synchronous: this process is a dedicated watcher, so a short blocking
        # write is safer than an async child that can race our own exit.
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        _log(f"emit: state={state} session={session_id} streaming={is_streaming} rc={result.returncode}")
    except Exception as exc:
        _log(f"emit: spawn error {exc}")


def clear(session_id: str) -> None:
    """Remove a DSH session's status row (the user only wants open sessions)."""
    cli = command()
    if not cli.exists():
        return
    try:
        subprocess.run(
            [str(cli), "--clear", "--session", session_id, "--quiet"],
            capture_output=True, timeout=10,
        )
    except Exception as exc:
        _log(f"clear: spawn error {exc}")


def process_log(log_path: pathlib.Path, session_id: str, seen: dict, cache: dict) -> None:
    try:
        st = log_path.stat()
        marker = (st.st_mtime, st.st_size)
    except OSError:
        return
    if seen.get(log_path) == marker:
        return

    events = decode_log(log_path)
    if not events:
        seen[log_path] = marker
        return

    now = time.time()
    state, message, streaming, last_ts = status_for(events)

    # Only show sessions that are still open/recently active. A DSH session
    # whose log has not changed for a while is closed, so remove its status row
    # instead of surfacing an old session among the open ones.
    if not last_ts or (now - last_ts) > ACTIVE_WINDOW_SECONDS:
        if seen.get(log_path) != marker:
            clear(session_id)
        seen[log_path] = marker
        cache.pop(str(log_path), None)
        return

    # Idle turn completion: if a session went quiet a while ago it is done,
    # regardless of whether its last surface state was running or error. A live
    # DSH session emits events continuously, so only truly silent logs age out.
    if (now - last_ts) > IDLE_DONE_SECONDS:
        state = "done"
        message = "DSH turn completed"
        streaming = False

    prev = cache.get(str(log_path))
    if prev == (state, message):
        seen[log_path] = marker
        return
    cache[str(log_path)] = (state, message)
    emit(state, session_id, message, streaming, cwd=session_cwd(events))
    seen[log_path] = marker


def main() -> int:
    parser = argparse.ArgumentParser(prog="dsh_status_light")
    parser.add_argument("--dsh-home", default=str(dsh_home()))
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--once", action="store_true", help="Scan once and exit.")
    args = parser.parse_args()

    root = sessions_root(pathlib.Path(args.dsh_home).expanduser())
    if not root.is_dir():
        _log(f"dsh sessions dir not found: {root}")
        return 2

    seen: dict = {}
    cache: dict = {}

    def full_scan():
        for log, session_id in log_entries(root):
            process_log(log, session_id, seen, cache)

    if args.once:
        full_scan()
        return 0

    _log(f"watching {root}")
    try:
        while True:
            full_scan()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())