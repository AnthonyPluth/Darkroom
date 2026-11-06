import SwiftUI

struct StatusChip: View {
    enum Tone {
        case ok
        case warn
        case danger
        case neutral
    }

    let tone: Tone
    let label: String

    var body: some View {
        HStack(spacing: 6) {
            shape
            Text(label)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(foreground)
        .padding(.horizontal, 10)
        .padding(.vertical, 2)
        .background(background, in: Capsule())
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var shape: some View {
        switch tone {
        case .ok, .neutral:
            Circle().fill(foreground).frame(width: 8, height: 8)
        case .warn:
            Rectangle().fill(foreground).frame(width: 8, height: 8).rotationEffect(.degrees(45))
        case .danger:
            RoundedRectangle(cornerRadius: 2, style: .continuous).fill(foreground).frame(width: 8, height: 8)
        }
    }

    private var foreground: Color {
        switch tone {
        case .ok: .ok
        case .warn: .warn
        case .danger: .danger
        case .neutral: .inkMuted
        }
    }

    private var background: Color {
        switch tone {
        case .ok: .okBg
        case .warn: .warnBg
        case .danger: .dangerBg
        case .neutral: .raised
        }
    }
}
