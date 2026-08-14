# DSH client-plugin authoring — concrete findings

Notation: `<pkg>` below means
`/Users/larry/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/<pkg>` (read-only reference install).
All quotes are verbatim identifiers from those sources.

---

## 1. Minimal exported entry for a client plugin (browser half)

A client plugin is a **Cordis object plugin**: a package bundle whose client entry
exports a named `apply(ctx)` function and a named `inject` array.

Real, smallest shipped example — `dsh-client-ui-trajectory`:

`dsh-client-ui-trajectory/lib/types/client/index.d.ts`:
```ts
import type { Context } from '@deepseek-ai/cordis';
export declare const inject: string[];               // required services
export declare function apply(ctx: Context): void;  // plugin body
```
with `lib/types/index.d.ts` (the *host* face of the same package):
```ts
export declare function apply(): void;   // host loader entry — no host behavior
```
The compiled browser bundle registers through the web module system
(`window.__ModuleLoader__.load({ id, factory })`); at the source level the
two named exports `{ apply, inject }` are what the Loader/graphing builds into
a fiber. The `apply` body from `lib/client.js` (verbatim shape):
```js
const inject = ["slots", "conversationEvents", "conversationViews", "sessions", "locale"];
function apply(ctx) {
  ctx.effect(() => ctx.locale.register(NS, { zh, en }), "ui-trajectory: dictionaries");
  registerTrajectoryMessageDefinitions(ctx);  // each calls ctx.conversationEvents.register(def)
  registerTrajectoryDefinition(ctx);
  ctx.slots.inject("conversation.view", () => ctx.slots.register({ ... }));
}
exports.apply = apply; exports.inject = inject;
```

**Exact entrypoint signature** (Cordis, `cordis/lib/types/registry.d.ts`):
```ts
interface Plugin.Function<T>  { (ctx: Context, config: T): any }
interface Plugin.Object<T>    { apply(ctx: Context, config: T): any }
interface Plugin.Base<T> {
  name?: string; Config?: StandardSchemaV1; inject?: Inject; provide?: string|string[];
}
export type Plugin<T> = Plugin.Function<T> | Plugin.Constructor<T> | Plugin.Object<T>;
```
So the minimal entry is a file that `export const inject = [ ...service names ...]` and `export function apply(ctx) { ... }`; the loader treats it as the object form `{ inject, apply }`.

**Dispose/cleanup contract** (`cordis/lib/types/fiber.d.ts`): there is **no** `dispose` return.
Cleanup is declarative:
- `ctx.effect(() => disposer, label)` — the effect body returns a disposer (or iterable of disposers) that run, in reverse registration order, when the fiber unloads.
- Every registry/hook call already returns an idempotent disposer; e.g. `ctx.on('event', ...)`, `ctx.conversationEvents.register(def)` → disposer, `ctx.slots.register(...)`. Registering inside an effect ties teardown to fiber unload.
- Fiber states: `PENDING LOADING ACTIVE FAILED DISPOSED UNLOADING`; plugin unload runs disposers; workflow unload auto-unsubscribes anything registered via `ctx.on`/`ctx.effect`.

---

## 2. How a client plugin gets loaded into a profile

Two cooperating mechanisms:

**(a) Profile bundle (Node/host + web entry).** Packages are declared via their `package.json` `dsh` section.
`dsh-app-boot/lib/types/profile.d.ts`:
```ts
interface DshBundleManifest { patch: string }
interface DshProfileManifest { bundles?: string[] }
interface DshManifestSection { bundle?: DshBundleManifest; profile?: DshProfileManifest }
```
A bundle declares `"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }` (e.g. `@deepseek-ai/dsh-web-app`). The patch file holds Loader `PatchOptions` (entries: `id`-targeted config overrides, `insert` lists, `!!js`), applied in `dsh.profile.bundles` order over an empty entry list (`cordis.yml` root is `[]`).

**(b) Client scan → `__DSH_BOOT__`.** The host Node half `dsh-client-modules/lib/index.js` declares:
> "scans the host Loader's entries for packages declaring `dsh.client`, composes the `window.__DSH_BOOT__` entry graph … serves `/plugins/<id>/client.js`"

Predicate (`WebEntryTable.scan`, same file): a package is a client plugin when its manifest has `dsh.client` **and** `dsh.client.platform === "web"`, and the package's exports exposes `exports["./client"]` → a bundle that the loader composes. The trajectory package is the reference:
```json
"exports": { ".": {...}, "./client": { "types": ".../types/client/index.d.ts", "default": "./lib/client.js" } },
"dsh": { "client": { "inject": ["@deepseek-ai/dsh-client-locale", "@deepseek-ai/dsh-client-runtime", "@deepseek-ai/dsh-client-ui-conversation"], "platform": "web" } }
```
So a package can be **both** a host bundle (`dsh.bundle.patch`) **and** a client plugin (`dsh.client.platform: "web"` + `exports["./client"]`).

