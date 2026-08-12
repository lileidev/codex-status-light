#!/usr/bin/env python3
"""Install the OpenCode integration for AgentsLight and remove legacy installs.

Replaces the standalone `opencode-status-light` plugin with one that drives the
shared AgentsLight CLI, so OpenCode sessions appear in the single AgentsLight
window alongside Codex and Claude. It also removes the now-obsolete standalone
artifacts:

  - OpenCode: the old plugin, ~/.opencode/status-light (bin+sessions), the
    OpenCode Status Light.app bundle, its skill symlink, and its log dir.
  - Codex: the legacy ~/.codex/status-light dir and the codex skill symlink
    (the active Codex hook handled by AgentsLight lives under ~/.codex/hooks.json
    pointing into ~/.agents-status-light and is left untouched).

Run with --dry-run to preview what would be deleted.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

OPENCODE_PLUGIN_DEST = pathlib.Path.home() / ".config" / "opencode" / "plugins" / "status-light.js"


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def remove(path: pathlib.Path, dry_run: bool) -> None:
    label = "would remove" if dry_run else "removed"
    if path.is_dir() and not path.is_symlink():
        print(f"{label} dir  {path}")
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        print(f"{label}      {path}")
        if not dry_run:
            if path.is_dir() and path.is_symlink():
                path.unlink()
            else:
                path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be removed")
    args = parser.parse_args()

    root = pathlib.Path(args.project_root).resolve()
    dry_run = args.dry_run

    # --- 1. Deploy the shared-CLI OpenCode plugin --------------------------
    source = root / "hooks" / "opencode_plugin.js"
    if not source.exists():
        warn(f"plugin source not found: {source}")
        return 1
    OPENCODE_PLUGIN_DEST.parent.mkdir(parents=True, exist_ok=True)
    dest = OPENCODE_PLUGIN_DEST
    print(f"{'would copy' if dry_run else 'copying'} plugin -> {dest}")
    if not dry_run:
        shutil.copy2(source, dest)
    print("OpenCode plugin now drives the shared AgentsLight CLI.")

    # --- 2. Remove standalone OpenCode install ---------------------------
    home = pathlib.Path.home()
    opencode_artifacts = [
        home / ".opencode" / "status-light",                      # bin/CLI + sessions
        home / "Applications" / "OpenCode Status Light.app",       # old bundle
        home / ".config" / "opencode" / "skills" / "opencode-status-light",  # skill symlink
        home / "Library" / "Logs" / "OpenCodeStatusLight",        # old logs
    ]
    print("\n-- OpenCode standalone artifacts --")
    for artifact in opencode_artifacts:
        remove(artifact, dry_run)

    # --- 3. Legacy Codex standalone artifacts ---------------------------
    # The live Codex integration (handled by AgentsLight) writes through
    # ~/.codex/hooks.json -> ~/.agents-status-light/hooks/codex_status_hook.py.
    # The old independent ~/.codex/status-light bundle is now redundant.
    codex_artifacts = [
        home / ".codex" / "status-light",
        home / ".agents" / "skills" / "codex-status-light",       # legacy symlink
    ]
    print("-- Legacy Codex standalone artifacts --")
    for artifact in codex_artifacts:
        remove(artifact, dry_run)

    if dry_run:
        print("\nDry run complete. Re-run without --dry-run to apply.")

    # Sanity check: ensure the shared CLI actually exists so the plugin works.
    cli = home / ".agents-status-light" / "bin" / "agents-light"
    if cli.exists():
        print(f"shared CLI present: {cli}")
    else:
        warn("shared CLI not found; run scripts/install.py for Codex first.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())