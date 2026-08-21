#!/usr/bin/env python3
"""Translate Claude Code lifecycle hook JSON into status-light updates.

Mirrors `codex_status_hook.py`'s state machine, but keyed to Claude Code hook
events and their stdin payload shape. Emits through the same shared
closely mirrors `codex_status_hook.py`'s state machine, but is keyed to Claude
Code hook events and their stdin payload shape. It emits through the shared
`agents-light` CLI, writing into a single state directory that the menu-bar app
(AgentsLight) watches, so one light serves both providers.

Claude Code delivers these events on stdin (`hook_event_name`):
  SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest,
  Notification, Stop, SubagentStop, SessionEnd, PreCompact
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

_LAUNCH_COOLDOWN_SECONDS = 10
_TOOL_THROTTLE_MS = 2000
_APP_RUNNING_CACHE_TTL_SECONDS = 5

# In-memory state, keyed by transcript/session id.
_current_state: dict[str, str] = {}
_current_streaming: dict[str, bool] = {}
_awaiting_input: set[str] = set()
_last_tool_time: dict[str, float] = {}

# Cached app-running probe to avoid pgrep on every event.
_app_running_cache: tuple[bool, float] | None = None
_app_running_lock = threading.Lock()

# Notification subtypes in Claude Code that indicate Claude is blocked on the
# user (asking a question or waiting for an approval), rather than progressing.
_WAITING_SUBTYPES = {"blocking", "question", "permission", "user-input", "request-permission", "needs-attention"}
_ERROR_SUBTYPES = {"error"}


def install_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "CLAUDE_STATUS_LIGHT_HOME",
            str(pathlib.Path.home() / ".claude" / "status-light"),
        )
    ).expanduser()


_LOG_MAX_BYTES = 512 * 1024  # rotate hook.log when it exceeds ~512 KB


def _log(message: str) -> None:
    """Append a timestamped line to the hook log, rotating it to stay bounded."""
    try:
        log_path = install_root() / "hook.log"
        try:
            if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
                rotated = log_path.with_name("hook.log.1")
                rotated.unlink(missing_ok=True)
                log_path.rename(rotated)
        except OSError:
            pass
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except Exception:
        pass


def _app_path() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "AGENTS_STATUS_LIGHT_APP",
            str(pathlib.Path.home() / "Applications" / "AgentsLight.app"),
        )
    ).expanduser()


def _launch_lock_path() -> pathlib.Path:
    return install_root() / ".launch-lock"


def _is_app_running() -> bool:
    """Return True if the menu-bar app is currently running."""
    global _app_running_cache
    with _app_running_lock:
        now = time.time()
        if _app_running_cache is not None:
            cached_value, cached_at = _app_running_cache
            if now - cached_at < _APP_RUNNING_CACHE_TTL_SECONDS:
                return cached_value

        try:
            probe = subprocess.run(
                ["/usr/bin/pgrep", "-x", "AgentsLight"],
                capture_output=True,
                timeout=2,
            )
            result = probe.returncode == 0
        except Exception as exc:
            _log(f"_is_app_running: pgrep error {exc}")
            result = False

        _app_running_cache = (result, now)
        return result


def ensure_app_running() -> None:
    """Launch the menu-bar app if it is not already running."""
    _log("ensure_app_running: start")
    if _is_app_running():
        return

    lock = _launch_lock_path()
    try:
        if lock.exists():
            elapsed = time.time() - lock.stat().st_mtime
            _log(f"ensure_app_running: lock age={elapsed:.1f}s")
            if elapsed < _LAUNCH_COOLDOWN_SECONDS:
                _log("ensure_app_running: skipped due to cooldown")
                return
    except Exception as exc:
        _log(f"ensure_app_running: lock check error {exc}")

    app = _app_path()
    if app.exists():
        cmd = ["/usr/bin/open", "-g", str(app)]
    else:
        cmd = ["/usr/bin/open", "-g", "-a", "AgentsLight"]

    _log(f"ensure_app_running: launching with {cmd}")
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        _log(f"ensure_app_running: open error {exc}")
        return

    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch(exist_ok=True)
        with _app_running_lock:
            _app_running_cache = (True, time.time())
        _log("ensure_app_running: lock touched")
    except Exception as exc:
        _log(f"ensure_app_running: lock touch error {exc}")


def _parent_process_name() -> str | None:
    """Return the executable name of the parent process, e.g. "claude".

    Uses `ps -o comm=` (just the executable name), not the full command line,
    so a hook run under Claude Code never mistakes the `~/.claude` path in a
    shell command for an actual Claude Code process.
    """
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(os.getppid()), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _session_id(event: dict) -> str:
    """Return a stable session ID for this Claude Code invocation.

    Always the Claude transcript session id (UUID). Using a PID with a UUID
    fallback was unstable across the hook events of one session (the parent-pid
    probe sometimes hits and sometimes misses), so a single interactive session
    was split into two status rows. The UUID is constant for the whole session,
    so one session = one row. The parent gate in main() still means only
    interactive Claude sessions write; stale UUID rows are pruned by the app
    (anyAgentProcessRunning + the 12h stale threshold).
    """
    session = event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if session:
        return str(session)
    return "manual"


def _question_header(event: dict) -> str:
    """Extract a short header describing why the assistant is waiting."""
    tool_input = event.get("tool_input") or event.get("input") or {}
    if isinstance(tool_input, dict):
        text = (
            tool_input.get("question")
            or tool_input.get("heading")
            or tool_input.get("message")
            or tool_input.get("prompt")
            or ""
        )
    else:
        text = ""
    if not text and event.get("notification") and isinstance(event["notification"], dict):
        text = event["notification"].get("message") or ""
    header = text.strip().splitlines()[0] if text else ""
    if len(header) > 40:
        header = header[:37] + "..."
    return header or "Claude needs input"


def _shared_root() -> pathlib.Path:
    """The shared install root both agents' hooks drive.

    The menu-bar app, CLI, and state directory live here once, shared by the
    Codex and Claude integrations. Overridable mainly for testing.
    """
    return pathlib.Path(
        os.environ.get(
            "AGENTS_STATUS_LIGHT_HOME",
            str(pathlib.Path.home() / ".agents-status-light"),
        )
    ).expanduser()


def _state_dir() -> pathlib.Path:
    """The directory the shared CLI writes to.

    Both the Codex and Claude hooks drive the same menu-bar app, so they must
    share one state directory. This matches the app's StatusStore watch target
    and the CLI's own default; an explicit override (used by tests) wins.
    """
    return pathlib.Path(
        os.environ.get(
            "AGENTS_STATUS_LIGHT_DIR",
            str(_shared_root() / "sessions"),
        )
    ).expanduser()


def _state_file_path(session_id: str) -> pathlib.Path:
    safe = "".join(character if character.isalnum() or character in "_.-" else "-" for character in session_id).strip("-.")
    return _state_dir() / f"{safe[:160] or 'default'}.json"


def _command() -> pathlib.Path:
    """Path to the shared status-light CLI."""
    return _shared_root() / "bin" / "agents-light"


def current_state(session_id: str) -> str | None:
    cached = _current_state.get(session_id)
    if cached is not None:
        return cached
    path = _state_file_path(session_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("state")
    except Exception:
        return None


def emit(state: str, message: str, event: dict, is_streaming: bool = False) -> None:
    """Write a state update to the shared status light.

    Runs synchronously so each write completes before the next event is
    handled. Claude Code invokes one hook (one event) at a time, so a
    synchronous write guarantees the on-disk status reflects the *last* event
    (e.g. yellow waiting -> blue running) in order. Forking an async subprocess
    per event previously let a stale "waiting" write finish after a newer
    "running" write and leave the light stuck on yellow. The CLI call is small
    (~30ms), well within the hook timeout.
    """
    command = _command()
    if not command.exists():
        _log(f"emit: CLI not found at {command}")
        return

    session_id = _session_id(event)
    args = [
        str(command), state,
        "--session", session_id,
        "--message", message,
        "--cwd", str(event.get("cwd") or os.getcwd()),
        "--source", f"hook:{event.get('hook_event_name', 'unknown')}",
        "--quiet",
    ]
    if event.get("turn_id"):
        args.extend(["--turn", str(event["turn_id"])])
    if is_streaming:
        args.append("--streaming")

    _log(f"emit: state={state} session={session_id} streaming={is_streaming}")
    try:
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        _log(f"emit: error {exc}")


def set_state(session_id: str, state: str, message: str, event: dict, is_streaming: bool = False) -> None:
    """Write a state update, mirroring the shared state machine's guards."""
    if session_id in _awaiting_input and state not in ("waiting", "error"):
        _log(f"set_state: ignoring {state} because session {session_id} is awaiting input")
        return

    if (
        _current_state.get(session_id) == state
        and _current_streaming.get(session_id) == is_streaming
        and state != "running"
    ):
        _log(f"set_state: dedup skipping {state} for session {session_id}")
        return

    _current_state[session_id] = state
    _current_streaming[session_id] = is_streaming
    emit(state, message, event, is_streaming=is_streaming)


