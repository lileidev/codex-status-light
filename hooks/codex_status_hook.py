#!/usr/bin/env python3
"""Translate Codex lifecycle hook JSON into status-light updates."""

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

# In-memory state mirrors opencode-status-light's JS plugin state machine.
# Keys are session IDs.
_current_state: dict[str, str] = {}
_current_streaming: dict[str, bool] = {}
_awaiting_input: set[str] = set()
_last_tool_time: dict[str, float] = {}

# Cached app-running probe to avoid pgrep on every event.
_app_running_cache: tuple[bool, float] | None = None
_app_running_lock = threading.Lock()


def install_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "CODEX_STATUS_LIGHT_HOME",
            str(pathlib.Path.home() / ".codex" / "status-light"),
        )
    ).expanduser()


def _log(message: str) -> None:
    """Append a timestamped line to the hook log for debugging."""
    try:
        log_path = install_root() / "hook.log"
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
    """Return True if the menu-bar app is currently running.

    Result is cached for a few seconds to avoid spawning pgrep on every event.
    """
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
    """Launch the menu-bar app if it is not already running.

    Uses a short cooldown lock to avoid repeated open(1) calls when Codex fires
    several lifecycle events in quick succession. The running check is cached
    for a few seconds, and open(1) is started asynchronously so the hook does
    not block Codex.
    """
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
        # Asynchronous launch: do not block Codex waiting for the app to start.
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
        # Mark the app as running in the cache so the next events skip pgrep.
        with _app_running_lock:
            _app_running_cache = (True, time.time())
        _log("ensure_app_running: lock touched")
    except Exception as exc:
        _log(f"ensure_app_running: lock touch error {exc}")


def _parent_process_name() -> str | None:
    """Return the executable name of the parent process, e.g. ``codex``.

    Uses `ps -o comm=` (which on macOS yields the executable path). We return
    the trailing basename so callers can compare against a bare process name;
    a hook never mistakes a `~/.codex` path in a shell command for an actual
    Codex process because we only read the parent's own executable, not argv.
    """
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(os.getppid()), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value.rsplit("/", 1)[-1] if value else None
    except Exception:
        return None


def _session_id(event: dict) -> str:
    """Return a stable session ID for this Codex invocation.

    Order of preference:
      1. ``CODEX_SESSION_ID`` env var — Codex injects this and it is stable for
         the whole of a single Codex session, so every hook event maps to one
         row. (Codex's ``event.session_id`` alone is NOT stable across events,
         which previously split one window into several status rows.)
      2. The parent process PID, when the parent really is a Codex process —
         lets the Swift app clean up rows once that process exits.
      3. Codex's ``session_id`` from the event, as a last resort.
    """
    env_id = os.environ.get("CODEX_SESSION_ID")
    if env_id:
        return str(env_id)
    try:
        if _parent_process_name() == "codex":
            # The parent is the Codex process itself, so its PID is a stable
            # per-window id: every hook event of one Codex run maps to one row.
            return str(os.getppid())
    except Exception as exc:
        _log(f"_session_id: ppid probe error {exc}")

    session = event.get("session_id")
    if session:
        return str(session)
    return "manual"


def _question_header(event: dict) -> str:
    """Extract a short question header from user-input tool arguments."""
    args = event.get("tool_args") or event.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    text = (
        args.get("message")
        or args.get("question")
        or args.get("prompt")
        or args.get("content")
        or ""
    )
    header = text.strip().splitlines()[0] if text else ""
    if len(header) > 40:
        header = header[:37] + "..."
    return header or "Question"


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
    """The directory the shared CLI writes to (what the app's StatusStore watches)."""
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
    """Return the most recently written state for a session, if known."""
    cached = _current_state.get(session_id)
    if cached is not None:
        return cached
    path = _state_file_path(session_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("state")
    except Exception:
        return None


def emit(state: str, message: str, event: dict, is_streaming: bool = False) -> None:
    """Write a state update asynchronously so the Codex hook does not block."""
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
        "--source", "codex",
        "--quiet",
    ]
    if event.get("turn_id"):
        args.extend(["--turn", str(event["turn_id"])])
    if is_streaming:
        args.append("--streaming")

    _log(f"emit: state={state} session={session_id} streaming={is_streaming}")
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        _log(f"emit: spawn error {exc}")


def set_state(session_id: str, state: str, message: str, event: dict, is_streaming: bool = False) -> None:
    """Write a state update, mirroring opencode-status-light's setState guards."""
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
    response = event.get("tool_response") or event.get("tool_result") or {}
    if isinstance(response, dict):
        if response.get("is_error") is True:
            return True
        if response.get("exit_code") not in (None, 0):
            return True
    text = json.dumps(response, ensure_ascii=False).lower()
    return any(marker in text for marker in ('"is_error": true', '"exit_code": 1', "fatal:", "traceback"))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception as exc:
        _log(f"main: failed to parse stdin: {exc}")
        return 0

    name = event.get("hook_event_name", "")
    _log(f"main: event={name}")

    # Make sure the status-light app is running whenever Codex is active.
    # SessionStart is the first hook fired, so this also covers "Codex started".
    ensure_app_running()

    session_id = _session_id(event)
    tool = event.get("tool_name") or event.get("tool") or "tool"

    if name == "SessionStart":
        set_state(session_id, "running", "Codex is working", event, is_streaming=False)
    elif name == "UserPromptSubmit":
        # A new user prompt means the user has answered a waiting question or
        # granted a permission request. Clear the awaiting-input guard so the
        # light transitions from yellow back to blue immediately.
        _awaiting_input.discard(session_id)
        # Codex has no message.part.updated event; approximate streaming by marking
        # the running state as streaming until the first PostToolUse arrives.
        set_state(session_id, "running", "Codex is working", event, is_streaming=True)
    elif name == "PermissionRequest":
        _awaiting_input.add(session_id)
        set_state(session_id, "waiting", f"Approval needed: {tool}", event)
    elif name == "PreToolUse" and str(tool) in {"request_user_input", "RequestUserInput"}:
        header = _question_header(event)
        _awaiting_input.add(session_id)
        set_state(session_id, "waiting", f"Question: {header}", event)
    elif name == "PostToolUse":
        if failed_tool(event):
            set_state(session_id, "error", f"Tool failed: {tool}", event)
        elif current_state(session_id) == "waiting" or session_id in _awaiting_input:
            _awaiting_input.discard(session_id)
            set_state(session_id, "running", "Codex is working", event, is_streaming=False)
        else:
            now = time.time() * 1000
            if now - _last_tool_time.get(session_id, 0) > _TOOL_THROTTLE_MS:
                set_state(session_id, "running", "Codex is working", event, is_streaming=False)
            else:
                _log(f"PostToolUse: throttled for session {session_id}")
            _last_tool_time[session_id] = now
    elif name == "Stop":
        _current_streaming[session_id] = False
        _awaiting_input.discard(session_id)
        if current_state(session_id) != "error":
            set_state(session_id, "done", "Codex turn completed", event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
