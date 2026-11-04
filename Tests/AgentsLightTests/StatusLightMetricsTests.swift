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

    /// The dot-chase sits on light grey glass in light appearance. A low-alpha
    /// trail washes out there, so each dot carries a neutral dark rim; it must
    /// clear the 3:1 non-text contrast bar on white and on the panel glass, while
    /// the trail still fades behind the leading cell.
    @Test @MainActor func chaseTrailHasContrastOnLightPanels() {
        #expect(StatusChaseView.trailFloor < 1.0, "the chase must still fade behind the head")
        #expect(StatusChaseView.rimOpacity > 0)

        // Composite the rim over pure white and over the floating panel's glass,
        // measured at 0.78 luminance in light appearance.
        for backdrop in [1.0, 0.78] {
            let rim = backdrop * (1 - StatusChaseView.rimOpacity)
            #expect(contrastRatio(rim, backdrop) >= 3.0)
        }
    }
}

/// WCAG contrast ratio between two grey levels in sRGB (0...1).
private func contrastRatio(_ a: Double, _ b: Double) -> Double {
    let first = relativeLuminance(a)
    let second = relativeLuminance(b)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)
}

private func relativeLuminance(_ gray: Double) -> Double {
    gray <= 0.03928 ? gray / 12.92 : pow((gray + 0.055) / 1.055, 2.4)
}
