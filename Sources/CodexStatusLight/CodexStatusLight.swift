import AppKit
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
        source: String? = nil,
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
}

private let activeSessionThreshold: TimeInterval = 60
private let staleSessionThreshold: TimeInterval = 43_200

@MainActor
final class StatusStore: ObservableObject {
    @Published private(set) var sessions: [SessionState] = []

    let stateDirectory: URL
    private var timer: Timer?
    private let decoder: JSONDecoder

    init(stateDirectory: URL = StatusStore.defaultStateDirectory) {
        self.stateDirectory = stateDirectory
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 0.75, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    static var defaultStateDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".codex/status-light/sessions", isDirectory: true)
    }

    var primary: SessionState? {
        let active = sessions.filter { Date().timeIntervalSince($0.updatedAt) < activeSessionThreshold }
        if !active.isEmpty {
            return active.max { lhs, rhs in
                let lp = priority(lhs.state)
                let rp = priority(rhs.state)
                return lp == rp ? lhs.updatedAt < rhs.updatedAt : lp < rp
            }
        }
        return sessions.filter { $0.state == .done }.max(by: { $0.updatedAt < $1.updatedAt })
    }

    private func priority(_ state: LightState) -> Int {
        switch state {
        case .error: 4
        case .waiting: 3
        case .running: 2
        case .done: 1
        }
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

        sessions = urls.compactMap { url in
            guard url.pathExtension == "json",
                  let data = try? Data(contentsOf: url),
                  let state = try? decoder.decode(SessionState.self, from: data)
            else { return nil }
            return state
        }.sorted { $0.updatedAt > $1.updatedAt }

        for url in urls where url.pathExtension == "json" {
            guard let data = try? Data(contentsOf: url),
                  let state = try? decoder.decode(SessionState.self, from: data)
            else { continue }
            if now.timeIntervalSince(state.updatedAt) > staleSessionThreshold {
                try? FileManager.default.removeItem(at: url)
            }
        }
    }
}

struct TrafficLightView: View {
    let activeState: LightState?
    let isStreaming: Bool

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.55)) { context in
            let runningOn = Int(context.date.timeIntervalSinceReferenceDate / 0.55).isMultiple(of: 2)
            HStack(spacing: 4) {
                lamp(.error, color: .red, illuminated: activeState == .error)
                lamp(.waiting, color: .yellow, illuminated: activeState == .waiting)
                lamp(.done, color: .green, illuminated: activeState == .done)
                lamp(.running, color: .blue, illuminated: activeState == .running && (!isStreaming || runningOn))
            }
            .padding(6)
            .background(.black.opacity(0.88), in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .stroke(.white.opacity(0.14), lineWidth: 1)
            }
        }
    }

    private func lamp(_ state: LightState, color: Color, illuminated: Bool) -> some View {
        Circle()
            .fill(illuminated ? color : color.opacity(0.14))
            .frame(width: 24, height: 24)
            .shadow(color: illuminated ? color.opacity(0.9) : .clear, radius: 6)
    }
}

struct MenuStatusIcon: View {
    let state: LightState?

    var body: some View {
        Image(systemName: state?.menuSymbol ?? "circle")
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(state?.color ?? .green)
    }
}

struct StatusContentView: View {
    @ObservedObject var store: StatusStore

    var body: some View {
        let primary = store.primary
        VStack(alignment: .leading, spacing: 8) {
            TrafficLightView(activeState: primary?.state, isStreaming: primary?.isStreaming ?? false)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Circle()
                        .fill(primary?.state.color ?? .secondary)
                        .frame(width: 8, height: 8)
                    Text(primary?.state.label ?? "Idle")
                        .font(.subheadline)
                }

                Text(primary?.message ?? "Waiting for a Codex task")
                    .font(.caption)
                    .lineLimit(2)
                    .frame(maxWidth: 160, alignment: .leading)

                if let cwd = primary?.cwd, !cwd.isEmpty {
                    Text(URL(fileURLWithPath: cwd).lastPathComponent)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                if store.sessions.count > 1 {
                    Divider()
                    Text("\(store.sessions.count) recent sessions")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .frame(minWidth: 180, minHeight: 110)
        .background(FloatingWindowAccessor())
    }
}

struct FloatingWindowAccessor: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            view.window?.level = .floating
            view.window?.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let store = StatusStore()
    private var statusWindow: NSPanel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.showWindow()
        }
    }

    func showWindow() {
        if statusWindow == nil {
            let window = NSPanel(
                contentRect: NSRect(x: 0, y: 0, width: 200, height: 130),
                styleMask: [.titled, .closable, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            window.title = "Codex Status Light"
            window.contentView = NSHostingView(rootView: StatusContentView(store: store))
            window.level = .floating
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            window.isFloatingPanel = true
            window.hidesOnDeactivate = false
            window.becomesKeyOnlyIfNeeded = true
            window.isReleasedWhenClosed = false
            let targetScreen = NSScreen.screens.first ?? NSScreen.main
            if let visibleFrame = targetScreen?.visibleFrame {
                let origin = NSPoint(
                    x: visibleFrame.maxX - window.frame.width - 24,
                    y: visibleFrame.maxY - window.frame.height - 24
                )
                window.setFrameOrigin(origin)
            } else {
                window.center()
            }
            statusWindow = window
        }

        statusWindow?.orderFrontRegardless()
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
            MenuStatusIcon(state: state)
        }
    }
}

struct MenuStatusView: View {
    @ObservedObject var store: StatusStore
    let showWindow: () -> Void

    var body: some View {
        let primary = store.primary
        VStack(alignment: .leading, spacing: 8) {
            Label(primary?.state.label ?? "Idle", systemImage: primary?.state.menuSymbol ?? "circle")
            Text(primary?.message ?? "Waiting for a Codex task")
                .font(.caption)
                .lineLimit(2)
            Divider()
            Button("Show floating light") {
                showWindow()
            }
            Button("Refresh") { store.refresh() }
            Divider()
            Button("Quit") { NSApp.terminate(nil) }
        }
        .padding(8)
        .frame(width: 260)
    }
}
