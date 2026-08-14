# DSH HOST-side Cordis plugin — concrete authoring guide

Notation: `<pkg>` =
`/Users/larry/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/<pkg>` (read-only reference).
All identifiers below are quoted verbatim from those sources.

---

## 1. Registering an out-of-tree HOST plugin into a profile

A built profile is a Cordis loader tree. The root config is an empty list; the
tree is produced by **applying patch layers** in order with the include plugin's
`applyEntryPatches(data, patches)`. Each layer is a list of `PatchOptions` /
`EntryOptions` rows.

`cordis-plugin-loader/lib/types/config/entry.d.ts` — the serialized entry row:
```ts
interface EntryOptions {
  id: string;               // stable id inside the entry tree
  name: string;             // module specifier imported — THE npm package name
  config?: any;             // config passed to the plugin
  group?: boolean|null;
  disabled?: boolean|null;
  inject?: Inject|null;     // required services / intercept config
}
```
`cordis-plugin-include/lib/types/index.d.ts` — patch row (what goes in a
`cordis.patch.yml`):
```ts
export interface PatchOptions {
  id?: string; insert?: EntryOptions[]; name?: string; config?: any;
  group?: boolean|null; disabled?: boolean|null; inject?: any;
}
```
The shipped base bundle `dsh-base/cordis.patch.yml` is the canonical example —
a single `- insert:` list of rows:
```yaml
- insert:
    - id: session
      name: '@deepseek-ai/dsh-session'
    - id: user-questions
      name: '@deepseek-ai/dsh-user-questions'
    - id: approval
      name: '@deepseek-ai/dsh-user-approval'
      config: { policy: ask }
    - id: subprocess
      name: '@deepseek-ai/dsh-subprocess-local'
    - id: agent
      name: '@deepseek-ai/dsh-agent'
    - id: agent-loop
      name: '@deepseek-ai/dsh-agent-loop'
      config: { agents: [] }
    - id: goal
      name: '@deepseek-ai/dsh-goal'
    - id: command-goal
      name: '@deepseek-ai/dsh-command-goal'
    ...
```

**To add your own host plugin to a profile** (e.g. `web`):
- Install the package into the profile: `dsh plugin --profile web add <your-pkg>`.
  The `dsh` launcher (`dsh/lib/plugin-*.js`, `runPlugin`) forwards this to
  **pnpm in the profile directory** (`$DSH_HOME/profiles/web`), then reconciles
  the profile manifest `package.json` `dsh.profile.bundles` from the installed
  state. pnpm writes into `$DSH_HOME/profiles/web/node_modules`.
