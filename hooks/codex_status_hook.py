#!/usr/bin/env python3
"""Translate Codex lifecycle hook JSON into status-light updates."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time


_LAUNCH_COOLDOWN_SECONDS = 10


def install_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "CODEX_STATUS_LIGHT_HOME",
            str(pathlib.Path.home() / ".codex" / "status-light"),
        )
    ).expanduser()


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
    try:
        probe = subprocess.run(
            ["pgrep", "-x", "CodexStatusLight"],
            capture_output=True,
            timeout=2,
        )
        if probe.returncode == 0:
            return
    except Exception:
        pass

    lock = _launch_lock_path()
    try:
        if lock.exists():
            elapsed = time.time() - lock.stat().st_mtime
            if elapsed < _LAUNCH_COOLDOWN_SECONDS:
                return
    except Exception:
        pass

    app = _app_path()
    if app.exists():
        cmd = ["/usr/bin/open", "-g", str(app)]
    else:
        cmd = ["/usr/bin/open", "-g", "-a", "Codex Status Light"]

    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch(exist_ok=True)
    except Exception:
        pass


def emit(state: str, message: str, event: dict, is_streaming: bool = False) -> None:
    command = install_root() / "bin" / "codex-status-light"
    if not command.exists():
        return
    args = [
        str(command), state,
        "--session", str(event.get("session_id") or "unknown"),
        "--message", message,
        "--cwd", str(event.get("cwd") or os.getcwd()),
        "--source", f"hook:{event.get('hook_event_name', 'unknown')}",
        "--quiet",
    ]
    if event.get("turn_id"):
        args.extend(["--turn", str(event["turn_id"])])
    if is_streaming:
        args.append("--streaming")
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def failed_tool(event: dict) -> bool:
    response = event.get("tool_response") or event.get("tool_result") or {}
    if isinstance(response, dict):
        if response.get("is_error") is True:
            return True
        if response.get("exit_code") not in (None, 0):
            return True
    text = json.dumps(response, ensure_ascii=False).lower()
    return any(marker in text for marker in ('"is_error": true', '"exit_code": 1', "fatal:", "traceback"))


def current_state(event: dict) -> str | None:
    session = str(event.get("session_id") or "unknown")
    safe = "".join(character if character.isalnum() or character in "_.-" else "-" for character in session).strip("-.")
    state_directory = pathlib.Path(
        os.environ.get(
            "CODEX_STATUS_LIGHT_DIR",
            str(install_root() / "sessions"),
        )
    ).expanduser()
    path = state_directory / f"{safe[:160] or 'default'}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("state")
    except Exception:
        return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    # Make sure the status-light app is running whenever Codex is active.
    # SessionStart is the first hook fired, so this also covers "Codex started".
    ensure_app_running()

    name = event.get("hook_event_name", "")
    tool = event.get("tool_name") or event.get("tool") or "tool"

    if name in {"SessionStart", "UserPromptSubmit"}:
        emit("running", "Codex is working", event)
    elif name == "PermissionRequest":
        emit("waiting", f"Approval needed: {tool}", event)
    elif name == "PreToolUse" and str(tool) in {"request_user_input", "RequestUserInput"}:
        emit("waiting", "Codex needs your input", event)
    elif name == "PostToolUse":
        if failed_tool(event):
            emit("error", f"Tool failed: {tool}", event)
        elif current_state(event) == "waiting":
            emit("running", "Codex is working", event)
    elif name == "Stop" and current_state(event) != "error":
        emit("done", "Codex turn completed", event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
