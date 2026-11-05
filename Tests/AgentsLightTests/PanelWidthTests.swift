import Foundation
import Testing
@testable import AgentsLight

/// The floating panel's border follows the content it hosts in both dimensions.
/// Idle content is short, so its measured width is clamped into a compact range
/// instead of staying at the fixed session width that left blank space beside it.
@Suite struct PanelWidthTests {

    @Test func contentWidthIsRoundedUpToAWholePoint() {
        #expect(AppDelegate.panelWidth(forContentWidth: 191.2) == 192)
        #expect(AppDelegate.panelWidth(forContentWidth: 192.6) == 193)
    }

    @Test func compactContentGetsCompactWidth() {
        // Measured idle content is ~192 pt; the panel should hug it rather than
        // keeping the 260 pt width used by session rows.
        let width = AppDelegate.panelWidth(forContentWidth: 192)
        #expect(width == 192)
        #expect(width < AppDelegate.panelWidth)
    }

    @Test func wideContentIsCappedAtTheSessionWidth() {
        #expect(AppDelegate.panelWidth(forContentWidth: 320) == AppDelegate.panelWidth)
    }

    @Test func narrowContentCannotCollapseTheBorder() {
        #expect(AppDelegate.panelWidth(forContentWidth: 40) == AppDelegate.minPanelWidth)
    }

    @Test func invalidMeasurementFallsBackToTheMaximumWidth() {
        // A hosting view that has not laid out yet can report zero/NaN. Falling
        // back to the maximum avoids flashing a collapsed or over-wide panel.
        #expect(AppDelegate.panelWidth(forContentWidth: 0) == AppDelegate.panelWidth)
        #expect(AppDelegate.panelWidth(forContentWidth: -10) == AppDelegate.panelWidth)
        #expect(AppDelegate.panelWidth(forContentWidth: .nan) == AppDelegate.panelWidth)
    }
}