- Make the package **resolvable as a bundle dependency** — it must be a
  declared dependency of the profile. (`reconcilePlugins` adds packages whose
  manifest declares `dsh.bundle` to the `bundles` list; a plain dependency that
  isn't a bundle stays a plain dependency and just needs its entry enabled.)
- **Enable its entry** by adding a row to the profile's user patch layer
  `$DSH_HOME/profiles/web/cordis.patch.yml`:
  ```yaml
  - insert:
      - id: my-status-light
        name: 'dsh-status-light'      # = your package, resolved from profile/node_modules
        config: { statusFile: '~/.dsh-status.json' }
  ```
  Or if your package is itself a *bundle* (declares `dsh.bundle.patch`), its
  own `cordis.patch.yml` ships the same `insert` and you add the bundle name to
  `dsh.profile.bundles`.

Profile/home layout (`dsh-home-paths`): home = `$DSH_HOME` else `~/.dsh`;
`$DSH_HOME/profiles/<name>/{package.json, cordis.yml, cordis.patch.yml}`;
`$DSH_HOME/profiles/<name>/node_modules` (pnpm); the flat fallback
`$DSH_HOME/profiles/node_modules` holds symlinks to every package the
installation depends on so bare specifiers resolve.

## 2. The exact Cordis HOST plugin signature

A host plugin's `name:`-pointed package must default-`export` (or export) a
Cordis `Plugin` (`cordis/lib/types/registry.d.ts`):
```ts
export type Plugin<T> = Plugin.Function<T> | Plugin.Constructor<T> | Plugin.Object<T>;
interface Plugin.Function<T> { (ctx: Context, config: T): any }
interface Plugin.Object<T>   { apply(ctx: Context, config: T): any }
interface Plugin.Base<T> {
  name?: string; Config?: schema; inject?: Inject; provide?: string | string[];
}
```
The **dominant shipped host-plugin form is the object plugin** — a module that
declares `name`, `inject`, and `apply(ctx)`. Verified, real examples:

`dsh-command-goal/lib/types/index.d.ts`:
```ts
export declare const name = "command-goal";
export declare const inject: string[];
export declare function apply(ctx: Context): void;
```
its `lib/index.js` body:
```js
const inject = ["commands", "goals"];
function apply(ctx) {
  ctx.commands.register({ name: "goal", description: "...",
    input: { hint: "..." }, handler: (invocation) => executeGoalCommand(ctx, invocation) });
}
export { apply, inject, name };
```

`dsh-tool-ask-user` — same trinity (`name="tool-ask-user"`, `inject`, `apply`).
`dsh-session` differs: it is a **Service provider** — `export default SessionStore`
where `class SessionStore extends Service`; the object/class forms are the same
union. So: **your host plugin is a module that exports `{ name, inject, apply(ctx) }`
(or a plain `(ctx) => ...` function).**

Cleanup: no `dispose` return. Register inside `ctx.effect(() => disposer,label)`;
`ctx.on(...)`, service-`register` calls return disposers; all are run on fiber
unload (`cordis/lib/types/fiber.d.ts`).

## 3. Host services to observe session lifecycle (session id + cwd)

> ⚠️ `ctx.sessions` is **different** on the host and the browser. On the
> **host** (what this plugin runs on) `ctx.sessions` is the
> `SessionStore` from `@deepseek-ai/dsh-session`. The browser's
> `ctx.sessions` (an `ISessions`/`SessionRuntime`) is a different, client-side
> type in `dsh-client-runtime` — don't mix them up.

### `ctx.sessions` — `SessionStore` (`@deepseek-ai/dsh-session`)
Services on `ctx`: `sessions: SessionStore`. Cordis events on `ctx`:
```
'session/created'(this: Scoped<Session>, session: Session): void
'session/disposed'(this: Scoped<Session>, session: Session): void
'session/event'(this: Scoped<Session>, session: Session, event: SessionEvent): void
'session/flush'(this: Scoped<Session>, session: Session): Promise<void> | void
```
The `Session` class (not a service) exposes:
```
readonly header: SessionHeader;         // has .id, .cwd, .createdAt, parentSession?, origin?
get id(): SessionId
get seq(): number
get events(): readonly SessionEvent[]   // immutable snapshot of the log
requestHeader(): EpochHeader | undefined
deriveMessages(): Message[]
```
`SessionHeader` (`dsh-session/lib/types/types.ts`) is where **cwd lives**:
```ts
interface SessionHeader { version; id: SessionId; createdAt; cwd?: string; parentSession?; ... }
```
`SessionStore` methods: `create(id?, opts?): Session`,
`prepare/enter/announce`, `get(id): Session|undefined`, `list(): Session[]`,
`fork(source, boundary?, childId?): Session`, `flush(session): Promise<boolean>`.

**The session's event vocabulary is the raw log** (`SessionEvent`, from
`dsh-session/types.ts`): `turn/start|end`, `step/start|end`, `user/message`,
`assistant/chunk`, `assistant/message`, `tool/call`, `tool/result`,
`todo/write`, `request/header`, `request/context`, `session/end-seed`. NosAnd
the dsh-user-approval extends the log with `approval/asked`, `approval/decided`,
`approval/policy`.

