#!/usr/bin/env python3
"""Install the app support files, hooks, CLI, and personal skill safely."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import plistlib
import shutil


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--codex-home", default=str(pathlib.Path.home() / ".codex"))
    parser.add_argument("--skills-home", default=str(pathlib.Path.home() / ".agents" / "skills"))
    parser.add_argument("--app-path", default=str(pathlib.Path.home() / "Applications" / "Codex Status Light.app"))
    parser.add_argument("--no-launch-agent", action="store_true")
    args = parser.parse_args()

    root = pathlib.Path(args.project_root).resolve()
    codex_home = pathlib.Path(args.codex_home).expanduser().resolve()
    install = codex_home / "status-light"
    skills_home = pathlib.Path(args.skills_home).expanduser().resolve()
    install.mkdir(parents=True, exist_ok=True)
    skills_home.mkdir(parents=True, exist_ok=True)

    for name in ("bin", "hooks"):
        destination = install / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(root / name, destination)
    (install / "sessions").mkdir(exist_ok=True)
    for executable in (install / "bin").iterdir():
        executable.chmod(executable.stat().st_mode | 0o755)

    hooks_path = codex_home / "hooks.json"
    if hooks_path.exists():
        existing = json.loads(hooks_path.read_text(encoding="utf-8"))
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(hooks_path, hooks_path.with_suffix(f".json.bak-{stamp}"))
    else:
        existing = {}
    addition = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    hooks_path.write_text(json.dumps(merge_hooks(existing, addition), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    skill_source = root / "skill" / "codex-status-light"
    skill_destination = skills_home / "codex-status-light"
    if skill_destination.exists() or skill_destination.is_symlink():
        if skill_destination.is_dir() and not skill_destination.is_symlink():
            shutil.rmtree(skill_destination)
        else:
            skill_destination.unlink()
    skill_destination.symlink_to(skill_source, target_is_directory=True)

    launch_agent = None
    if not args.no_launch_agent:
        launch_agents = pathlib.Path.home() / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True, exist_ok=True)
        launch_agent = launch_agents / "com.local.codex-status-light.plist"
        payload = {
            "Label": "com.local.codex-status-light",
            "ProgramArguments": ["/usr/bin/open", "-a", str(pathlib.Path(args.app_path).expanduser())],
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
