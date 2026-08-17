import AppKit
import Combine
import CoreServices
import Darwin
import SwiftUI

enum LightState: String, Codable, CaseIterable {
    case running
    case waiting
    case done
    case error

    var label: String {
        switch self {
        case .running: "Running"
        case .waiting: "Needs input"
        case .done: "Complete"
        case .error: "Error"
        }
    }

    var menuSymbol: String {
        switch self {
        case .running: "circle.fill"
        case .waiting: "circle.fill"
        case .done: "circle.fill"
        case .error: "circle.fill"
        }
    }

    var color: Color {
        switch self {
        case .running: .blue
        case .waiting: .yellow
        case .done: .green
        case .error: .red
        }
    }
}

/// The coding-agent a session belongs to, shown as a small indicator in front
/// of each row so lives from different terminals stay distinguishable.
enum Agent: String, CaseIterable, Codable {
    case claude
    case codex
    case opencode
    case dsh

    /// The menu-bar/reference label used in test assertions.
    /// A distinct (if imperfect) SF Symbol per agent keeps the rows readable.
    var symbol: String {
        switch self {
        case .claude: "sparkles"
        case .codex: "chevron.left.forwardslash.chevron.right"
        case .opencode: "terminal"
        case .dsh: "cpu"
        }
    }

    var tint: Color {
        switch self {
        case .claude: .orange
        case .codex: .blue
        case .opencode: .green
        // DeepSeek brand blue; only used as the fallback glyph since .dsh rows
        // render the whale image.
        case .dsh: Color(red: 0x4D / 255.0, green: 0x6B / 255.0, blue: 0xFE / 255.0)
        }
    }

    /// Name of a bundled PNG rendered as the row's glyph instead of an SF Symbol.
    /// Nil means "use `symbol` / `tint`" (the default SF Symbol path). Agents with
    /// an official brand mark ship it here so the status light shows their real
    /// icon (Codex → OpenAI blossom, DSH → DeepSeek whale).
    var imageResource: String? {
        switch self {
        case .claude: "claude-logo"
        case .codex: "openai-logo"
        case .dsh: "dsh-whale"
        case .opencode: nil
        }
    }
}

struct SessionState: Codable, Identifiable, Equatable {
    let sessionID: String
    var state: LightState
    var message: String
    var cwd: String
    var updatedAt: Date
    var turnID: String?
    var source: String?
    var isStreaming: Bool = false
    /// Resolved by StatusStore.refresh(); not persisted in the state file.
    var agent: Agent?

    var id: String { sessionID }

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case state
        case message
        case cwd
        case updatedAt = "updated_at"
        case turnID = "turn_id"
        case source
        case isStreaming = "is_streaming"
    }

    init(
        sessionID: String,
        state: LightState,
        message: String,
        cwd: String,
        updatedAt: Date,
        turnID: String?,
        source: String?,
        isStreaming: Bool = false
    ) {
        self.sessionID = sessionID
        self.state = state
        self.message = message
        self.cwd = cwd
        self.updatedAt = updatedAt
        self.turnID = turnID
        self.source = source
        self.isStreaming = isStreaming
        agent = nil
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionID = try container.decode(String.self, forKey: .sessionID)
        state = try container.decode(LightState.self, forKey: .state)
        message = try container.decode(String.self, forKey: .message)
        cwd = try container.decode(String.self, forKey: .cwd)
        updatedAt = try container.decode(Date.self, forKey: .updatedAt)
        turnID = try container.decodeIfPresent(String.self, forKey: .turnID)
        source = try container.decodeIfPresent(String.self, forKey: .source)
        isStreaming = try container.decodeIfPresent(Bool.self, forKey: .isStreaming) ?? false
        agent = nil
    }

    /// A compact label for the session: "<folder> <HHmmss>".
    /// The timestamp lets multiple instances in the same directory remain
    /// distinguishable, and avoids special characters like colons.
    var displayTitle: String {
        let folder = cwd.isEmpty ? "unknown" : URL(fileURLWithPath: cwd).lastPathComponent
        let formatter = DateFormatter()
        formatter.dateFormat = "HHmmss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return "\(folder) \(formatter.string(from: updatedAt))"
    }
}

private let staleSessionThreshold: TimeInterval = 43_200  // 12 hours

@MainActor
final class StatusStore: ObservableObject {
    @Published private(set) var sessions: [SessionState] = []

