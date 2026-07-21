# Codex Status Light

A macOS menu-bar and floating traffic light for Codex task state.

| Light | Meaning |
|---|---|
| Red | A tool failed or the task was marked blocked |
| Yellow | Codex needs input or approval |
| Green | The turn completed |
| Blinking blue | Codex is running normally |

## Build and run

```sh
swift build -c release
swift run CodexStatusLight
```

To package a launchable menu-bar app under `~/Applications`:

```sh
scripts/package_app.sh
open "$HOME/Applications/Codex Status Light.app"
```

Open the menu-bar item and choose **Show floating light** to keep the panel above
other windows.

## Install Codex integration

```sh
python3 scripts/install.py
```

The installer copies the CLI and hooks under `~/.codex/status-light`, merges
`~/.codex/hooks.json` after making a timestamped backup when necessary, and
links the personal skill into `~/.agents/skills/codex-status-light`. It also
creates `~/Library/LaunchAgents/com.local.codex-status-light.plist` so the
packaged app opens at login. Pass `--no-launch-agent` to opt out.

After installation, restart Codex and use `/hooks` to review and trust the new
command hooks. Hooks automatically track turn start, permission requests, tool
errors, and turn completion. The skill handles semantic waiting/blocking states.

## Manual CLI

```sh
~/.codex/status-light/bin/codex-status-light waiting \
  --session manual --message "Need architecture approval"
```

Set `CODEX_STATUS_LIGHT_DIR` to use a different state directory for tests.