### `ctx.agent` lifecycle low-light (from `@deepseek-ai/dsh-agent` runtime-types)
Cordis `Events`, agent-scoped:
```
'agent/created', 'agent/disposed'
'agent/status'(payload { agent, status:'idle'|'running' })      // === 'busy' everywhere
'agent/error'(payload { agent, turn, step, error }): void
'agent/session-start'(payload { agent, source }): void
'agent/pre-step', 'agent/request', 'agent/request-error', 'agent/turn-stopping'
'agent/inbox/{inserted,claimed,discarded}'
```
`Agent` exposes `id` (=== session id; they share one wire id), `.status`
(`'idle'|'running'`), `.session` (Session), `.ctx` (scoped), `whenIdle()`.

### Tool execution
- **Before/after per tool**: watch `session/event` for `event.type === 'tool/call'`
  and `'tool/result'`, or the equivalent mux frames in the browser. On the host
  `session/event` gives you the authoritative cached log (`event.data.callId`,
  `name`, `arguments`, and for result `event.data.message` + `error`).
- **Tool failure**: `tool/result` events carry `event.data.error` (`{name,code}`)
  and `message.content[0].isError === true`.

### Permission / approval "waiting"
`ctx.approval: ApprovalService` (`@deepseek-ai/dsh-user-approval`) + a
`approval/request` waterfall event `(Scoped<ApprovalService>, req, next)`.
The **authoritative, replayable** view is two session-log events that
`dsh-user-approval` injects into `dsh-session/types.SessionEventMap`:
```
'approval/asked':  { id: ApprovalRequestId; toolName: string; callId?: CallId; reason?: string }
'approval/decided':{ id: ApprovalRequestId; outcome: ApprovalOutcome }
```
→ a pending approval shows up as `approval/asked` with no matching
`approval/decided`, `session.event` typed. Listen on `session/event`.

### User question waiting (`ask_user_question`)
`ctx.userQuestions: UserQuestionService` (`@deepseek-ai/dsh-user-questions`).
It exposes `registerProvider(provider): () => void` and `ask(request)`. **There
is NO dedicated host cordova event / session-log entry** for a user question
(pending). The only signal that a question is being asked is that the tool is
executing (the agent is mid-`tool/call` for `ask_user_question`) and is waiting.
So the closest host signal for the user-question "waiting" state is the agent's
`agent/status === 'running'` plus a pending `tool/call` with
`name === 'ask_user_question'` and no `tool/result` yet. (The **browser** gets a
dedicated `question/requested` mux frame, but the host plugin lives on the
host, not the browser.)

### Session done / idle / end
- Idle ⇄ running: `agent/status { 'idle' | 'running' }`.
- A turn finished: session event `turn/end` with
  `event.data.reason: 'completed'|'aborted'|'blocked'|'error'|'max-tokens'`.
  (Reason carries a structured `TurnEndReason`.)
- Session ended wholly: `session/disposed` fired (the store's detach/announce
  teardown) — the durable "done" signal when the async is gone.
- A session error with no turn position: `agent/error`.

### Session-telemetry seam (what you asked — exists but secondary)
`@deepseek-ai/dsh-session-telemetry` (`ctx.sessionTelemetry:
SessionTelemetryBackend`) and a `session-telemetry/record` **waterfall** that
redacts an outbound `SessionTelemetryRecord` = `{ channel:'ledger'|'ops',
time, severity:'info'|'warn'|'error', attributes, body }`.
`severity==='error'` is pre-mapped for tool-result `isError`, `turn/end` error
reasons, and `agent-error`. This is the **capture/export** seam (mounted but
**disabled by default** in `dsh-base`), not the primary subscription surface.
The authoritative firehose is `session/event` on `ctx.sessions` — the telemetry
coordinator is itself a consumer of it. So prefer `session/event` for your
status logic.

## 4. Library code to shell out — confirmed, plugins are NOT sandboxed