**There is no `~/.config/dsh/plugins` drop-in.** Home is `~/.dsh` (`$DSH_HOME`), layout:
- `$DSH_HOME/profiles/<name>/{package.json, cordis.yml, cordis.patch.yml, pnpm-workspace.yaml}`
- `$DSH_HOME/profiles/<name>/node_modules` (pnpm-managed out-of-tree plugins)
- `$DSH_HOME/profiles/node_modules` — auto-maintained flat symlink fallback (bare package resolution)
- `$DSH_HOME/cordis.patch.yml` — **home-level user patch** (apply over every profile; this is your nearest "drop-in" analogy: put your plugin entry here, shared across profiles)

**How to install a third-party bundle/plugin into a profile** — `dsh plugin`:
`dsh plugin --profile <name> <pnpm args>` forwards to pnpm in the profile dir, then reconciles `dsh.profile.bundles` from installed state (a dependency whose package declares `dsh.bundle` joins the layer stack).
- `dsh plugin --profile web add <your-plugin-package>` (registry, git, path, or tarball; relative `file:`/`link:` specs are re-anchored to your invoking dir).
- Then the plugin's entries must be enabled: add rows in `$DSH_HOME/profiles/web/cordis.patch.yml` (`insert`) or have the bundle ship its own `cordis.patch.yml`).
- `$DSH_HOME` override: `$DSH_HOME` env, else `~/.dsh`.

If the package declares `dsh.client` + `platform:"web"` and is **an enabled Loader entry** in the composed tree, `ds-client-modules` discovers it and injects it into `window.__DSH_BOOT__` automatically — no extra registration.

---

## 3. Subscribing to session lifecycle events in the browser

Three sanctioned observation surfaces:

**(a) `ctx.conversationEvents` (ConversationEventRegistry)** — the higher-level registry the trajectory plugin uses.
`dsh-client-runtime/lib/types/client/conversation/event-registry.ts`:
```ts
class ConversationEventRegistry extends ConversationDefinitionRegistry<ConversationNodeDefinition> {
  register(def): () => void;              // returns idempotent disposer
  registerFallback(def): () => void;
}
```
It expands events into business "definitions". The session vocabulary is the **raw Session event log** union (`dsh-session/lib/types/types.d.ts`):
```ts
interface SessionEventMap {
  'turn/start': { turn: number };
  'turn/end':   { turn: number; reason: TurnEndReason };
  'step/start': { turn: number; step: number };
  'step/end':   { turn: number; step: number };
  'user/message': UserMessage;                       // prompt submitted
  'assistant/chunk': { turn; step; chunk: StreamChunk };
  'assistant/message': { turn; step; message: AssistantMessage; usage? };
  'tool/call': { turn; step; callId: CallId; name: string; arguments: string };
  'tool/result': { turn; step; message: ToolResultMessage; error?; meta? };
  'todo/write': { todos: TodoItem[] };
  'request/header': { header: EpochHeader; reason: RequestHeaderReason };
  'request/context': RequestContext;
  'session/end-seed': Record<string, never>;
}
type SessionEvent = ... { type: K; seq: number; time: number; data: SessionEventMap[K] } ...
```
`conversationEvents.register(def)` where `def: ConversationNodeDefinition` has `match/start/update/publication/buildLocationData/buildViewNode`.

**(b) Direct event streams over the wire client (`ctx.connection.api.events`).**
`@deepseek-ai/dsh-host-apiproxy/api` `EventsApi`:
```ts
mux(request: {since?: Record<SessionId,number>}, signal): AsyncIterable<RpcRequest<MuxFrame>>;
host(request: {}): AsyncIterable<RpcRequest<HostFrame>>;
```
`MuxFrame` = raw `session/event` passthrough, `session/subscribed`, `approval/requested`, `approval/resolved`, `question/requested` (ask_user_question), `question/resolved`, `session/queue`, `session/jobs`, `session/projection`, `host/remote-event`, `stream/error`.
`HostFrame` = `host/session-added` (drives `cwd`, `blank`, `origin`, `agentPreset`), `host/session-removed`, `host/session-status` (running flips), `host/agent-error`, `host/workspace-changed|removed|order-changed`, `host/archived-sessions-changed`.

