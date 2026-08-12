import Foundation
import Testing
@testable import AgentsLight

@Suite @MainActor
struct StatusStoreTests {

    @Test func everyAgentHasStableDistinctIcon() {
        #expect(Agent.allCases.count == 3)
        #expect(Set(Agent.allCases.map { $0.symbol }).count == Agent.allCases.count,
                "each agent gets its own icon so rows stay distinguishable")
        #expect(Set(Agent.allCases.map { $0.rawValue }).count == 3)
    }

    @Test func everyStateUsesStableFilledMenuIndicator() {
        #expect(LightState.allCases.count == 4)
        #expect(LightState.allCases.allSatisfy { $0.menuSymbol == "circle.fill" })
    }

    @Test func defaultDirectory() {
        let path = StatusStore.defaultStateDirectory.path
        #expect(path.hasSuffix(".agents-status-light/sessions"))
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

        let store = StatusStore(stateDirectories: [directory])
        store.refresh()

        #expect(store.sessions.count == 3)
        #expect(store.primary?.state == .error, "highest-priority session (error) should win over more recent running")
    }

    @Test func priorityWinsOverRecencyRegardlessOfAge() throws {
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

        let store = StatusStore(stateDirectories: [directory])
        store.refresh()

        #expect(store.sessions.count == 2)
        #expect(store.primary?.state == .error, "error should outrank done even when older")
    }

    @Test func returnsMostRecentSessionWhenPrioritiesAreEqual() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        let states: [(LightState, String, TimeInterval)] = [
            (.running, "older-running", -20),
            (.running, "recent-running", -5),
            (.done, "done-session", -10),
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

        let store = StatusStore(stateDirectories: [directory])
        store.refresh()

        #expect(store.sessions.count == 3)
        #expect(store.primary?.sessionID == "recent-running", "when priorities are equal, the most recent session should win")
    }

    @Test func displaySessionsShowsAllSessionsSortedByPriorityThenRecency() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        let states: [(LightState, String, TimeInterval)] = [
            (.done, "old-done", -120),
            (.done, "recent-done", -30),
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

        let store = StatusStore(stateDirectories: [directory])
        store.refresh()

        #expect(store.displaySessions.count == 3)
        #expect(store.displaySessions[0].sessionID == "old-error", "error should rank first regardless of age")
        #expect(store.displaySessions[1].sessionID == "recent-done", "more recent done should come next")
        #expect(store.displaySessions[2].sessionID == "old-done")
    }

    @Test func aggregatesSessionsAcrossMultipleDirectories() throws {
        let dirA = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let dirB = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dirA, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: dirB, withIntermediateDirectories: true)
        defer {
            try? FileManager.default.removeItem(at: dirA)
            try? FileManager.default.removeItem(at: dirB)
        }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let now = Date()

        // One session in each watched directory; both must appear.
        let inA = SessionState(
            sessionID: "dir-a-session", state: .running, message: "a", cwd: "/tmp",
            updatedAt: now, turnID: nil, source: "test", isStreaming: false
        )
        let inB = SessionState(
            sessionID: "dir-b-session", state: .done, message: "b", cwd: "/tmp",
            updatedAt: now, turnID: nil, source: "test", isStreaming: false
        )
        try encoder.encode(inA).write(to: dirA.appendingPathComponent("dir-a-session.json"))
        try encoder.encode(inB).write(to: dirB.appendingPathComponent("dir-b-session.json"))

        let store = StatusStore(stateDirectories: [dirA, dirB])
        store.refresh()

        #expect(store.sessions.count == 2)
        #expect(store.sessions.map { $0.sessionID }.contains("dir-a-session"))
        #expect(store.sessions.map { $0.sessionID }.contains("dir-b-session"))
    }

    @Test func defaultDirectoriesAreTheSingleSharedRoot() {
        let paths = StatusStore.defaultStateDirectories.map { $0.path }
        #expect(paths.count == 1)
        #expect(paths[0].hasSuffix(".agents-status-light/sessions"),
                "all agents now write the shared root; no per-agent legacy dir is watched")
    }

    @Test func primaryKeepsErrorSessionVisibleUntilTwelveHourCleanup() throws {
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

        let store = StatusStore(stateDirectories: [directory])
        store.refresh()

        #expect(store.primary?.state == .error, "error session should stay visible until the 12h stale cleanup")
    }

    @Test func primaryIsNilWhenNoSessions() {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = StatusStore(stateDirectories: [directory])
        #expect(store.sessions.isEmpty)
        #expect(store.primary == nil, "no sessions returns nil; UI defaults to done/green")
    }

    @Test func removesStaleSessionFiles() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = StatusStore(stateDirectories: [directory])

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

        #expect(store.sessions.count == 1)
        #expect(FileManager.default.fileExists(atPath: url.path) == false, "files older than 12 hours should be removed")
    }

    @Test func removesSessionFileForDeadProcess() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = StatusStore(stateDirectories: [directory])
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601

        let deadSession = SessionState(
            sessionID: "999999",
            state: .running,
            message: "dead process",
            cwd: "/tmp",
            updatedAt: Date(),
            turnID: nil,
            source: "test",
            isStreaming: false
        )
        let data = try encoder.encode(deadSession)
        let url = directory.appendingPathComponent("999999.json")
        try data.write(to: url)

        store.refresh()

        #expect(store.sessions.isEmpty, "session for a non-existent process should be removed")
        #expect(FileManager.default.fileExists(atPath: url.path) == false, "dead process session file should be deleted")
    }

    @Test func keepsSessionFileForNonNumericSessionID() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = StatusStore(stateDirectories: [directory])
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601

        let manualSession = SessionState(
            sessionID: "manual-cli-session",
            state: .done,
            message: "manual update",
            cwd: "/tmp",
            updatedAt: Date(),
            turnID: nil,
            source: "cli",
            isStreaming: false
        )
        let data = try encoder.encode(manualSession)
        let url = directory.appendingPathComponent("manual-cli-session.json")
        try data.write(to: url)

        store.refresh()

        #expect(store.sessions.count == 1, "non-numeric session IDs should not be auto-cleaned")
        #expect(FileManager.default.fileExists(atPath: url.path), "manual session file should be preserved")
    }

    @Test func staleWaitingSessionRemainsPrimary() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = StatusStore(stateDirectories: [directory])
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601

        let waitingSession = SessionState(
            sessionID: "waiting-old",
            state: .waiting,
            message: "needs input",
            cwd: "/tmp",
            updatedAt: Date().addingTimeInterval(-120),
            turnID: nil,
            source: "test",
            isStreaming: false
        )
        let data = try encoder.encode(waitingSession)
        let url = directory.appendingPathComponent("waiting-old.json")
        try data.write(to: url)

        store.refresh()

        #expect(store.primary?.state == .waiting, "a waiting session should stay active until the user responds")
    }

    @Test func staleRunningSessionRemainsPrimary() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = StatusStore(stateDirectories: [directory])
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601

        let runningSession = SessionState(
            sessionID: "running-old",
            state: .running,
            message: "thinking",
            cwd: "/tmp",
            updatedAt: Date().addingTimeInterval(-120),
            turnID: nil,
            source: "test",
            isStreaming: false
        )
        let data = try encoder.encode(runningSession)
        let url = directory.appendingPathComponent("running-old.json")
        try data.write(to: url)

        store.refresh()

        #expect(store.primary?.state == .running, "a running session should stay active while the model is thinking without output")
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

        let store = StatusStore(stateDirectories: [directory])
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

        let store = StatusStore(stateDirectories: [directory])
        store.refresh()

        #expect(store.primary?.isStreaming == true)
    }

    @Test func displayTitleUsesFolderAndTimestampWithoutSpecialCharacters() {
        let formatter = DateFormatter()
        formatter.dateFormat = "HHmmss"
        formatter.locale = Locale(identifier: "en_US_POSIX")

        let updatedAt = Date()
        let session = SessionState(
            sessionID: "test",
            state: .running,
            message: "test",
            cwd: "/Users/larry/project",
            updatedAt: updatedAt,
            turnID: nil,
            source: "test"
        )

        let title = session.displayTitle
        #expect(title.hasPrefix("project "), "title should start with the cwd folder name")
        let timestamp = String(title.dropFirst("project ".count))
        #expect(timestamp == formatter.string(from: updatedAt), "timestamp should match HHmmss format")
        #expect(timestamp.rangeOfCharacter(from: CharacterSet(charactersIn: ":/.")) == nil, "timestamp should not contain special characters")
    }
}
