# Panel UI evidence (issue #1)

Before/after renders of the floating panel, captured off-screen from the real
SwiftUI views (`StatusContentView` over a desktop-like gradient backdrop, dark
appearance, same sessions in both revisions).

| File | Revision |
| --- | --- |
| `00-before-after-comparison.png` | All cases side by side |
| `01-before-4-sessions.png` | `main` @ `20e5e93` |
| `02-after-4-sessions.png` | this branch |
| `03-before-long-title.png` | `main` @ `20e5e93` |
| `04-after-long-title.png` | this branch |
| `05-before-idle.png` | `main` @ `20e5e93` |
| `06-after-idle.png` | this branch |
| `07-light-chip-comparison.png` | Status-light chip removed, all cases |
| `08-light-chip-zoom.png` | Status-light chip removed, first row at 3x |
| `09-before-agent-icon-alignment.png` | `1b35bc5`, glyph above the light |
| `09-after-agent-icon-alignment.png` | this branch, glyph centred on the light |
| `10-streaming-light-before.png` | `main` @ `6473e96`, streaming light on the light panel |
| `11-streaming-light-after.png` | this branch, same streaming light |
| `12-streaming-light-comparison.png` | Before/after side by side, light appearance |
| `13-streaming-light-comparison-dark.png` | Before/after side by side, dark appearance |

Captured at 2x backing scale, downscaled to 1x here.

10–13 render the real `StatusContentView` in an off-screen window so the glass
material and appearance match the app; the streaming light is shown at 8x. The
light panel sits over a white window and the dark panel over a black one, since
the panel is translucent. Same sessions, viewport and streaming state in both
revisions.

These are off-screen renders, not screen captures: this machine has no
on-console GUI session, so `screencapture` cannot grab the live panel. The same
views and the same panel-sizing code run in both revisions, so the comparison is
of real layout output. The resulting window heights were additionally confirmed
against the packaged app's actual window bounds (4 sessions 288 → 207 pt, one
session 120 → 64 pt, idle 120 → 60 pt).
