# Why the Codex yellow light lags: Codex 0.166 hook lifecycle

Investigation for issue [#4](https://github.com/lileidev/codex-status-light/issues/4),
"codex 黄灯状态切换延迟太高" ("the Codex yellow light changes state too slowly").

- **Codex version:** `codex-cli 0.166.0` (source tag
  [`rust-v0.166.0`](https://github.com/openai/codex/tree/rust-v0.166.0))
- **Captured on:** 2026-11-04, macOS, `approvals_reviewer = "user"`, VS Code-originated
  Codex session (`originator: codex-tui`, `source: vscode`)
- **Status-light code:** this repository at `6473e96`

## TL;DR

The status light is not the bottleneck. The hook writes a `waiting` state file about
20–30 ms after Codex fires `PermissionRequest`, and the app reacts to the FSEvents
change within about 0.5 s.

The latency comes from Codex's hook contract:

1. `PermissionRequest` fires **before** the approval prompt is shown.
2. Codex exposes **no hook event when the user answers** that prompt (the app-server's
   internal status is covered in Option 3 below).
3. The next hook after an approved tool is `PostToolUse`, which fires only after the
   tool has **finished successfully**; a denied or failed tool does not fire it at all.

So after the user presses "approve", the light remains yellow for the whole runtime of
the approved command (and, if the model keeps calling tools, until the next hook of any
kind). After a denial or tool failure it can stay yellow until the next hooked event —
typically the next `PostToolUse`, `Stop`, or `SessionEnd`.

There is no hook-only fix that observes the native prompt and clears the light at the
instant of approval (a hook could take over the decision with its own approval UI, but
that changes Codex's flow; see below). The practical options are:

1. **Small, safe, partial fix:** add `PreModelRequest` (and `Interrupt`) hooks to clear
   stale yellow as soon as the turn resumes after a denial/failure; long commands still
   stay yellow while they run.
2. **Correct fix (upstream):** request a paired approval-resolved / human-input-resolved
   hook from Codex. This is already tracked upstream in
   [openai/codex#39426](https://github.com/openai/codex/issues/39426) and
   [openai/codex#28833](https://github.com/openai/codex/issues/28833).
3. **Experimental local path:** consume the app-server's
   `thread/status/changed` notifications, which already carry
   `waitingOnApproval` / `waitingOnUserInput`. This is the only local way to see the
   resolution instantly, but it needs a transport/protocol spike and is version-coupled.

The rest of this document is the evidence and the trade-offs.

## What was measured

### Hook → state file → UI

From `~/.codex/status-light/hook.log`:

- `PermissionRequest` → `emit: state=waiting` is typically **20–30 ms**
  (observed range roughly 19–60 ms; the occasional 100–250 ms entry is an outlier,
  not the steady state).
- The shared CLI writes the JSON atomically (`bin/agents-light`,
  `write_state()` uses `tempfile` + `os.replace`).
- `StatusStore` watches the state directory with FSEvents, latency `0.5`, and coalesces
  refreshes (`Sources/AgentsLight/AgentsLight.swift`, `startWatching()`).

None of those add seconds. The gap is between Codex events, not between the hook and the app.

### The 61-second yellow period

A representative trace from `hook.log` (session `01a1a47f-…`, UTC):

```text
2026-11-04T07:03:22.927Z main: event=PermissionRequest
2026-11-04T07:03:22.933Z ensure_app_running: start
2026-11-04T07:03:22.956Z emit: state=waiting session=01a1a47f-…
2026-11-04T07:04:24.225Z main: event=PostToolUse
2026-11-04T07:04:24.254Z emit: state=running session=01a1a47f-…
```

The light was yellow for **61.3 s**. The log cannot show the exact moment the user
answered, but it contains no state change between the request and the tool completion;
the source below explains why Codex cannot report that moment.
Across the captured log there are 153 yellow periods, with a median of 12.7 s in the
most recent session and a maximum of 47 minutes in the older session; some of that is
genuine time-away-from-keyboard, but the mechanism above is why the light cannot clear
on the answer itself.

## How Codex 0.166 runs hooks

Codex executes every command hook as a short-lived child process of the app-server, one
process per event. The hook therefore has no memory between events; this integration
already reads the on-disk state file for its current-state checks (see
`current_state()`/`set_state()` in `hooks/codex_status_hook.py`), because its in-memory
guards do not survive between invocations.

The hook events configurable in 0.166 include:

```text
PreModelRequest
PreToolUse, PermissionRequest, PostToolUse
PreCompact, PostCompact
SessionStart, SessionEnd, UserPromptSubmit
SubagentStart, SubagentStop, Stop, Interrupt
```

`PreModelRequest` and `Interrupt` are observation-only. `PermissionRequest` and
`PreToolUse` can return decisions, but that does not help this integration: the light
needs to observe the user's answer, not make it.

### Approval path (shell command example)

The relevant order in the 0.166 source is:

```text
turn starts
  → PreModelRequest hook
  → model requests a tool
  → PreToolUse hook                    (registry.rs: run_pre_tool_use_hooks, before tool.run)
  → tool.run(...)
      → sandbox/policy needs approval
      → PermissionRequest hook         (hooks/events/permission_request.rs:
                                        "runs in the approval path, before ...
                                        user approval UI is shown")
      → user answers (allow/deny)      ← no hook here
      → command executes
  → PostToolUse hook                   (only if the tool succeeded; after it finished)
  → model continues / turn ends
      → PreModelRequest next, then Stop
```

Key source points:

- `PreToolUse` is invoked in the tool dispatch path, *before* the tool's `run`, so it
  happens before the approval prompt, not after it:
  [`codex-rs/core/src/tools/registry.rs`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/tools/registry.rs#L839)
- `PermissionRequest` is invoked inside the approval path, immediately before the
  reviewer/UI:
  [`codex-rs/core/src/tools/approvals.rs`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/tools/approvals.rs#L529)
  and
  [`codex-rs/hooks/src/events/permission_request.rs`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/hooks/src/events/permission_request.rs#L1-L8)
- `PostToolUse` is only scheduled when the tool result is a success:
  [`codex-rs/core/src/tools/registry.rs`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/tools/registry.rs#L947)
- The app-server has the exact information the light wants — it keeps a
  `ThreadWatchActiveGuard` for pending permission and user-input requests and exposes
  `ThreadActiveFlag::waitingOnApproval` / `waitingOnUserInput`:
  [`codex-rs/app-server/src/thread_status.rs`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server/src/thread_status.rs#L190-L215)
  and
  [`codex-rs/app-server-protocol/src/protocol/v2/thread.rs`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs).

### Why the rollout log does not help

The approval request/decision are transient `EventMsg` values and are explicitly excluded
from the durable rollout:
[`codex-rs/rollout/src/policy.rs`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/rollout/src/policy.rs#L190-L203)
lists `ExecApprovalRequest`, `RequestUserInput`, and `ApplyPatchApprovalRequest` under
"Transient, non-durable events". Tailing the rollout JSONL therefore cannot reveal the
moment the user answers.

## Why the light stays yellow

| Situation | Next hook Codex fires | Result |
|---|---|---|
| User approves a command, command runs for 30 s | `PostToolUse` after the command finishes | Yellow for the whole command, then blue |
| User approves, then model thinks for 20 s before the next tool | `PreModelRequest` (currently unhooked) | Yellow until the next hooked event; often the next `PostToolUse` |
| User denies an approval | none (`PostToolUse` is success-only) | Yellow until the next tool call, `Stop`, or turn end |
| Tool fails/errors | none (`PostToolUse` is success-only) | Yellow can persist until the next event |
| User cancels/aborts the turn | `Interrupt` or `Stop` (currently unhooked/partly hooked) | Yellow until `Interrupt`/`Stop`, or the app's 30-minute stale-row timeout |

The hook's `_awaiting_input` guard is in-process only and is discarded with the process;
it cannot bridge events. That is not the cause of the delay, but it is why the only
reliable guard is the state file.

## What does **not** fix it

- **`PreToolUse` with `matcher: "*"`** — runs before the approval request, so setting
  "running" there would be overwritten by the subsequent `PermissionRequest`. It cannot
  observe the answer.
- **`PostToolUse` on failure** — Codex does not emit it for failures/denials; only
  successful tool results reach it.
- **Codex `notify`** — supports turn completion, and upstream added
  [`approval-requested`](https://github.com/openai/codex/issues/11808)
  ([#14813](https://github.com/openai/codex/issues/14813)), but neither reports approval
  resolution. It tells you when a prompt appears, which the hook already does.
- **Tailing the rollout / session JSONL** — approval decisions are transient and are not
  persisted (see above).
- **TUI desktop notifications** — `approval-requested` only; no external resolved event.

One theoretical exception: a `PermissionRequest` hook can return `allow`/`deny`, so it
could replace the native prompt with its own approval UI, know the answer, and clear the
light instantly. That takes over Codex's approval flow (and its trust/security model), so
it is a product decision rather than a status-light fix; it is not proposed here.

## Optimization options

### Option 1 — hook-based early clear (small, safe, partial)

Add two events to `hooks/hooks.json` and handle them in
`hooks/codex_status_hook.py`:

- **`PreModelRequest`**: if the session's current state is `waiting`, switch to
  `running` ("Codex is working", streaming). This fires as soon as the turn resumes
  after a denied/failed tool, and after an approved tool finishes, so stale yellow is
  cleared at the first opportunity Codex provides.
- **`Interrupt`**: set `done` ("Codex turn interrupted") unless the state is already
  `error`, so aborts clear yellow instead of leaving it until `Stop`.
- Skip `PreModelRequest` events that carry `agent_id`/`agent_type` (subagents), so a
  subagent's model request cannot clear the parent's waiting row.

Cost/risk:

- One extra hook process per model request (~20–30 ms), plus a new hook-trust prompt in
  Codex after `~/.codex/hooks.json` changes.
- **Does not fix the headline case**: while an approved long-running command is running,
  neither `PreModelRequest` nor any other hook fires. The yellow remains for the command's
  runtime.
- Tests are straightforward: extend `Tests/test_codex_hook.py` for the two new events
  and add the entries to `hooks/hooks.json`.

### Option 2 — paired approval-resolved hook (correct fix, upstream)

Ask Codex to expose a paired event that fires exactly once when a permission or
user-input request is resolved, regardless of allow/deny/cancel. This is the only way a
command hook can clear the light at the instant of approval. The upstream issues already
describe the need and the privacy/shape constraints:

- [openai/codex#39426](https://github.com/openai/codex/issues/39426) — output-only
  human-input lifecycle: pending + resolved for permissions and questions
- [openai/codex#28833](https://github.com/openai/codex/issues/28833) — a post-routing
  signal for passive approval notifications

Recommended: add a short comment/upvote with this integration's concrete use case
(desktop status light, 0.166, VS Code/CLI, `approvals_reviewer = "user"`).

### Option 3 — app-server status observer (experimental, medium/large)

The app-server already tracks the exact state:

- `ThreadStatus` has `activeFlags: ["waitingOnApproval" | "waitingOnUserInput"]`
  (`codex app-server generate-ts` output for 0.166).
- `ThreadStatusChanged` notifications are emitted by
  `ThreadWatchManager::mutate_and_publish()` and sent through
  `OutgoingMessageSender::send_server_notification()`, which broadcasts to all connected
  clients (`connection_ids` empty → `OutgoingEnvelope::Broadcast`).
- The RAII `ThreadWatchActiveGuard` is dropped when the approval/input request is
  resolved, so the status flag disappears at exactly the right moment.

A small observer could connect to the app-server, listen for
`thread/status/changed`, map `threadId` to the status-light session id, and set
`waiting`/`running` directly. That would remove the hook race entirely.

Why it is not the immediate recommendation:

- The app-server control socket/protocol is experimental and versioned; a quick probe
  with `codex app-server proxy` against the running managed daemon did not complete an
  `initialize`. Whether a second client can attach reliably needs a focused spike.
- Upstream [#39426](https://github.com/openai/codex/issues/39426) explicitly notes that
  a separate read-only observer cannot attach to every existing App, IDE, and CLI task.
- It duplicates/replaces the hook's state machine and needs its own lifecycle (reconnect,
  multiple Codex versions, no daemon present, app-server restart).

It is still the most promising local path if the status light is allowed to depend on the
app-server protocol.

### Option 4 — UI-side expiry (not recommended)

Auto-clearing yellow after N seconds, or clearing it on any keyboard/mouse activity,
would hide the symptom but also hide a genuinely pending approval when the user steps
away. It changes the meaning of the light, so it should not be the default.

## Recommendation

1. **Now:** keep the current hooks as the baseline; if a partial improvement is wanted,
   implement Option 1 (small diff + tests). It noticeably shortens the stale-yellow tail
   after denials, failures, and aborts.
2. **Shortly:** ask upstream for Option 2. That is the only way to clear at the instant of
   approval with a supported hook contract.
3. **Only if instant clearing is required locally:** spike Option 3 against the current
   app-server, with a clear fallback to the hooks when the observer cannot connect.

## Update: implemented command-start watcher (2026-11-09)

Instead of waiting for Option 1/2/3, the local hook now bridges the gap itself. On
`PermissionRequest` it records the waiting state's fingerprint and spawns a detached
watcher (`codex_status_hook.py --watch-approval <job>`):

- If the event carries a shell command (`tool_input.command` / `cmd`), the watcher polls
  `ps` for a *new* process whose command line contains a distinctive slice of it. Codex
  runs approved commands as `/bin/zsh -lc <command>`, so this is the instant execution
  starts (0.3 s poll; measured clear 0.2–0.3 s after the command appears).
- Otherwise (`write_stdin`, `apply_patch`, …) it tails the event's `transcript_path` and
  clears when the tool output line appears, which also covers approvals that never spawn
  a fresh process.
- It writes `running` only while the exact waiting state (state + message + `updated_at`)
  is still on disk, so a newer approval, a denial followed by another event, `Stop`, or
  `SessionEnd` always wins. It exits when the Codex process disappears or after 15
  minutes, and job files are pruned.

Tests: 12 new cases in `Tests/test_codex_hook.py` (needle extraction, `ps` matching,
transcript parsing, spawn, clear/stop paths); the hook-spawn and transcript paths were
also run end-to-end with a synthetic `PermissionRequest` against an isolated state dir.

Limits: this is a local heuristic, not a supported hook contract. It needs `ps` (already
used by `_parent_process_name` / `ensure_app_running`), and it cannot clear at the instant
of a *denial* — there is still no event until the next Codex action, so Option 1 remains a
useful complement. The app-server `thread/status/changed` route (Option 3) is still the
only way to observe approvals and denials exactly.

## Reproduction

The trace can be reproduced from the hook log:

```sh
grep -n 'event=PermissionRequest\|emit: state=waiting\|event=PostToolUse' \
  ~/.codex/status-light/hook.log | tail -30
```

The source references above are pinned to `rust-v0.166.0`. The local hook log is not
committed (it contains session ids and timings only).

## Appendix: local environment

```text
codex-cli 0.166.0
approvals_reviewer = "user"
session source = vscode / originator = codex-tui
status-light hooks installed at ~/.agents-status-light/hooks/codex_status_hook.py
hooks.json events: SessionStart, UserPromptSubmit, PermissionRequest,
                  PreToolUse (request_user_input only), PostToolUse, Stop, SessionEnd
```