**(c) cordis events on `ctx`** — `slots/changed`, `connection/reset` (declared client-wide), and allowlisted host events arriving via the `host/remote-event` mux frame dispatched to `ctx.remote.$dispatch(event, args)`; consumers observe with `ctx.remote.$on(...)` (allowlist in `dsh-api-remotes` `API_REMOTE_FORWARDED_EVENTS`).

Which event = which status you care about:
- session created → `host/session-added` (host stream) or `agent/created` (host side)
- prompt submitted → `user/message` (mux) / `agent/inbox/claimed` (host)
- assistant streaming → `assistant/chunk`, settlement `assistant/message`
- tool before/after → `tool/call`, `tool/result`
- permission asked / ask_user_question → `approval/requested` / `question/requested` mux frames
- session idle / done / error → host status: `host/session-status {running:boolean}` (client) or `agent/status {idle|running}` / `agent/error` (`@deepseek-ai/dsh-agent` host events)

---

## 4. Getting session id + cwd, and running a child process from the browser

**Session id + cwd (read from a client plugin, browser side):**
- Session id: `ctx.sessions.list` snapshot → `SessionListState.current`; or on an agent-scoped ctx `ctx.sessions.scopeOf(ctx)`; or `ctx.sessions.binding(id).session.sessionId` (`dsh-client-runtime`, `sessions/service.ts`).
- cwd: `api.host.describe()` returns `{ version, cwd, attachedSessions, canOpenPath }` — the host process working directory (root for session persistence and tool execution). Also `SessionSummary.cwd` (list rows) and `host/session-added` carries `cwd` (the per-session working dir).

**THE CRUX — the browser cannot run a CLI or write an arbitrary file. This is a hard, verified blocker.**

The browser half's HOST-Reaching surface is exactly `ctx.connection = { api: IApiClient, isLoopback, hostDescription, rpc }` plus the generated remotecamel ns (`ctx.remote`, e.g. `remote.commands`). The entire host API method set (`ApiProxy`, `dsh-host-apiproxy/api/index.ts`) is:
```
sessions, subagents, host, workspace, skills, agentPresets, events, goals,
settings, credentials, llm, respond
```
and the `host` domain (`host.ts`) contains **only**:
`describe`, `pickDirectory`, `listDirectory`, `createDirectory`, `openPath`.

There is **no `host.exec` / `host.spawn` / `host.writeFile` / `host.run`-type RPC.** There is no `spawn` in a browser. `settings`/`credentials` persist config/credentials, not arbitrary JSON files; `host` touches the filesystem only for directory listing/picking and OS open-with-app; `createDirectory` creates directories but never writes file content.

Closest working alternatives (pick one):

1. **Host-side Node plugin (recommended).** Put a Cordis plugin on the **host** (Node) side of the same profile. It can `import { runNativeCommand } from '@deepseek-ai/dsh-native-command'` (a **zero-dependency no-shell `execFile` runner**: `runNativeCommand(command, args, signal)` spawns the executable directly, captures stdout/stderr, propagates abort). It can also just use plain `node:child_process` / `node:fs` to write `status.json`. It subscribes to the **host** session lifecycle events from `@deepseek-ai/dsh-agent`:
```
'agent/created', 'agent/disposed',
'agent/status'   (payload: { agent, status: 'idle'|'running' }),
'agent/error',   (payload: { agent, turn, step, error }),
'agent/inbox/inserted', 'agent/inbox/claimed', 'agent/turn-stopping',
'agent/session-start', 'agent/pre-step', 'agent/request', ...
```
These are declared in `dsh-agent/lib/types/runtime-types.d.ts`:
```ts
declare module '@deepseek-ai/cordis' {
  interface Events {
    'agent/status'(this: Scoped<Agent>, payload: { agent: Agent; status: AgentStatus }): void; // AgentStatus='idle'|'running'
    'agent/error'(this: Scoped<Agent>, ...): void;
    ...
  }
}
```
So a host bundle's `redis: (ctx) => ctx.on('agent/status', ...)` can drive the status light with no browser in the loop, and write the JSON+Spawn the CLI directly.

**2. Slash-command/tool bridge.** The browser's `SessionFace.command(line)` (or the generic `remote.commands` in `SessionRemotes`) tells the **host** to execute a slash command against the session's agent. If you register a host-side command plugin (`db:schedule`, `cctl...` — a Corda command handler in the host bundle), the browser plugin on state transition calls your command, and the host plugin does the actual `spawn`/write. This keeps the write on the host (safe) while letting the browser drive the trigger.