    /// All directories the light watches. Currently the shared AgentsLight
    /// directory plus the legacy OpenCode directory, so one app surfaces
    /// Claude, Codex, and OpenCode from their per-agent state files.
    let stateDirectories: [URL]
    private var cleanupTimer: Timer?
    private var fsEventStream: FSEventStreamRef?
    private let decoder: JSONDecoder

    init(stateDirectories: [URL] = StatusStore.defaultStateDirectories) {
        self.stateDirectories = stateDirectories
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        refresh()
        startWatching()
        // Periodic cleanup timer for sessions whose owning process has exited.
        // 30s is frequent enough to keep the UI tidy while avoiding the cost of
        // the old 0.75s polling loop.
        cleanupTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    static var defaultStateDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".agents-status-light/sessions", isDirectory: true)
    }

    /// The directories watched by default. All three agents (Claude, Codex,
    /// OpenCode) now write into the single shared root, so this is just that
    /// one directory.
    static var defaultStateDirectories: [URL] {
        [defaultStateDirectory]
    }

    // MARK: - FSEvents-based directory watching
    // These methods are intentionally non-isolated so they can be torn down
    // safely. The callback hops back to the MainActor for refresh().

    private func startWatching() {
        guard fsEventStream == nil else { return }

        let callback: FSEventStreamCallback = { _, clientCallBackInfo, _, _, _, _ in
            guard let info = clientCallBackInfo else { return }
            let store = Unmanaged<StatusStore>.fromOpaque(info).takeUnretainedValue()
            Task { @MainActor in
                store.refresh()
            }
        }

        let paths = stateDirectories.map { $0.path as CFString } as CFArray
        var context = FSEventStreamContext(
            version: 0,
            info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil,
            release: nil,
            copyDescription: nil
        )

        fsEventStream = FSEventStreamCreate(
            kCFAllocatorDefault,
            callback,
            &context,
            paths,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            0.5,
            FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagUseCFTypes)
        )

        if let stream = fsEventStream {
            FSEventStreamSetDispatchQueue(stream, DispatchQueue.main)
            FSEventStreamStart(stream)
        }
    }

    private func stopWatching() {
        guard let stream = fsEventStream else { return }
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        fsEventStream = nil
    }

    /// Sessions that should currently be displayed in the UI.
    ///
    /// Mirrors opencode-status-light: every loaded session is shown and sorted by
    /// priority (error > waiting > running > done), then by most recent update.
    /// The UI renders an idle state when this list is empty.
    var displaySessions: [SessionState] {
        sessions.sorted {
            let lp = priority($0.state)
            let rp = priority($1.state)
            if lp != rp { return lp > rp }
            return $0.updatedAt > $1.updatedAt
        }
    }

    /// The highest-priority display session, used for the menu-bar icon and for
    /// backward-compatible single-light consumers.
    var primary: SessionState? { displaySessions.first }

    private func priority(_ state: LightState) -> Int {
        switch state {
        case .error: 4
        case .waiting: 3
        case .running: 2
        case .done: 1
        }
    }

    /// Best-effort process name lookup for a given PID using `sysctl`.
    /// Used to clean up session files left behind by exited agent processes.
    private func processName(for pid: pid_t) -> String? {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        let result = sysctl(&mib, u_int(mib.count), &info, &size, nil, 0)
        guard result == 0 else { return nil }
        return withUnsafePointer(to: &info.kp_proc.p_comm) {
            $0.withMemoryRebound(to: CChar.self, capacity: Int(MAXCOMLEN)) {
                String(cString: $0)
            }
        }
    }

    /// The agent process names that own sessions the light tracks.
    /// Codex, Claude Code, and OpenCode all drive the same status light.
    private static let agentProcessNames = ["codex", "claude", "opencode"]

    /// Returns `true` when the session ID represents a live agent session.
    /// Numeric IDs are treated as PIDs and checked directly. Non-numeric IDs
    /// (e.g. transcript UUIDs or "manual") are kept only when at least one
    /// supported agent process is still running; this prevents stale UUID
    /// sessions from lingering after the owning agent exits.
    private func isAgentProcessAlive(sessionID: String) -> Bool {
        if let pid = pid_t(sessionID) {
            // A session file is only ever created by an agent hook using the
            // agent's own PID, so liveness is simply "is that process alive".
            // We must NOT require the process name here: the Claude Code
            // launcher exposes a version string (e.g. "2.1.227") as its
            // p_comm rather than "claude", so a name check would wrongly mark
            // every open Claude terminal as dead. The agent row icon is still
            // resolved separately by agent(for:), which falls back to claude.
            return kill(pid, 0) == 0
        }

        // Non-numeric session id: keep it only if any supported agent runs, or if
        // DSH itself is actively writing session logs (see anyDshSessionRunning).
        return anyAgentProcessRunning() || anyDshSessionRunning()
    }

    private func anyAgentProcessRunning() -> Bool {
        // `pgrep -x` matches the exact process name; multiple -x args act as
        // AND, so probe each supported agent independently and OR the results.
        return Self.agentProcessNames.contains { doesAnyProcessExist([$0]) }
    }

    /// Returns `true` while DSH is actively writing session logs.
    ///
    /// DSH runs under generic ``node``, so there is no stable process name to
    /// pgrep. Instead we treat DSH as alive whenever one of its persisted
    /// ``$DSH_HOME/sessions/**/session.jsonl.zstd`` logs changed within a short
    /// window — a live turn writes events continuously. This keeps per-session
    /// DSH rows visible while DSH is in use, and lets them age out (stale-removal
    /// below) once DSH closes.
    private func anyDshSessionRunning() -> Bool {
        let dshHome = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".dsh/sessions", isDirectory: true)
        guard let enumerator = FileManager.default.enumerator(
            at: dshHome, includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else { return false }
        let cutoff = Date().addingTimeInterval(-90)  // ~90s of no DSH writes => idle
        var found = false
        for case let url as URL in enumerator where url.lastPathComponent == "session.jsonl.zstd" {
            if let date = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate,
               date > cutoff {
                found = true
                break
            }
        }
        return found
    }

    /// Returns whether `pgrep` finds at least one process named in `names`.
    /// (pgrep with multiple -x names acts as AND across them, so call once per
    /// name and OR the results.)
    private func doesAnyProcessExist(_ names: [String]) -> Bool {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        var arguments = ["-x"]
        arguments.append(contentsOf: names)
        task.arguments = arguments
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            task.waitUntilExit()
            return task.terminationStatus == 0
        } catch {
            return true
        }
    }

    /// Tags a session row with the agent that owns it, so the icon is accurate.
    /// - A DSH session carries ``source`` field ``"dsh"`` (written by the DSH
    ///   status bridge) — tag it before the UUID/process heuristics run.
    /// - Numeric IDs are PIDs (Codex and OpenCode): resolve the live process
    ///   name.
    /// - Non-numeric IDs fall back to Claude Code transcript UUIDs.
    private func agent(for sessionID: String, source: String?) -> Agent? {
        // Explicit source tags written by the status hooks / bridge are the most
        // reliable signal. Codex emits `codex`, DSH emits `dsh`; a bare `hook:...`
        // source is a Claude session (Claude's hook reports its transcript UUID).
        if let src = source?.lowercased() {
            if src == "codex" { return .codex }
            if src == "dsh" { return .dsh }
            if src == "opencode" { return .opencode }
        }
        guard let pid = pid_t(sessionID), let name = processName(for: pid) else {
            return .claude  // UUID transcript belongs to Claude Code
        }
        if name.lowercased().contains("codex") { return .codex }
        if name.lowercased().contains("opencode") { return .opencode }
        return .claude
    }

    func refresh() {
        let now = Date()

        var pairs: [(state: SessionState, url: URL)] = []
        for directory in stateDirectories {
            guard let urls = try? FileManager.default.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]
            ) else {
                continue
            }

            for url in urls where url.pathExtension == "json" {
                guard let data = try? Data(contentsOf: url),
                      var state = try? decoder.decode(SessionState.self, from: data)
                else { continue }

                // Tag the row with its owning agent so mixed lives stay
                // distinguishable in the shared window.
                state.agent = agent(for: state.sessionID, source: state.source)

                // Remove sessions whose owning agent process has already exited.
                if !isAgentProcessAlive(sessionID: state.sessionID) {
                    try? FileManager.default.removeItem(at: url)
                    continue
                }

                pairs.append((state, url))
            }
        }

        sessions = pairs.map { $0.state }.sorted { $0.updatedAt > $1.updatedAt }

        // Clean up very old session files so the directories do not grow forever.
        for (state, url) in pairs {
            if now.timeIntervalSince(state.updatedAt) > staleSessionThreshold {
                try? FileManager.default.removeItem(at: url)
            }
        }
    }

}

