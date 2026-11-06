import SwiftUI

struct SecondaryButton<Label: View>: View {
    private let action: () -> Void
    private let label: Label
    @Environment(\.isEnabled) private var isEnabled

    init(action: @escaping () -> Void, @ViewBuilder label: () -> Label) {
        self.action = action
        self.label = label()
    }

    var body: some View {
        Button(action: action) {
            label
                .font(.headline)
                .foregroundStyle(Color.ink)
                .frame(maxWidth: .infinity, minHeight: 48)
                .background(Color.surface, in: RoundedRectangle(cornerRadius: Layout.controlRadius, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: Layout.controlRadius, style: .continuous).stroke(Color.lineStrong, lineWidth: 1))
                .contentShape(RoundedRectangle(cornerRadius: Layout.controlRadius, style: .continuous))
        }
        .buttonStyle(.plain)
        .opacity(isEnabled ? 1 : 0.4)
    }
}
