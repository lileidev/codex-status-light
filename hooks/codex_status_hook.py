#!/usr/bin/env python3
"""Translate Codex lifecycle hook JSON into status-light updates."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time


_LAUNCH_COOLDOWN_SECONDS = 10
_TOOL_THROTTLE_MS = 2000

# In-memory state mirrors opencode-status-light's JS plugin state machine.
# Keys are session IDs.
_current_state: dict[str, str] = {}
_current_streaming: dict[str, bool] = {}
_awaiting_input: set[str] = set()
_last_tool_time: dict[str, float] = {}


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
            "CODEX_STATUS_LIGHT_APP",
            str(pathlib.Path.home() / "Applications" / "Codex Status Light.app"),
        )
    ).expanduser()


def _launch_lock_path() -> pathlib.Path:
    return install_root() / ".launch-lock"


def ensure_app_running() -> None:
    """Launch the menu-bar app if it is not already running.

    Uses a short cooldown lock to avoid repeated open(1) calls when Codex fires
    several lifecycle events in quick succession.
    """
    _log("ensure_app_running: start")
    try:
        probe = subprocess.run(
            ["/usr/bin/pgrep", "-x", "CodexStatusLight"],
            capture_output=True,
            timeout=2,
        )
        _log(f"ensure_app_running: pgrep returncode={probe.returncode}")
        if probe.returncode == 0:
            return
    except Exception as exc:
        _log(f"ensure_app_running: pgrep error {exc}")

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
        cmd = ["/usr/bin/open", "-g", "-a", "Codex Status Light"]

    _log(f"ensure_app_running: launching with {cmd}")
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log(f"ensure_app_running: open returncode={result.returncode}")
    except Exception as exc:
        _log(f"ensure_app_running: open error {exc}")
        return

    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch(exist_ok=True)
        _log("ensure_app_running: lock touched")
    except Exception as exc:
        _log(f"ensure_app_running: lock touch error {exc}")


def _session_id(event: dict) -> str:
    """Return a stable session ID for this Codex invocation.

    Prefer an ID provided by Codex, then an environment variable, then the parent
    process PID. Using a PID enables the Swift app to clean up sessions whose
    owning Codex process has exited.
    """
    session = event.get("session_id") or os.environ.get("CODEX_SESSION_ID")
    if session:
        return str(session)
    try:
        return str(os.getppid())
    except Exception:
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


def _state_file_path(session_id: str) -> pathlib.Path:
    safe = "".join(character if character.isalnum() or character in "_.-" else "-" for character in session_id).strip("-.")
    state_directory = pathlib.Path(
        os.environ.get(
            "CODEX_STATUS_LIGHT_DIR",
            str(install_root() / "sessions"),
        )
    ).expanduser()
    return state_directory / f"{safe[:160] or 'default'}.json"


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
    command = install_root() / "bin" / "codex-status-light"
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
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