struct StatusLightView: View {
    let activeState: LightState?
    let isStreaming: Bool

    var body: some View {
        let color = activeState?.color ?? .blue
        let isChasing = activeState == .running && isStreaming

        ZStack {
            if isChasing {
                StatusChaseView(color: color)
            } else {
                Circle()
                    .fill(color)
                    .frame(width: 16, height: 16)
                    .shadow(color: color.opacity(0.9), radius: 6)
            }
        }
        .frame(width: 24, height: 24)
        .padding(6)
        .background(.black.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(.white.opacity(0.18), lineWidth: 1)
        }
    }
}

/// DSH-style dot-chase: a blue ring of dots where one lit cell sweeps clockwise
/// around the 3x3 grid rim, one cell (~125ms) at a time, trailing off to dim.
/// Mirrors DSH's `_dsh-state-dot-chase` animation.
struct StatusChaseView: View {
    let color: Color

    // Cell centroids on a 0..8 grid in sweep order (clockwise around the rim),
    // matching DSH's original 3x3 edge positions.
    static let cells: [CGPoint] = [
        .init(x: 0, y: 0), .init(x: 4, y: 0), .init(x: 8, y: 0),
        .init(x: 8, y: 4),
        .init(x: 8, y: 8), .init(x: 4, y: 8), .init(x: 0, y: 8), .init(x: 0, y: 4),
    ]

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.06)) { context in
            ChaseContent(date: context.date, cells: Self.cells, color: color)
        }
    }
}

