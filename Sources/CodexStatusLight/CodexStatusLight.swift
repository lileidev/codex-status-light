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

struct SessionState: Codable, Identifiable, Equatable {
    let sessionID: String
    var state: LightState
    var message: String
    var cwd: String
    var updatedAt: Date
    var turnID: String?
    var source: String?
    var isStreaming: Bool = false

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

    let stateDirectory: URL
    private var cleanupTimer: Timer?
    private var fsEventStream: FSEventStreamRef?
    private let decoder: JSONDecoder

    init(stateDirectory: URL = StatusStore.defaultStateDirectory) {
        self.stateDirectory = stateDirectory
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        refresh()
        startWatching()
        // Low-frequency cleanup timer for stale sessions (older than 12h).
        cleanupTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    static var defaultStateDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".codex/status-light/sessions", isDirectory: true)
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

        let path = stateDirectory.path as CFString
        let paths = [path] as CFArray
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
    /// Used to clean up session files left behind by exited Codex processes.
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

    /// Returns `true` only when the numeric session ID looks like a live Codex
    /// process. Non-numeric IDs (e.g. "manual") are left untouched.
    private func isCodexProcessAlive(sessionID: String) -> Bool {
        guard let pid = pid_t(sessionID) else { return true }
        if kill(pid, 0) != 0 { return false }
        guard let name = processName(for: pid) else { return false }
        return name.lowercased().contains("codex")
    }

    func refresh() {
        guard let urls = try? FileManager.default.contentsOfDirectory(
            at: stateDirectory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else {
            sessions = []
            return
        }

        let now = Date()

        var pairs: [(state: SessionState, url: URL)] = []
        for url in urls where url.pathExtension == "json" {
            guard let data = try? Data(contentsOf: url),
                  let state = try? decoder.decode(SessionState.self, from: data)
            else { continue }

            // Remove sessions whose Codex process has already exited.
            if !isCodexProcessAlive(sessionID: state.sessionID) {
                try? FileManager.default.removeItem(at: url)
                continue
            }

            pairs.append((state, url))
        }

        sessions = pairs.map { $0.state }.sorted { $0.updatedAt > $1.updatedAt }

        // Clean up very old session files so the directory does not grow forever.
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
        TimelineView(.periodic(from: .now, by: 0.55)) { context in
            let runningOn = Int(context.date.timeIntervalSinceReferenceDate / 0.55).isMultiple(of: 2)
            let color = activeState?.color ?? .green
            let illuminated: Bool = {
                guard let activeState else { return false }
                if activeState == .running && isStreaming {
                    return runningOn
                }
                return true
            }()

            Circle()
                .fill(illuminated ? color : color.opacity(0.14))
                .frame(width: 24, height: 24)
                .shadow(color: illuminated ? color.opacity(0.9) : .clear, radius: 6)
                .padding(6)
                .background(.black.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
                .overlay {
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(.white.opacity(0.18), lineWidth: 1)
                }
        }
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
                            Text("Waiting for a Codex task")
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
            window.title = "Codex Status Light"
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
struct CodexStatusLightApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra {
            MenuStatusView(store: appDelegate.store) {
                appDelegate.showWindow()
            }
        } label: {
            let state = appDelegate.store.primary?.state
            MenuStatusIcon(state: state, label: "C")
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
                        Text("Waiting for a Codex task")
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
