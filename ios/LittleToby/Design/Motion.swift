import SwiftUI

/// Toby's motion language, the same curves and durations as everywhere else
/// (scripts/toby_anim.py CURVES and DURATIONS; tests/test_motion_tokens.py
/// checks this file against them). Things arrive quickly and settle softly,
/// leave faster than they came, and never overshoot.
enum Motion {
    // cubic-bezier control points
    static let curveEnter: (Double, Double, Double, Double) = (0.16, 1.0, 0.3, 1.0)
    static let curveExit: (Double, Double, Double, Double) = (0.55, 0.0, 0.8, 0.2)
    static let curveMove: (Double, Double, Double, Double) = (0.33, 1.0, 0.68, 1.0)
    static let curveStandard: (Double, Double, Double, Double) = (0.2, 0.0, 0.0, 1.0)
    static let curveCarry: (Double, Double, Double, Double) = (0.45, 0.0, 0.55, 1.0)

    // seconds
    static let durationInstant: Double = 0.09
    static let durationQuick: Double = 0.16
    static let durationStandard: Double = 0.24
    static let durationEmphasized: Double = 0.34
    static let durationGentle: Double = 0.52

    static func curve(_ c: (Double, Double, Double, Double), _ duration: Double) -> Animation {
        .timingCurve(c.0, c.1, c.2, c.3, duration: duration)
    }

    /// Something arriving: a card, a sheet's contents, a new step.
    static let enter = curve(curveEnter, durationEmphasized)
    /// Something leaving.
    static let exit = curve(curveExit, durationStandard)
    /// Travelling between two resting places.
    static let move = curve(curveMove, durationEmphasized)
    /// Small state changes: press, colour, a value updating.
    static let standard = curve(curveStandard, durationQuick)
    /// Large surfaces, and Toby's own gestures.
    static let gentle = curve(curveEnter, durationGentle)
}

extension AnyTransition {
    /// Fade in and settle up a few points; the app's standard arrival.
    static var arrive: AnyTransition {
        .asymmetric(insertion: .opacity.combined(with: .offset(y: 8)).animation(Motion.enter),
                    removal: .opacity.animation(Motion.exit))
    }
}
