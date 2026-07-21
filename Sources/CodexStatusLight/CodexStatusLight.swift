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
        case .running: "circle.dotted"
        case .waiting: "circle.fill"
        case .done: "circle.fill"
        case .error: "circle.fill"
        }
    }

    var color: Color {
        switch self {
        case .running: .secondary
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

    var id: String { sessionID }

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case state
        case message
        case cwd
        case updatedAt = "updated_at"
        case turnID = "turn_id"
    }
}

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
        let recent = sessions.filter { Date().timeIntervalSince($0.updatedAt) < 43_200 }
        return recent.max { lhs, rhs in
            let lp = priority(lhs.state)
            let rp = priority(rhs.state)
            return lp == rp ? lhs.updatedAt < rhs.updatedAt : lp < rp
        } ?? sessions.first
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

        sessions = urls.compactMap { url in
            guard url.pathExtension == "json",
                  let data = try? Data(contentsOf: url),
                  let state = try? decoder.decode(SessionState.self, from: data)
            else { return nil }
            return state
        }.sorted { $0.updatedAt > $1.updatedAt }
    }

    private func priority(_ state: LightState) -> Int {
        switch state {
        case .error: 4
        case .waiting: 3
        case .running: 2
        case .done: 1
        }
    }
}

struct TrafficLightView: View {
    let activeState: LightState

    var body: some View {
        VStack(spacing: 10) {
            lamp(.error, color: .red)
            lamp(.waiting, color: .yellow)
            lamp(.done, color: .green)
        }
        .padding(12)
        .background(.black.opacity(0.88), in: RoundedRectangle(cornerRadius: 22))
        .overlay {
            RoundedRectangle(cornerRadius: 22)
                .stroke(.white.opacity(0.14), lineWidth: 1)
        }
    }

    private func lamp(_ state: LightState, color: Color) -> some View {
        Circle()
            .fill(activeState == state ? color : color.opacity(0.14))
            .frame(width: 42, height: 42)
            .shadow(color: activeState == state ? color.opacity(0.9) : .clear, radius: 10)
    }
}

struct StatusContentView: View {
    @ObservedObject var store: StatusStore

    var body: some View {
        let primary = store.primary
        HStack(alignment: .top, spacing: 16) {
            TrafficLightView(activeState: primary?.state ?? .running)

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Circle()
                        .fill(primary?.state.color ?? .secondary)
                        .frame(width: 10, height: 10)
                    Text(primary?.state.label ?? "Idle")
                        .font(.headline)
                }

                Text(primary?.message ?? "Waiting for a Codex task")
                    .font(.body)
                    .lineLimit(3)
                    .frame(maxWidth: 270, alignment: .leading)

                if let cwd = primary?.cwd, !cwd.isEmpty {
                    Text(URL(fileURLWithPath: cwd).lastPathComponent)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if store.sessions.count > 1 {
                    Divider()
                    Text("\(store.sessions.count) recent sessions")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.top, 6)
        }
        .padding(18)
        .frame(minWidth: 390, minHeight: 190)
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

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }
}

@main
struct CodexStatusLightApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = StatusStore()

    var body: some Scene {
        MenuBarExtra {
            MenuStatusView(store: store)
        } label: {
            let state = store.primary?.state ?? .running
            Image(systemName: state.menuSymbol)
                .symbolRenderingMode(.palette)
                .foregroundStyle(state.color)
        }

        Window("Codex Status Light", id: "status-light") {
            StatusContentView(store: store)
        }
        .windowResizability(.contentSize)
    }
}

struct MenuStatusView: View {
    @ObservedObject var store: StatusStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let primary = store.primary
        VStack(alignment: .leading, spacing: 8) {
            Label(primary?.state.label ?? "Idle", systemImage: primary?.state.menuSymbol ?? "circle")
            Text(primary?.message ?? "Waiting for a Codex task")
                .font(.caption)
                .lineLimit(2)
            Divider()
            Button("Show floating light") {
                openWindow(id: "status-light")
                NSApp.activate(ignoringOtherApps: true)
            }
            Button("Refresh") { store.refresh() }
            Divider()
            Button("Quit") { NSApp.terminate(nil) }
        }
        .padding(8)
        .frame(width: 260)
    }
}