def failed_tool(event: dict) -> bool:
    response = event.get("tool_response") or {}
    if isinstance(response, dict):
        if response.get("is_error") is True:
            return True
        if response.get("exit_code") not in (None, 0):
            return True
        if response.get("success") is False:
            return True
    text = json.dumps(response, ensure_ascii=False).lower()
    return any(marker in text for marker in ('"is_error": true', '"success": false', '"exit_code": 1', "fatal:", "traceback"))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception as exc:
        _log(f"main: failed to parse stdin: {exc}")
        return 0

    name = event.get("hook_event_name", "")
    _log(f"main: event={name}")

    # Skip embedded/external Claude Code sessions that are not launched by an
    # interactive `claude` process (notably Obsidian Copilot and similar tools
    # that drive Claude Code programmatically). Their parent process is not
    # `claude`, so they would otherwise clutter the status light and never be
    # pruned. Only real interactive Claude sessions are surfaced. This also
    # makes _session_id() return a PID (which the Swift app can clean up) rather
    # than a lingering transcript UUID.
    #
    # A CLAUDE_STATUS_LIGHT_PARENT env override exists so tests (and any
    # controlled harness) can simulate an interactive claude parent; normal
    # Obsidian/embedded uses never set it.
    parent = _parent_process_name()
    if os.environ.get("CLAUDE_STATUS_LIGHT_PARENT"):
        parent = os.environ["CLAUDE_STATUS_LIGHT_PARENT"]
    if parent != "claude":
        _log(f"main: skipping (parent not claude, got {parent!r}) — event={name}")
        return 0

    ensure_app_running()

    session_id = _session_id(event)
    tool = event.get("tool_name") or event.get("tool") or "tool"

    if name == "SessionStart":
        set_state(session_id, "running", "Claude is working", event, is_streaming=False)
    elif name == "UserPromptSubmit":
        # A new user prompt answers a waiting question or approval.
        _awaiting_input.discard(session_id)
        set_state(session_id, "running", "Claude is working", event, is_streaming=True)
    elif name == "PermissionRequest":
        _awaiting_input.add(session_id)
        set_state(session_id, "waiting", f"Approval needed: {tool}", event)
    elif name == "Notification":
        notification = event.get("notification") or {}
        notification = notification if isinstance(notification, dict) else {}
        # Claude Code provides the kind both as `notification.subtype` and (in
        # newer releases, e.g. 2.1.x) as top-level `notification_type` with
        # values like "permission_prompt". Read whichever is present.
        subtype = (
            notification.get("type")
            or notification.get("subtype")
            or event.get("notification_type")
            or event.get("subtype")
            or ""
        )
        if isinstance(subtype, str) and (subtype.lower() in _WAITING_SUBTYPES or subtype in {"permission_prompt", "question_prompt"}):
            _awaiting_input.add(session_id)
            set_state(session_id, "waiting", _question_header(event), event)
        elif isinstance(subtype, str) and subtype.lower() in _ERROR_SUBTYPES:
            set_state(session_id, "error", f"Claude reported: {subtype}", event)
        elif isinstance(subtype, str) and subtype.lower() == "info":
            set_state(session_id, "running", "Claude is working", event, is_streaming=True)
    elif name == "PreToolUse":
        # A question-supplying tool implies Claude is waiting on the user.
        if str(tool) in {"AskUserQuestion", "ask_user_question"}:
            _awaiting_input.add(session_id)
            set_state(session_id, "waiting", _question_header(event), event)
    elif name == "PostToolUse":
        if failed_tool(event):
            set_state(session_id, "error", f"Tool failed: {tool}", event)
        elif current_state(session_id) == "waiting" or session_id in _awaiting_input:
            _awaiting_input.discard(session_id)
            set_state(session_id, "running", "Claude is working", event, is_streaming=False)
        else:
            now = time.time() * 1000
            if now - _last_tool_time.get(session_id, 0) > _TOOL_THROTTLE_MS:
                set_state(session_id, "running", "Claude is working", event, is_streaming=False)
            else:
                _log(f"PostToolUse: throttled for session {session_id}")
            _last_tool_time[session_id] = now
    elif name in ("Stop", "SubagentStop"):
        if not event.get("stop_hook_active"):
            _current_streaming[session_id] = False
            _awaiting_input.discard(session_id)
            if current_state(session_id) != "error":
                set_state(session_id, "done", "Claude turn completed", event)
    elif name in ("SessionEnd", "PreCompact"):
        _current_streaming[session_id] = False
        _awaiting_input.discard(session_id)
        if current_state(session_id) != "error":
            set_state(session_id, "done", "Claude session ended", event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())