# AgentsLight

A macOS menu-bar and floating status light for coding-agent task state. One
light serves **Claude Code** and **Codex** (OpenAI) from shared state.

| State | Color | Meaning |
|---|---|---|
| `running` | Blue (solid) | An agent is running normally |
| `running` | Blue (blinking) | An agent is actively streaming output |
| `waiting` | Yellow | An agent needs input or approval |
| `done` | Green | The turn completed / idle |
| `error` | Red | A tool failed or the task was marked blocked |

## Build and run

```sh
swift build -c release
swift run AgentsLight
```

To package a launchable menu-bar app under `~/Applications`:

```sh
scripts/package_app.sh
open "$HOME/Applications/AgentsLight.app"
```

Open the menu-bar item and choose **Show floating light** to keep the panel above
other windows after closing it. The floating light opens automatically when the
app launches.

## Install agent integrations

The shared CLI, state directory, and hooks live under `~/.agents-status-light/`.
Each agent's installer adds its own hook and registers it, so one app serves
every agent.

### Claude Code

```sh
python3 scripts/install_claude.py
```

Merges a `hooks` block into `~/.claude/settings.json` (after a timestamped
backup), registering `SessionStart`, `UserPromptSubmit`, `PermissionRequest`,
`Notification`, `PostToolUse`, and `Stop` to drive the light.

### Codex

```sh
python3 scripts/install.py
```

Copies the shared CLI and the Codex hook under `~/.agents-status-light`, merges
`~/.codex/hooks.json`, and removes any legacy login LaunchAgent. The app is now
launched automatically when Codex starts or processes a command.

If you prefer the old login auto-launch behavior, pass `--launch-agent`:

```sh
python3 scripts/install.py --launch-agent
```

After installation, restart the agent and use `/hooks` (Claude) or `/hooks`
(Codex) to review and trust the new command hooks. Hooks automatically track
turn start, permission requests, tool errors, and turn completion.

## Manual CLI

```sh
# Update status manually
~/.agents-status-light/bin/agents-light waiting \
  --session manual --message "Need architecture approval"

# Indicate the assistant is actively streaming output
~/.agents-status-light/bin/agents-light running \
  --session manual --message "Streaming..." --streaming

# Clear a session
~/.agents-status-light/bin/agents-light --clear --session manual
```

Set `AGENTS_STATUS_LIGHT_DIR` to use a different state directory for tests.