/// Renders the dot-chase ring for a given animation date; a single lit dot
/// sweeps clockwise around the 8 rim cells each second.
private struct ChaseContent: View {
    let date: Date
    let cells: [CGPoint]
    let color: Color

    var body: some View {
        let phase = date.timeIntervalSinceReferenceDate / 0.125
        return ZStack {
            ForEach(Array(cells.enumerated()), id: \.offset) { pair in
                let i = pair.offset
                let cell = pair.element
                let d = Self.ringDistance(Int(i), bright: phase)
                let intensity = d <= 0.05 ? 1.0 : max(0.14, 1.0 - d / 2.6)
                Circle()
                    .fill(color.opacity(intensity))
                    .frame(width: 4.4, height: 4.4)
                    // Map the 0..8 grid onto a 16x16 square whose side equals the
                    // steady dot's diameter, so the chase reads at the same size.
                    .position(x: 4 + cell.x * 2, y: 4 + cell.y * 2)
            }
        }
    }

    /// Ring distance (0..8 cells) of cell `index` from the current bright phase.
    private static func ringDistance(_ index: Int, bright: Double) -> Double {
        var d = Double(index) - bright
        d = (d.truncatingRemainder(dividingBy: 8) + 8).truncatingRemainder(dividingBy: 8)
        return d
    }
}

struct MenuStatusIcon: View {
    let state: LightState?
    let label: String

    var body: some View {
        ZStack {
            Image(systemName: state?.menuSymbol ?? "circle")
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(state?.color ?? .green)

            Text(label)
                .font(.system(size: 8, weight: .bold))
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.8), radius: 0.5)
        }
    }
}

