import Foundation
import Testing
@testable import CodexStatusLight

@Suite @MainActor
struct StatusStoreTests {

    @Test func everyStateUsesStableFilledMenuIndicator() {
        #expect(LightState.allCases.count == 4)
        #expect(LightState.allCases.allSatisfy { $0.menuSymbol == "circle.fill" })
    }

    @Test func priorityWinsOverRecency() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        let states: [(LightState, String, TimeInterval)] = [
            (.error, "error-session", -30),
            (.running, "running-session", -5),
            (.done, "done-session", -15),
        ]

        for (state, id, offset) in states {
            let session = SessionState(
                sessionID: id,
                state: state,
                message: state.rawValue,
                cwd: "/tmp",
                updatedAt: now.addingTimeInterval(offset),
                turnID: nil,
                source: "test",
                isStreaming: false
            )
            let data = try encoder.encode(session)
            let url = directory.appendingPathComponent("\(id).json")
            try data.write(to: url)
        }

        let store = StatusStore(stateDirectory: directory)
        store.refresh()

        #expect(store.sessions.count == 3)
        #expect(store.primary?.state == .error, "highest-priority active session (error) should win over more recent running")
    }

    @Test func primaryFallsBackToMostRecentDoneWhenAllSessionsAreStale() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        let states: [(LightState, String, TimeInterval)] = [
            (.done, "old-done", -120),
            (.error, "old-error", -180),
        ]

        for (state, id, offset) in states {
            let session = SessionState(
                sessionID: id,
                state: state,
                message: state.rawValue,
                cwd: "/tmp",
                updatedAt: now.addingTimeInterval(offset),
                turnID: nil,
                source: "test",
                isStreaming: false
            )
            let data = try encoder.encode(session)
            let url = directory.appendingPathComponent("\(id).json")
            try data.write(to: url)
        }

        let store = StatusStore(stateDirectory: directory)
        store.refresh()

        #expect(store.sessions.count == 2)
        #expect(store.primary?.state == .done, "stale done session should keep the idle light green")
    }

    @Test func returnsMostRecentActiveSession() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        let states: [(LightState, String, TimeInterval)] = [
            (.error, "error-session", -120),
            (.running, "running-session", -10),
            (.done, "done-session", -20),
        ]

        for (state, id, offset) in states {
            let session = SessionState(
                sessionID: id,
                state: state,
                message: state.rawValue,
                cwd: "/tmp",
                updatedAt: now.addingTimeInterval(offset),
                turnID: nil,
                source: "test",
                isStreaming: false
            )
            let data = try encoder.encode(session)
            let url = directory.appendingPathComponent("\(id).json")
            try data.write(to: url)
        }

        let store = StatusStore(stateDirectory: directory)
        store.refresh()

        #expect(store.sessions.count == 3)
        #expect(store.primary?.state == .running, "most recently updated active session should win")
    }

    @Test func removesStaleSessionFiles() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        // Create the store before writing the stale file so the cleanup happens
        // during the explicit refresh() call below, not during init.
        let store = StatusStore(stateDirectory: directory)

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        let oldSession = SessionState(
            sessionID: "ancient",
            state: .done,
            message: "ancient",
            cwd: "/tmp",
            updatedAt: now.addingTimeInterval(-86_400),
            turnID: nil,
            source: "test",
            isStreaming: false
        )
        let data = try encoder.encode(oldSession)
        let url = directory.appendingPathComponent("ancient.json")
        try data.write(to: url)

        store.refresh()

        // The session is read before cleanup, so it appears in memory once.
        #expect(store.sessions.count == 1)
        #expect(FileManager.default.fileExists(atPath: url.path) == false, "files older than 12 hours should be removed")
    }

    @Test func defaultDirectory() {
        let path = StatusStore.defaultStateDirectory.path
        #expect(path.hasSuffix(".codex/status-light/sessions"))
    }

    @Test func primaryIsNilWhenOnlyStaleNonDoneSessions() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        let session = SessionState(
            sessionID: "old-error",
            state: .error,
            message: "old error",
            cwd: "/tmp",
            updatedAt: now.addingTimeInterval(-120),
            turnID: nil,
            source: "test",
            isStreaming: false
        )
        let data = try encoder.encode(session)
        let url = directory.appendingPathComponent("old-error.json")
        try data.write(to: url)

        let store = StatusStore(stateDirectory: directory)
        store.refresh()

        #expect(store.primary == nil, "stale non-done sessions should not keep the light on")
    }

    @Test func primaryIsNilWhenNoSessions() {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = StatusStore(stateDirectory: directory)
        #expect(store.sessions.isEmpty)
        #expect(store.primary == nil, "no sessions returns nil; UI defaults to done/green")
    }

    @Test func defaultsIsStreamingToFalse() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let updatedAt = ISO8601DateFormatter().string(from: Date())
        let legacy = """
        {"session_id":"legacy","state":"running","message":"legacy","cwd":"/tmp","updated_at":"\(updatedAt)","source":"test"}
        """
        let url = directory.appendingPathComponent("legacy.json")
        try legacy.write(to: url, atomically: true, encoding: .utf8)

        let store = StatusStore(stateDirectory: directory)
        store.refresh()

        #expect(store.sessions.count == 1)
        #expect(store.primary?.isStreaming == false, "missing is_streaming should default to false")
    }

    @Test func isStreamingRoundTrips() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let session = SessionState(
            sessionID: "streaming",
            state: .running,
            message: "streaming test",
            cwd: "/tmp",
            updatedAt: Date(),
            turnID: nil,
            source: "test",
            isStreaming: true
        )
        let data = try encoder.encode(session)
        let url = directory.appendingPathComponent("streaming.json")
        try data.write(to: url)

        let store = StatusStore(stateDirectory: directory)
        store.refresh()

        #expect(store.primary?.isStreaming == true)
    }
}
