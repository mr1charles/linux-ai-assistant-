import SwiftUI

/// Little Toby's face, drawn the same way as on the computer (the Face widget
/// in scripts/linux_agent_apple.py and draw_chibi in scripts/chibi.py): the
/// warm gradient head, blush, the sprout with its rainbow tip, and eyes and
/// mouth that follow what Toby is doing.
///
///   happy        connected and idle: a soft smile, breathing, blinking
///   thinking     working out what to do: looks up, three motes drift round
///   focused      doing the steps: eyes on the task, a small concentrated mouth
///   waiting      needs your OK: looks down toward the prompt
///   celebrating  a task finished: happy closed eyes, a small hop
///   concerned    something went wrong: a small frown, nothing dramatic
///   sleepy       the computer is asleep or unreachable: eyes closed, a z
///
/// It redraws at up to 30 frames a second only while on screen, and holds
/// still when Reduce Motion is on.
struct TobyFace: View {
    var mood: TobyMood
    var size: CGFloat = 150
    var listening = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            Canvas { context, canvasSize in
                let t = reduceMotion ? 0 : timeline.date.timeIntervalSinceReferenceDate
                FacePainter(mood: mood, time: t, listening: listening, still: reduceMotion)
                    .paint(in: &context, size: canvasSize)
            }
        }
        .frame(width: size, height: size * 1.15)
        .accessibilityElement()
        .accessibilityLabel(Text(accessibilityText))
    }

    private var accessibilityText: String {
        switch mood {
        case .sleepy: return "Toby, asleep"
        case .idle, .happy: return "Toby"
        case .thinking: return "Toby, thinking"
        case .focused: return "Toby, working"
        case .waiting: return "Toby, waiting for your OK"
        case .celebrating: return "Toby, done"
        case .concerned: return "Toby, something went wrong"
        }
    }
}

private struct FacePainter {
    var mood: TobyMood
    var time: Double
    var listening: Bool
    var still: Bool