The host is Node. **Plugin code runs unsandboxed in the host process.** The DSH
sandbox (`dsh-bash-sandbox`, `dsh-sandbox-local`, `dsh-sandbox-policy`) gates
**tools** (`dsh-tool-bash`, `dsh-tool-pwsh`), not plugin `apply(ctx)` code.
Evidence: `dsh-subprocess-local/lib/index.js` line 6 itself does
`import { execFileSync, spawn, spawnSync } from "node:child_process";` and
consumes it freely.

The **sanctioned** spawn API is the subprocess seam —
`ctx.subprocess: SubprocessRuntime` (`@deepseek-ai/dsh-subprocess` +
`@deepseek-ai/dsh-subprocess-local`). The dsh-native-command
(`@deepseek-ai/dsh-native-command`) is a lighter no-shell `execFile` wrapper:
```ts
export type NativeCommandRunner =
  (command: string, args: readonly string[], signal: AbortSignal) => Promise<{stdout: string; stderr: string}>;
export const runNativeCommand: NativeCommandRunner;
```
For shelling out, the recommended primitive is either `runNativeCommand` or
`ctx.subprocess.spawn(spec)` where
```ts
interface SubprocessSpawnSpec {
  argv: readonly string[];        // argv[0] = program, never shell-interpreted
  cwd: string;
  stdio: { stdin: SubprocessStdinMode; stdout: SubprocessOutputMode; stderr: SubprocessOutputMode };
  graceMs: number;                // SIGTERM→grace→SIGKILL escalation
  signal?: AbortSignal;           // trigger terminate escalation
}
```
`ctx.subprocess.spawn(spec).outcome` resolves when the process closes
(`SubprocessHandle`: `stdout/.done/.outcome`; there is also `resolveExecutable`
and `spawnTerminal`). Either works inside a host plugin. Writing the status file
is plain `import { writeFile, mkdtemp } from 'node:fs/promises'` — the host has
full filesystem access; it is the *browser* that can't.

If you want to avoid the managed subprocess handle (or just want a one-shot
exec) both `runNativeCommand` and a direct `node:child_process` import are
accepted inside a host plugin — importing `node:child_process` inside shipped
host packages is normal and unsandboxed.

---

## 5. Minimal HOST status-light plugin skeleton

Below is a complete, runnable host plugin package. It (a) subscribes to host
session/agent lifecycle, (b) writes a JSON status file and spawns a helper CLI,
for the **current** session id + the session's `cwd`.

`package.json`:
```jsonc
{
  "name": "dsh-status-light",
  "version": "0.1.0",
  "type": "module",
  "main": "index.js",
  "exports": { ".": "./index.js" },
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } },
  "peerDependencies": {
    "@deepseek-ai/cordis": "^4.0.1",
    "@deepseek-ai/dsh-agent": "^0.1.0-rc.6",
    "@deepseek-ai/dsh-session": "^0.1.0-rc.6"
  }
}
```
`cordis.patch.yml` (only if you package as a bundle; a profile-patch `insert` is
equally fine — see §1):
```yaml
- insert:
    - id: status-light
      name: 'dsh-status-light'
      config:
        statusFile: '~/.dsh-status.json'
        helper: '/usr/local/bin/agents-light'
```