**3. Headless wrapper / watch file poll.** If you cannot ship a host plugin, the host of truth can poll a status file that the *agent tools* write (e.g. bash/subprocess tool writing a JSON during a run), or a headless profile wrapper can publish on session/run end — but that's brittle and requires the agent to call a tool.

**Recommendation:** do the write/host work in a **host-side Node plugin** in the profile; keep the browser plugin (if any) as a thin signal relay (it can read session id/cwd via `ctx.sessions` + `api.host.describe` and render a chrome/native). For a desktop status light, a PowerShell on macOS just runs `open -a` or writes a file — do that in the host plugin.

---

## 5. Smallest working plugin (host side is required for the file write)

### Host-side plugin (write the status JSON + spawn CLI)

`package.json` (a dual-face bundle: host plugin AND client entry):
```jsonc
{
  "name": "dsh-status-light",
  "type": "module",
  "exports": {
    ".": { "default": "./host.js" },           // host face
    "./client": { "default": "./browser.js" }  // browser face (bundle)
  },
  "dsh": {
    "bundle": { "patch": "./cordis.patch.yml" },
    "client": { "platform": "web", "inject": ["@deepseek-ai/dsh-client-runtime", "@deepseek-ai/cordis"] }
  },
  "peerDependencies": { "@deepseek-ai/cordis": "^4.0.1", "@deepseek-ai/dsh-agent": "^0.1.0-rc.6", "@deepseek-ai/dsh-native-command": "^0.1.0-rc.6" }
}
```

`lib.js` (host Node side):
```js
import { runNativeCommand } from '@deepseek-ai/dsh-native-command';
import { writeFile } from 'node:fs/promises';

const STATUS = process.env.STATUS_FILE ?? `${process.env.HOME}/.dsh-status.json`;
async function setState(s) {
  await writeFile(STATUS, JSON.stringify({ at: Date.now(), ...s }));
  // Fork the macOS helper that updates the desktop light:
  await runNativeCommand('/usr/local/bin/status-light', [s.state, s.label ?? '']);
}

export const inject = ['sessions'];               // optionally needs session service
export function apply(ctx) {
  // host lifecycle events → never needs the browser, writes directly:
  ctx.on('agent/status', ({ agent, status }) =>
    setState({ session: agent.id, state: status === 'running' ? 'busy' : 'idle', label: 'dsh' }));
  ctx.on('agent/error', ({ agent }) =>
    setState({ session: agent.id, state: 'error', label: 'dsh' }));
}
```
`cordis.patch.yml` (declared as the bundle's patch layer) main line to enable the entry:
```yaml
- id: system-prompt          # (no-op) ...
insert:
  - name: dsh-status-light    # referred by exports["."]
```
(replace with the include entry idiom used elsewhere — see existing bundle patch files.)

### Browser-half observer (optional relay, reads session id/cwd)
```js
export const inject = ['connection', 'conversationEvents', 'sessions'];
export function apply(ctx) {
  ctx.connection.api.host.describe({}).then(({ data, ok }) => {
    const cwd = ok && data ? data.cwd : undefined;
    const current = ctx.sessions.list.getSnapshot()?.current;
    // current = SessionId, cwd = workspace root
  });
}
```

---

## Concrete files read (all under the read-only reference install)

- `dsh/README.md`, `dsh/lib/plugin-*.js`, `dsh/lib/profile-boot-*.js`
- `dsh-app-boot/README.md`, `dsh-app-boot/lib/types/index.d.ts`, `.../profile.ts`
- `dsh-client-runtime/lib/index.js`, `lib/types/client/**`, `lib/client.ts`
  - esp. `.../conversation/event-registry.ts`, `.../contract/{conversation,sessions,session,store}.ts`, `.../sessions/{service,manager,remotes}.ts`
- `dsh-client-modules/README.md`, `lib/index.js`, `lib/types/client/{manifest,system}.ts`
- `dsh-client-ui-trajectory/package.json`, `lib/client.js` (apply body), `lib/types/{index.ts,client/index.ts}`
- `dsh-client-connection/README.md`, `lib/types/client/{index,api,rpc}.ts`
- `dsh-host-apiproxy/lib/types/api/{index,host,events,settings,credentials}.ts`, `.../sessions.ts`
- `dsh-native-command/README.md`
- `dsh-agent/lib/types/{runtime-types,dispatch,index}.ts`
- `dsh-session/lib/types/{types,known-event-types}.ts`
- `dsh-home-paths/lib/types/index.ts`
- `cordis/lib/types/{index,registry,fiber,context}.ts`