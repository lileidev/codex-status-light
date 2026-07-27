# Codex Status Light

A macOS menu-bar and floating traffic light for Codex task state.

| Light | Meaning |
|---|---|
| Red | A tool failed or the task was marked blocked |
| Yellow | Codex needs input or approval |
| Green | The turn completed / idle |
| Solid blue | Codex is running normally |
| Blinking blue | Codex is actively streaming output |

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
other windows after closing it. The floating light opens automatically when the
app launches.

## Install Codex integration

```sh
python3 scripts/install.py
```

The installer copies the CLI and hooks under `~/.codex/status-light`, merges
`~/.codex/hooks.json` after making a timestamped backup when necessary, and
removes any legacy login LaunchAgent. The app is now launched automatically
when Codex starts or processes a command (via the `SessionStart` hook and
subsequent lifecycle hooks).

If you prefer the old login auto-launch behavior, pass `--launch-agent`:

```sh
python3 scripts/install.py --launch-agent
```

After installation, restart Codex and use `/hooks` to review and trust the new
command hooks. Hooks automatically track turn start, permission requests, tool
errors, and turn completion.

## Manual CLI

```sh
# Update status manually
~/.codex/status-light/bin/codex-status-light waiting \
  --session manual --message "Need architecture approval"

# Indicate the assistant is actively streaming output
~/.codex/status-light/bin/codex-status-light running \
  --session manual --message "Streaming..." --streaming

# Clear a session
~/.codex/status-light/bin/codex-status-light --clear --session manual
```

Set `CODEX_STATUS_LIGHT_DIR` to use a different state directory for tests.
