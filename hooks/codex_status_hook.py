#!/usr/bin/env python3
"""Translate Codex lifecycle hook JSON into status-light updates."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys


def install_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "CODEX_STATUS_LIGHT_HOME",
            str(pathlib.Path.home() / ".codex" / "status-light"),
        )
    ).expanduser()


def emit(state: str, message: str, event: dict) -> None:
    command = install_root() / "bin" / "codex-status-light"
    if not command.exists():
        return
    args = [
        str(command), state,
        "--session", str(event.get("session_id") or "unknown"),
        "--message", message,
        "--cwd", str(event.get("cwd") or os.getcwd()),
        "--source", f"hook:{event.get('hook_event_name', 'unknown')}",
    ]
    if event.get("turn_id"):
        args.extend(["--turn", str(event["turn_id"])])
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
