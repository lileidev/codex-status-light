#!/usr/bin/env python3
"""Install the Codex status-light integration.

Installs the shared CLI and the Codex hook under `~/.agents-status-light`, and
merges Codex hook events into `~/.codex/hooks.json` (after a timestamped backup
when necessary). The AgentsLight menu-bar app is launched on demand by these
hooks, so no login LaunchAgent is needed by default.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import plistlib
import shutil
import subprocess

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PreToolUse", "PostToolUse", "Stop")


def merge_hooks(existing: dict, addition: dict) -> dict:
    result = dict(existing)
    hooks = dict(result.get("hooks", {}))
    for event in HOOK_EVENTS:
        current = list(hooks.get(event, []))
        incoming = addition.get("hooks", {}).get(event, [])
        current = [item for item in current if "codex_status_hook.py" not in json.dumps(item)]
        hooks[event] = current + incoming
    result["hooks"] = hooks
    return result


def absolutify_hook_commands(config: dict, python_path: pathlib.Path, hook_script: pathlib.Path) -> dict:
    """Rewrite hook commands to use absolute interpreter and script paths.

    Codex may run hooks with a minimal PATH that does not include `python3`,
    and it may not expand `~` in command strings. Absolute paths avoid both.
    """
    absolute_command = f"{python_path} {hook_script}"
    copied = json.loads(json.dumps(config))

    def replace(obj):
        if isinstance(obj, dict):
            if obj.get("type") == "command" and "codex_status_hook.py" in obj.get("command", ""):
                obj["command"] = absolute_command
            else:
                for value in obj.values():
                    replace(value)
        elif isinstance(obj, list):
            for item in obj:
                replace(item)

    replace(copied)
    return copied


def remove_legacy_launch_agent() -> None:
    """Unload and delete any previously installed login LaunchAgent."""
    launch_agent = pathlib.Path.home() / "Library" / "LaunchAgents" / "com.local.codex-status-light.plist"
    if not launch_agent.exists():
        return
    subprocess.run(["launchctl", "unload", str(launch_agent)], capture_output=True)
    try:
        launch_agent.unlink()
        print(f"removed_legacy_launch_agent={launch_agent}")
    except Exception as exc:
        print(f"warning: failed to remove legacy launch agent: {exc}", file=__import__("sys").stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--codex-home", default=str(pathlib.Path.home() / ".codex"))
    parser.add_argument("--install-root", default=str(pathlib.Path.home() / ".agents-status-light"))
    parser.add_argument("--skills-home", default=str(pathlib.Path.home() / ".agents" / "skills"))
    parser.add_argument(
        "--launch-agent",
        action="store_true",
        help="Install a login LaunchAgent that starts the app at login (legacy behavior).",
    )
    args = parser.parse_args()

    root = pathlib.Path(args.project_root).resolve()
    codex_home = pathlib.Path(args.codex_home).expanduser().resolve()
    install = pathlib.Path(args.install_root).expanduser().resolve()
    skills_home = pathlib.Path(args.skills_home).expanduser().resolve()
    install.mkdir(parents=True, exist_ok=True)

    remove_legacy_launch_agent()

    # Shared CLI.
    bin_dir = install / "bin"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
    bin_dir.mkdir(parents=True)
    shutil.copy2(root / "bin" / "agents-light", bin_dir / "agents-light")
    (bin_dir / "agents-light").chmod(0o755)

    # Codex hook (shared root's hooks dir, distinct filename from Claude's).
    hooks_dir = install / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "hooks" / "codex_status_hook.py", hooks_dir / "codex_status_hook.py")

    (install / "sessions").mkdir(exist_ok=True)

    hooks_path = codex_home / "hooks.json"
    if hooks_path.exists():
        existing = json.loads(hooks_path.read_text(encoding="utf-8"))
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(hooks_path, hooks_path.with_suffix(f".json.bak-{stamp}"))
    else:
        existing = {}
    addition = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    merged = merge_hooks(existing, addition)
    merged = absolutify_hook_commands(
        merged,
        python_path=pathlib.Path("/usr/bin/python3"),
        hook_script=install / "hooks" / "codex_status_hook.py",
    )
    hooks_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    skill_source = root / "skill" / "codex-status-light"
    skill_destination = skills_home / "codex-status-light"
    if skill_destination.exists() or skill_destination.is_symlink():
        if skill_destination.is_dir() and not skill_destination.is_symlink():
            shutil.rmtree(skill_destination)
        else:
            skill_destination.unlink()
    skill_destination.symlink_to(skill_source, target_is_directory=True)

    launch_agent = None
    if args.launch_agent:
        launch_agents = pathlib.Path.home() / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True, exist_ok=True)
        launch_agent = launch_agents / "com.local.codex-status-light.plist"
        payload = {
            "Label": "com.local.codex-status-light",
            "ProgramArguments": ["/usr/bin/open", "-a", "AgentsLight"],
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        }
        launch_agent.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))

    print(f"installed_support={install}")
    print(f"installed_hooks={hooks_path}")
    print(f"installed_skill={skill_destination}")
    if launch_agent is not None:
        print(f"installed_launch_agent={launch_agent}")
    print("Open Codex /hooks to review and trust the new command hooks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())