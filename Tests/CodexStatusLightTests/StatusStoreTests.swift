import Foundation
import Testing
@testable import CodexStatusLight

@Suite @MainActor
struct StatusStoreTests {
    @Test func runningUsesBlinkingFilledIndicator() {
        #expect(LightState.allCases.count == 4)
        #expect(LightState.running.blinks)
        #expect(LightState.running.menuSymbol == "circle.fill")
        #expect(!LightState.error.blinks)
        #expect(!LightState.waiting.blinks)
        #expect(!LightState.done.blinks)
    }

    @Test func errorHasPriorityOverNewerDoneState() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let error = SessionState(
            sessionID: "error", state: .error, message: "failed", cwd: "/tmp",
            updatedAt: Date().addingTimeInterval(-10), turnID: nil
        )
        let done = SessionState(
            sessionID: "done", state: .done, message: "complete", cwd: "/tmp",
            updatedAt: Date(), turnID: nil
        )
        try encoder.encode(error).write(to: directory.appendingPathComponent("error.json"))
        try encoder.encode(done).write(to: directory.appendingPathComponent("done.json"))

        let store = StatusStore(stateDirectory: directory)
        #expect(store.primary?.state == .error)
    }
}
