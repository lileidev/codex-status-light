# AgentsLight

A macOS menu-bar and floating status light for coding-agent task state. One
light serves **Claude Code**, **Codex** (OpenAI), **OpenCode**, and the
**DeepSeek Harness (DSH)** from shared state.

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
Each agent's installer adds its own integration (a command hook for Claude/Codex,
an OpenCode plugin, or a DSH session-log watcher), so one app serves every agent.

Only **interactive** sessions are shown: the Claude and Codex hooks skip any
invocation whose parent process is not `claude`/`codex`, so sessions driven
programmatically by other tools (e.g. Obsidian Copilot) and CI never appear.
OpenCode uses a real process PID (auto-pruned on exit), and DSH sessions use an
activity window (see below), so neither needs a parent-process filter.

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

### DeepSeek Harness (DSH)

DSH has no command hook like Codex/Claude, so instead this project watches DSH's
durable session logs directly. DSH persists every session as a zstd-compressed
JSONL file (`$DSH_HOME/sessions/<workspace>/<session>/session.jsonl.zstd`);
`hooks/dsh_status_light.py` tails those logs and writes one status file per DSH
session into the shared state directory, so each DSH session shows up as its own
row (tagged with DeepSeek's blue whale icon, rendered from
`Sources/AgentsLight/Resources/dsh-whale.png`). No modification of DSH itself is
required.

Agent rows with an official brand mark display their real logo instead of an SF
Symbol: Claude rows use the Claude app icon (`Sources/AgentsLight/Resources/claude-logo.png`),
Codex rows use the OpenAI blossom (`Sources/AgentsLight/Resources/openai-logo.png`),
and DSH rows use DeepSeek's whale (`Sources/AgentsLight/Resources/dsh-whale.png`).

```sh
python3 scripts/install_dsh.py           # install + LaunchAgent watcher
python3 scripts/install_dsh.py --no-launch   # install only; run the watcher by hand
```

The login LaunchAgent keeps the watcher running; to run it manually:

```sh
~/.agents-status-light/hooks/dsh_status_light.py
```

State mapping: a DSH `user/message`, `turn/start`, `step/start`, or `tool/call`
turns the light blue, blinking while the model is streaming output (a chunk
landed within a recent window, so sustained generation keeps blinking even when
the tail of the log is momentarily a tool event); a **deep dive** (`reasoning-chunks`
with no visible output after it) also keeps it blue/streaming and labels the turn
"DeepSeek is reasoning…(沉思中)" instead of looking idle; an unanswered
`ask_user_question`/`request_user_input` tool call or a pending sandbox approval
(`approval/asked` with no matching `approval/decided`) turns it yellow; a
`turn/end` reported with an error/blocked reason turns it red; a `turn/end` with
a completed reason, or a turn that goes quiet, turns it green.

Only **recently active** DSH sessions are shown: a DSH session whose log has not
been written for `DSH_ACTIVE_WINDOW_SECONDS` seconds (default 180, i.e. 3
minutes) has its status row removed, so old DSH sessions are pruned automatically
and don't clutter the light — only the sessions you're actually working in stay
visible.

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