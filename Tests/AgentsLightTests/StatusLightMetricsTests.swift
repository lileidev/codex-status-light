import Foundation
import Testing
@testable import AgentsLight

/// A row is top-aligned, so a short glyph beside a tall light drifts upward unless
/// it claims the same height. The agent glyph therefore occupies the light's whole
/// footprint, which centres the two on one horizontal line.
@Suite struct StatusLightMetricsTests {

    @Test func footprintIsTheLightPlusItsPadding() {
        #expect(StatusLightView.lightSize == 24)
        #expect(StatusLightView.lightPadding == 6)
        #expect(StatusLightView.footprint == StatusLightView.lightSize + StatusLightView.lightPadding * 2)
        #expect(StatusLightView.footprint == 36)
    }

    @Test func glyphIsTallerThanTheBareMark() {
        // The brand mark is drawn at 14 pt. If the glyph view were only that tall it
        // would sit above the light's centre; the footprint is what fixes the offset.
        #expect(StatusLightView.footprint > 14)
    }
}
