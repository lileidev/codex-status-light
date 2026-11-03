import Foundation
import Testing
@testable import AgentsLight

/// The floating panel's height follows the content it hosts, so a panel with one
/// row no longer keeps the empty space the old fixed-per-row height reserved.
@Suite struct PanelHeightTests {

    @Test func contentHeightIsRoundedUpToAWholePoint() {
        #expect(AppDelegate.panelHeight(forContentHeight: 202.4) == 203)
        #expect(AppDelegate.panelHeight(forContentHeight: 202.6) == 203)
    }

    @Test func heightMatchesContent() {
        #expect(AppDelegate.panelHeight(forContentHeight: 60) == 60)
        #expect(AppDelegate.panelHeight(forContentHeight: 207) == 207)
    }

    @Test func tallerContentGrowsThePanel() {
        let one = AppDelegate.panelHeight(forContentHeight: 64)
        let four = AppDelegate.panelHeight(forContentHeight: 202)
        #expect(four > one)
    }

    @Test func panelWidthIsFixed() {
        #expect(AppDelegate.panelWidth == 260)
    }
}