struct SessionRowView: View {
    let session: SessionState

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            AgentIndicator(agent: session.agent)
            StatusLightView(activeState: session.state, isStreaming: session.isStreaming)
            VStack(alignment: .leading, spacing: 2) {
                Text(session.displayTitle)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(session.message)
                    .font(.caption)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

/// A small glyph marking which agent produced a session. Falls back to a
/// neutral SF Symbol when no brand image present. Agents with an official brand
/// mark (Codex → OpenAI blossom, DSH → DeepSeek whale) render their bundled PNG.
struct AgentIndicator: View {
    let agent: Agent?

    var body: some View {
        Group {
            if let resource = agent?.imageResource,
               let path = Bundle.main.path(forResource: resource, ofType: "png")
                          ?? Bundle.module.path(forResource: resource, ofType: "png"),
               let nsImage = NSImage(contentsOfFile: path) {
                Image(nsImage: nsImage)
                    .resizable()
                    .scaledToFill()
                    .frame(width: 14, height: 14)
                    .clipShape(RoundedRectangle(cornerRadius: 3, style: .continuous))
            } else {
                Image(systemName: agent?.symbol ?? "circle")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(agent?.tint ?? .secondary)
                    .frame(width: 14, height: 14)
            }
        }
        .help(agent?.rawValue ?? "Unknown agent")
    }
}

struct StatusContentView: View {
    @ObservedObject var store: StatusStore
    @State private var window: NSWindow?

    var body: some View {
        ZStack(alignment: .topTrailing) {
            VStack(alignment: .leading, spacing: 8) {
                if store.displaySessions.isEmpty {
                    HStack(alignment: .top, spacing: 8) {
                        StatusLightView(activeState: .done, isStreaming: false)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Idle")
                                .font(.caption)
                            Text("Waiting for an agent task")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                } else {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(store.displaySessions) { session in
                            SessionRowView(session: session)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
            .padding(12)
            .frame(width: 260)
            .background(
                RoundedRectangle(cornerRadius: 16)
                    .fill(.ultraThinMaterial)
                    .overlay(RoundedRectangle(cornerRadius: 16).fill(.black.opacity(0.25)))
                    .overlay(RoundedRectangle(cornerRadius: 16).stroke(.white.opacity(0.18), lineWidth: 1))
            )

            Button {
                window?.close()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.white.opacity(0.7))
                    .padding(8)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.top, 8)
            .padding(.trailing, 8)
        }
        .background(FloatingWindowAccessor { window = $0 })
    }
}

struct FloatingWindowAccessor: NSViewRepresentable {
    let onWindow: (NSWindow) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            if let window = view.window {
                onWindow(window)
            }
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let store = StatusStore()
    private var statusWindow: NSPanel?
    private var cancellables = Set<AnyCancellable>()

    /// Height needed to show all sessions without scrolling.
    /// - One session / idle row needs about 120 pt.
    /// - Each additional session adds roughly one row height.
    private func windowHeight(for sessionCount: Int) -> CGFloat {
        let rowHeight: CGFloat = 56
        let baseHeight: CGFloat = 64
        return max(120, baseHeight + CGFloat(sessionCount) * rowHeight)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.showWindow()
        }

        // Resize the floating window whenever the underlying sessions change.
        store.$sessions
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.resizeWindow() }
            .store(in: &cancellables)
    }

    func showWindow() {
        if statusWindow == nil {
            let height = windowHeight(for: store.displaySessions.count)
            let window = NSPanel(
                contentRect: NSRect(x: 0, y: 0, width: 260, height: height),
                styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            // No system title bar; the SwiftUI view provides its own close button.
            window.title = "AgentsLight"
            window.contentView = NSHostingView(rootView: StatusContentView(store: store))
            window.level = .floating
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            window.isOpaque = false
            window.backgroundColor = .clear
            window.isFloatingPanel = true
            window.hidesOnDeactivate = false
            window.becomesKeyOnlyIfNeeded = true
            window.isReleasedWhenClosed = false
            window.isMovableByWindowBackground = true
            window.hasShadow = true
            let targetScreen = NSScreen.screens.first ?? NSScreen.main
            if let visibleFrame = targetScreen?.visibleFrame {
                let origin = NSPoint(
                    x: visibleFrame.maxX - window.frame.width - 24,
                    y: visibleFrame.maxY - height - 24
                )
                window.setFrameOrigin(origin)
            } else {
                window.center()
            }
            statusWindow = window
        }

        statusWindow?.orderFrontRegardless()
    }

    private func resizeWindow() {
        guard let window = statusWindow else { return }

        let newHeight = windowHeight(for: store.displaySessions.count)
        let frame = window.frame
        let newOrigin = NSPoint(
            x: frame.maxX - frame.width,
            y: frame.maxY - newHeight
        )
        window.setFrame(
            NSRect(x: newOrigin.x, y: newOrigin.y, width: frame.width, height: newHeight),
            display: true,
            animate: false
        )
    }
}

@main
struct AgentsLightApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra {
            MenuStatusView(store: appDelegate.store) {
                appDelegate.showWindow()
            }
        } label: {
            let state = appDelegate.store.primary?.state
            MenuStatusIcon(state: state, label: "A")
        }
    }
}

struct MenuStatusView: View {
    @ObservedObject var store: StatusStore
    let showWindow: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if store.displaySessions.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    StatusLightView(activeState: .done, isStreaming: false)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Idle")
                            .font(.caption)
                        Text("Waiting for an agent task")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 6)
                Divider()
            } else {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(store.displaySessions) { session in
                        SessionRowView(session: session)
                    }
                }
                .padding(.vertical, 6)
                Divider()
            }

            VStack(alignment: .leading, spacing: 4) {
                Button("Show floating light") {
                    showWindow()
                }
                Button("Refresh") { store.refresh() }
                Divider()
                Button("Quit") { NSApp.terminate(nil) }
            }
            .padding(.vertical, 8)
        }
        .padding(.horizontal, 12)
        .frame(width: 260)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(.ultraThinMaterial)
                .overlay(RoundedRectangle(cornerRadius: 12).fill(.black.opacity(0.25)))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.18), lineWidth: 1))
        )
    }
}