    func paint(in context: inout GraphicsContext, size: CGSize) {
        let r = min(size.width, size.height / 1.15) * 0.40
        var cx = size.width / 2
        var cy = size.height * 0.58

        // breathing, and a small hop when celebrating
        let breathe = still ? 1.0 : 1.0 + 0.015 * sin(time * 2 * .pi * 0.35)
        if mood == .celebrating && !still {
            cy -= abs(sin(time * 2 * .pi * 1.6)) * r * 0.14
        }
        if mood == .focused && !still {
            cx += sin(time * 2 * .pi * 0.25) * r * 0.02
        }
        let head = r * breathe

        if listening {
            let pulse = still ? 0.5 : 0.5 + 0.5 * sin(time * 2 * .pi * 1.2)
            let ring = head * (1.18 + 0.05 * pulse)
            context.stroke(Path(ellipseIn: CGRect(x: cx - ring, y: cy - ring, width: ring * 2, height: ring * 2)),
                           with: .color(Theme.accent.opacity(0.35 + 0.35 * pulse)), lineWidth: 3)
        }

        // soft shadow under the head
        context.fill(Path(ellipseIn: CGRect(x: cx - head * 0.8, y: cy + head * 1.05, width: head * 1.6, height: head * 0.22)),
                     with: .color(.black.opacity(0.12)))

        // the sprout with its rainbow tip — the wake ring, worn as a hat
        var sprout = Path()
        sprout.move(to: CGPoint(x: cx, y: cy - head * 0.95))
        sprout.addCurve(to: CGPoint(x: cx + head * 0.25, y: cy - head * 1.32),
                        control1: CGPoint(x: cx + head * 0.05, y: cy - head * 1.2),
                        control2: CGPoint(x: cx + head * 0.22, y: cy - head * 1.25))
        context.stroke(sprout, with: .color(Theme.skinDark), style: StrokeStyle(lineWidth: head * 0.07, lineCap: .round))
        let hue = (time * 0.25).truncatingRemainder(dividingBy: 1)
        let tip = head * 0.1
        context.fill(Path(ellipseIn: CGRect(x: cx + head * 0.27 - tip, y: cy - head * 1.36 - tip, width: tip * 2, height: tip * 2)),
                     with: .color(Color(hue: hue, saturation: 0.65, brightness: 1)))

        // head
        let headRect = CGRect(x: cx - head, y: cy - head, width: head * 2, height: head * 2)
        context.fill(Path(ellipseIn: headRect),
                     with: .radialGradient(Gradient(colors: [Theme.skinLight, Theme.skinDark]),
                                           center: CGPoint(x: cx - head * 0.3, y: cy - head * 0.3),
                                           startRadius: head * 0.1, endRadius: head))

        // eyes
        let look = lookOffset()
        let eyeR = head * 0.14
        let eyeY = cy - head * 0.1 + look.y * head * 0.12
        let blinkPhase = time.truncatingRemainder(dividingBy: 3.7)
        var open: Double = (blinkPhase < 0.12 && !still) ? 0.1 : 1.0
        if mood == .sleepy { open = 0 }
        if mood == .focused { open *= 0.85 }
        for side in [-1.0, 1.0] {
            let ex = cx + side * head * 0.36 + look.x * head * 0.14
            if mood == .celebrating {
                var arc = Path()
                arc.addArc(center: CGPoint(x: ex, y: eyeY + eyeR * 0.4), radius: eyeR * 0.9,
                           startAngle: .degrees(207), endAngle: .degrees(333), clockwise: false)
                context.stroke(arc, with: .color(Theme.faceInk), style: StrokeStyle(lineWidth: head * 0.07, lineCap: .round))
            } else if open <= 0.01 {
                var lid = Path()
                lid.move(to: CGPoint(x: ex - eyeR, y: eyeY))
                lid.addQuadCurve(to: CGPoint(x: ex + eyeR, y: eyeY), control: CGPoint(x: ex, y: eyeY + eyeR * 0.6))
                context.stroke(lid, with: .color(Theme.faceInk), style: StrokeStyle(lineWidth: head * 0.06, lineCap: .round))
            } else {
                let h = eyeR * 2 * open
                context.fill(Path(ellipseIn: CGRect(x: ex - eyeR, y: eyeY - h / 2, width: eyeR * 2, height: h)),
                             with: .color(Theme.faceInk))
                if open > 0.5 {
                    let c = eyeR * 0.32
                    context.fill(Path(ellipseIn: CGRect(x: ex + eyeR * 0.35 - c, y: eyeY - eyeR * 0.35 - c, width: c * 2, height: c * 2)),
                                 with: .color(.white.opacity(0.85)))
                }
            }
            // blush
            let b = head * 0.13
            context.fill(Path(ellipseIn: CGRect(x: cx + side * head * 0.56 - b, y: cy + head * 0.2 - b, width: b * 2, height: b * 2)),
                         with: .color(Theme.blush.opacity(0.35)))
        }

        // mouth
        let mw = head * 0.5
        let my = cy + head * 0.36
        let curve = mouthCurve() * head * 0.28
        var mouth = Path()
        mouth.move(to: CGPoint(x: cx - mw / 2, y: my))
        mouth.addCurve(to: CGPoint(x: cx + mw / 2, y: my),
                       control1: CGPoint(x: cx - mw / 4, y: my + curve),
                       control2: CGPoint(x: cx + mw / 4, y: my + curve))
        context.stroke(mouth, with: .color(Theme.faceInk), style: StrokeStyle(lineWidth: head * 0.075, lineCap: .round))

        // thinking: three motes drifting round the head, in place of a spinner
        if mood == .thinking {
            for i in 0..<3 {
                let angle = time * 0.9 + Double(i) * 2 * .pi / 3
                let orbit = head * 1.3
                let mx = cx + cos(angle) * orbit
                let myy = cy - head * 0.2 + sin(angle) * orbit * 0.45
                let m = head * (0.06 + 0.02 * sin(time * 2 + Double(i)))
                context.fill(Path(ellipseIn: CGRect(x: mx - m, y: myy - m, width: m * 2, height: m * 2)),
                             with: .color(Theme.accent.opacity(0.55 + 0.25 * sin(time * 1.5 + Double(i)))))
            }
        }

        // asleep: a small z drifting up
        if mood == .sleepy {
            let rise = still ? 0.5 : (time * 0.4).truncatingRemainder(dividingBy: 1)
            let z = Text("z").font(.system(size: head * 0.32, weight: .bold, design: .rounded))
                .foregroundColor(Theme.accent.opacity(1 - rise))
            context.draw(z, at: CGPoint(x: cx + head * 0.95 + rise * head * 0.2, y: cy - head * (0.8 + rise * 0.5)))
        }
    }

    private func lookOffset() -> CGPoint {
        switch mood {
        case .thinking: return CGPoint(x: 0.5, y: -0.6)
        case .waiting: return CGPoint(x: 0, y: 0.8)          // toward the prompt below
        case .focused: return CGPoint(x: still ? 0 : 0.3 * sin(time * 0.8), y: 0.35)
        default:
            if still { return .zero }
            return CGPoint(x: 0.25 * sin(time * 0.37), y: 0.12 * sin(time * 0.23))
        }
    }

    private func mouthCurve() -> Double {
        switch mood {
        case .happy: return 0.35
        case .idle: return 0.25
        case .celebrating: return 0.95
        case .focused: return 0.08
        case .thinking: return -0.03
        case .waiting: return 0.15
        case .concerned: return -0.28
        case .sleepy: return 0.05
        }
    }
}
