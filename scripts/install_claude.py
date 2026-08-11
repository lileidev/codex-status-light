#!/usr/bin/env python3
"""Install the Claude Code status-light integration.

Mirrors `install.py` but for Claude Code: it installs the shared CLI and the
Claude hook under `~/.agents-status-light`, and merges the hook events into
`~/.claude/settings.json` (after a timestamped backup). The menu-bar app, CLI,
and state directory are shared with the Codex integration, so the two installers
write into the same root (adding distinct hook files) and no separate app
install is needed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil

# Events that mirror the Codex integration's state machine, mapped to Claude
# Code's hook vocabulary. PermissionRequest and Notification cover Claude's
# "waiting for user" states; PostToolUse covers running/error; Stop covers done.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "Notification", "PostToolUse", "Stop")


def merge_hooks(existing: dict, addition: dict) -> dict:
    result = dict(existing)
    hooks = dict(result.get("hooks", {}))
    for event in HOOK_EVENTS:
        current = list(hooks.get(event, []))
        incoming = addition.get("hooks", {}).get(event, [])
        current = [item for item in current if "claude_status_hook.py" not in json.dumps(item)]
        hooks[event] = current + incoming
    result["hooks"] = hooks
    return result


def absolutify_hook_commands(config: dict, python_path: pathlib.Path, hook_script: pathlib.Path) -> dict:
    """Rewrite hook commands to use absolute interpreter and script paths."""
    absolute_command = f"{python_path} {hook_script}"
    copied = json.loads(json.dumps(config))

    def replace(obj):
        if isinstance(obj, dict):
            if obj.get("type") == "command" and "claude_status_hook.py" in obj.get("command", ""):
                obj["command"] = absolute_command
            else:
                for value in obj.values():
                    replace(value)
        elif isinstance(obj, list):
            for item in obj:
                replace(item)

    replace(copied)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--install-root", default=str(pathlib.Path.home() / ".agents-status-light"))
    parser.add_argument("--settings", default=str(pathlib.Path.home() / ".claude" / "settings.json"))
    args = parser.parse_args()

    root = pathlib.Path(args.project_root).resolve()
    install = pathlib.Path(args.install_root).expanduser().resolve()
    settings_path = pathlib.Path(args.settings).expanduser().resolve()
    bin_dir = install / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    # Shared CLI. Same program Codex uses; keeps re-running idempotent by
    # overwriting from the repo copy.
    shutil.copy2(root / "bin" / "agents-light", bin_dir / "agents-light")
    (bin_dir / "agents-light").chmod(0o755)

    # Claude hook (kept alongside the distinct Codex hook in the shared root).
    hooks_dir = install / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "hooks" / "claude_status_hook.py", hooks_dir / "claude_status_hook.py")

    (install / "sessions").mkdir(exist_ok=True)

    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings_path, settings_path.with_suffix(f".json.bak-{stamp}"))
    else:
        existing = {}
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    addition = json.loads((root / "hooks" / "claude_settings.json").read_text(encoding="utf-8"))
    merged = merge_hooks(existing, addition)
    merged = absolutify_hook_commands(
        merged,
        python_path=pathlib.Path("/usr/bin/python3"),
        hook_script=install / "hooks" / "claude_status_hook.py",
    )
    settings_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"installed_support={install}")
    print(f"installed_settings={settings_path}")
    print("Restart Claude Code; hooks run automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())