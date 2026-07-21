---
name: codex-status-light
description: Keep the global Codex macOS status light synchronized with task progress. Use whenever Codex performs multi-step work, waits for user input or approval, completes a turn, or encounters a blocking error, especially when terminal activity may be missed.
---

# Codex Status Light

Use the installed CLI at `~/.codex/status-light/bin/codex-status-light`.
Lifecycle hooks already handle normal turn start, permission prompts, tool failures,
and turn completion. Add explicit updates only for semantic states hooks cannot infer.

## Set state

Derive the session identifier from `CODEX_SESSION_ID` when present. Otherwise use a
stable short identifier containing the repository name and current task.

```sh
~/.codex/status-light/bin/codex-status-light STATE \
  --session "$SESSION" --message "SHORT REASON"
```

Use these states:

- `running`: resume after the user answers, or start a new substantial phase.
- `waiting`: immediately before asking a blocking question, requesting human review,
  or pausing for an external action.
- `done`: only when the requested outcome is complete and verified as appropriate.
- `error`: when work is blocked by a failure that cannot be resolved in the current
  turn. Do not use it for a transient failed command while useful recovery continues.

Keep messages short and free of secrets. Status updates are best-effort: if the CLI is
missing or fails, continue the user's task and mention the integration problem only
when it affects the requested outcome.

## Required sequence

1. Let hooks set `running` on user prompt submission.
2. Set `waiting` before a blocking user question not represented by a permission tool.
3. Set `running` after receiving the answer.
4. Set `error` before reporting a genuine blocker.
5. Let the `Stop` hook set `done` for ordinary completed turns. Explicitly set `done`
   only when a workflow ends outside the normal Codex turn lifecycle.
