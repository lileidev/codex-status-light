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

Captured at 2x backing scale, downscaled to 1x here.

These are off-screen renders, not screen captures: this machine has no
on-console GUI session, so `screencapture` cannot grab the live panel. The same
views and the same panel-sizing code run in both revisions, so the comparison is
of real layout output. The resulting window heights were additionally confirmed
against the packaged app's actual window bounds (4 sessions 288 → 207 pt, one
session 120 → 64 pt, idle 120 → 60 pt).
