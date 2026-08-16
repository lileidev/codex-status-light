#!/usr/bin/env python3
"""Install the DeepSeek Harness (DSH) integration for AgentsLight.

DSH has no command hook like Codex/Claude, so instead of registering lifecycle
hooks we deploy a small watcher that tails DSH's durable session logs
(``$DSH_HOME/sessions/**/session.jsonl.zstd``) and drives the shared
``agents-light`` CLI. It writes one status file per DSH session into the shared
``~/.agents-status-light/sessions`` directory the menu-bar app already watches,
so DSH rows appear side-by-side with Codex, Claude, and OpenCode.

Usage:

    python3 scripts/install_dsh.py                 # install support + LaunchAgent
    python3 scripts/install_dsh.py --no-launch    # install only (run manually)
    python3 scripts/install_dsh.py --dry-run      # preview paths

After installation the LauncherAgent runs the watcher continuously. If you
prefer to run it by hand:

    ~/.agents-status-light/hooks/dsh_status_light.py
"""

from __future__ import annotations

import argparse
import pathlib
import plistlib
import shutil
import sys

DSH_HOME_DEFAULT = pathlib.Path.home() / ".dsh"


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--install-root", default=str(pathlib.Path.home() / ".agents-status-light"))
    parser.add_argument("--dsh-home", default=str(DSH_HOME_DEFAULT))
    parser.add_argument("--no-launch", action="store_true",
                        help="Do not install the login LaunchAgent; install files only.")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be done.")
    args = parser.parse_args()

    root = pathlib.Path(args.project_root).resolve()
    install = pathlib.Path(args.install_root).expanduser().resolve()
    dsh_home = pathlib.Path(args.dsh_home).expanduser().resolve()

    if not (dsh_home / "sessions").is_dir():
        warn(f"warning: DSH sessions dir not found at {dsh_home / 'sessions'}. "
             "Start/use DSH once so it persists a session, then re-run this installer.")

    def act(src: pathlib.Path, dest: pathlib.Path, executable: bool = False) -> None:
        if args.dry_run:
            print(f"  copy {src} -> {dest}")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if executable:
            dest.chmod(0o755)

    # Shared CLI + DSH bridge under the shared install root.
    act(root / "bin" / "agents-light", install / "bin" / "agents-light", executable=True)
    act(root / "hooks" / "dsh_status_light.py", install / "hooks" / "dsh_status_light.py", executable=True)
    if not args.dry_run:
        (install / "sessions").mkdir(parents=True, exist_ok=True)

    # Login LaunchAgent to keep the watcher running.
    launch_label = "com.local.dsh-status-light"
    launch_plist = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{launch_label}.plist"
    interpreter = "/usr/bin/python3"
    script = install / "hooks" / "dsh_status_light.py"
    if not args.no_launch:
        payload = {
            "Label": launch_label,
            "ProgramArguments": [interpreter, str(script)],
            "RunAtLoad": True,
            "KeepAlive": True,
            # Login LaunchAgents start with a minimal PATH that does not include
            # Homebrew (where the zstd decompressor lives). Extend it so the
            # watcher can find zstdcat / zstd.
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            },
            "StandardOutPath": str(install / "dsh_status_light.out.log"),
            "StandardErrorPath": str(install / "dsh_status_light.err.log"),
            # Run in the user GUI (Aqua) session, not the generic Background
            # daemon session: from a Background launchd job, `/usr/bin/open` to
            # launch the GUI app takes ~15-20s to attach, but from the user
            # session it is ~1s. DSH auto-lanch of the status-light app needs the
            # poor man's user session to be quick.
            "LimitLoadToSessionType": "Aqua",
        }
        if args.dry_run:
            print(f"  would write LaunchAgent {launch_plist}")
        else:
            launch_plist.parent.mkdir(parents=True, exist_ok=True)
            launch_plist.write_bytes(
                plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
            )
    elif not args.dry_run and launch_plist.exists():
        launch_plist.unlink()

    print(f"installed_cli={install / 'bin' / 'agents-light'}")
    print(f"installed_hook={script}")
    print(f"dsh_home={dsh_home}")
    if args.no_launch or args.dry_run:
        print("Start the watcher manually:")
        print(f"    python3 {script}")
    elif not args.dry_run:
        print(f"installed_launch_agent={launch_plist}  (logind launchctl will start it at login)")
    print("After this, restart AgentsLight; DSH sessions will appear alongside Codex/Claude/OpenCode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())