`index.js` (host plugin — object form `{ name, inject, apply }`):
```js
import { existsSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { runNativeCommand } from '@deepseek-ai/dsh-native-command';

export const name = 'status-light';
export const inject = ['sessions', 'agent'];   // wait for SessionStore + agent loop

// -- state bus -----------------------------------------------------------
function set(state, config) {
  const file = process.env.DSH_STATUS_FILE
    ?? (config?.statusFile ?? join(homedir(), '.dsh-status.json'));
  writeFileSync(file, JSON.stringify({ at: Date.now(), ...state }));
  // Fork the macOS CLI that updates the desktop light (the sanctioned spawn):
  runNativeCommand('/usr/local/bin/agents-light', [state.state, state.session ?? ''], new AbortController().signal)
    .catch((e) => console.error('status-light spawn failed', e));
}

// -- map DSH lifecycle onto the 4 states -------------------------------
export function apply(ctx, config) {
  // current session id + cwd come from the store / the agent
  const sessOf = (id) => ctx.sessions?.get(id);
  const cwdOf = (sess) => sess?.header?.cwd ?? process.cwd();

  // idle ⇄ running
  ctx.on('agent/status', ({ agent }) => {
    const sess = agent?.session;
    set({
      state: agent.status === 'running' ? 'busy' : 'done',
      session: sess?.id,
      cwd: cwdOf(sess),
    }, config);
  });

  // a turn finished → 'done' (or 'error' on failure)
  ctx.on('session/event', (session, event) => {
    if (event.type !== 'turn/end') return;
    const failed = event.data.reason?.kind === 'error';
    set({
      state: failed ? 'error' : 'done',
      session: session.id,
      cwd: cwdOf(session),
    }, config);
  });

  // a tool began/ended → 'busy' / tool failure → 'error'
  ctx.on('session/event', (session, event) => {
    if (event.type === 'tool/call') {
      set({ state: 'busy', tool: event.data.name,
            session: session.id, cwd: cwdOf(session) }, config);
    } else if (event.type === 'tool/result') {
      const err = event.data.error || event.data.message?.content?.[0]?.isError;
      set({ state: err ? 'error' : 'busy', tool: event.data.name,
            session: session.id, cwd: cwdOf(session) }, config);
    }
  });

  // a permission/approval is waiting (log-only approval/asked event)
  ctx.on('session/event', (session, event) => {
    if (event.type === 'approval/asked') {
      set({ state: 'waiting', reason: event.data.toolName,
            session: session.id, cwd: cwdOf(session) }, config);
    } else if (event.type === 'approval/decided') {
      set({ state: 'busy', session: session.id,
            cwd: cwdOf(session) }, config);
    }
  });

  // initial idle snapshot
  set({ state: 'idle' }, config);

  // run everything inside effects so teardown disposes listeners
  ctx.effect(() => () => set({ state: 'idle' }, config), 'status-light: final idle');
}
export default { name, inject, apply };
```
Notes:
- Installed as a plugin and enabled by adding `dsh-status-light` to the
  profile `cordis.patch.yml` (or, if you prefer, the bundle's own patch). After
  `dsh plugin --profile web add dsh-status-light`, the `id: status-light` row lets
  the Loader import it from `profiles/web/node_modules`.
- `runNativeCommand` and `node:child_process` are both legal in a host plugin —
  the sandbox gates only the model's `bash_tool`/`subprocess_tool`.
- Since a running dsh drives the status host process, the status plugin best
  runs on the **host** (webapp or the CLI), not the browser — this is exactly the
  host capability it doesn't have.

**Key file paths read (host side, read-only reference):**
- `dsh/README.md`, `dsh/lib/plugin-*.js` (`runPlugin`), `dsh/lib/profile-boot-*.js`
- `dsh-app-boot/README.md`, `dsh-app-boot/lib/types/profile.ts`
- `dsh-base/cordis.patch.yml`, `dsh-web-app/cordis.patch.yml`
- `cordis-plugin-include/lib/types/index.ts` (PatchOptions)
- `cordis-plugin-loader/lib/types/config/entry.ts` (EntryOptions)
- `cordis/lib/types/{index,registry,fiber}.ts`
- `dsh-session/lib/types/index.ts`, `.../types/types.ts`
- `dsh-agent/lib/types/runtime-types.ts`
- `dsh-user-approval/lib/types/index.ts`, `dsh-user-questions/lib/types/index.ts`
- `dsh-session-telemetry/lib/types/index.ts`, `dsh-session-stats/lib/types/index.ts`
- `dsh-subprocess/lib/types/{index.ts,types.ts}`, `dsh-subprocess-local/lib/index.ts`
- `dsh-native-command/lib/types/index.ts`