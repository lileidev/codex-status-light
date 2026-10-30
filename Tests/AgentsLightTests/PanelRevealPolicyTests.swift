import Foundation
import Testing
@testable import AgentsLight

/// The floating panel can be closed while AgentsLight keeps running in the menu
/// bar. These tests pin down exactly which session changes are allowed to bring
/// it back, so a routine refresh never fights the user's close.
@Suite struct PanelRevealPolicyTests {

    private func session(_ id: String, _ state: LightState) -> SessionState {
        SessionState(
            sessionID: id,
            state: state,
            message: state.label,
            cwd: "/tmp/project",
            updatedAt: Date(timeIntervalSince1970: 0),
            turnID: nil,
            source: "dsh"
        )
    }

    @Test func newActiveSessionRevealsPanel() {
        #expect(AppDelegate.shouldRevealPanel(
            previous: [:],
            current: [session("s1", .running)]
        ))
    }

    @Test func routineRunningRefreshDoesNotRevealPanel() {
        #expect(!AppDelegate.shouldRevealPanel(
            previous: ["s1": .running],
            current: [session("s1", .running)]
        ))
    }

    @Test func firstTurnAfterTerminalStateRevealsPanel() {
        for terminal in [LightState.done, .error] {
            #expect(AppDelegate.shouldRevealPanel(
                previous: ["s1": terminal],
                current: [session("s1", .running)]
            ), "a new turn after \(terminal) should bring the panel back")
        }
    }

    @Test func transitionIntoAttentionRevealsPanel() {
        #expect(AppDelegate.shouldRevealPanel(
            previous: ["s1": .running],
            current: [session("s1", .waiting)]
        ), "waiting for input should interrupt a hidden panel")
        #expect(AppDelegate.shouldRevealPanel(
            previous: ["s1": .running],
            current: [session("s1", .error)]
        ), "a new error should interrupt a hidden panel")
    }

    @Test func repeatedAttentionStateDoesNotRevealPanel() {
        #expect(!AppDelegate.shouldRevealPanel(
            previous: ["s1": .waiting],
            current: [session("s1", .waiting)]
        ))
        #expect(!AppDelegate.shouldRevealPanel(
            previous: ["s1": .error],
            current: [session("s1", .error)]
        ))
    }

    @Test func doneAndRemovedSessionsDoNotRevealPanel() {
        #expect(!AppDelegate.shouldRevealPanel(
            previous: [:],
            current: [session("s1", .done)]
        ))
        #expect(!AppDelegate.shouldRevealPanel(
            previous: ["s1": .running],
            current: [session("s1", .done)]
        ))
        #expect(!AppDelegate.shouldRevealPanel(
            previous: ["s1": .running],
            current: []
        ))
    }
}
