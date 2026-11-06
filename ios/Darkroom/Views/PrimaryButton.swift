import SwiftUI

struct PrimaryButton<Label: View>: View {
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
                .foregroundStyle(Color.accentInk)
                .frame(maxWidth: .infinity, minHeight: 48)
                .background(Color.accent, in: RoundedRectangle(cornerRadius: Layout.controlRadius, style: .continuous))
                .contentShape(RoundedRectangle(cornerRadius: Layout.controlRadius, style: .continuous))
        }
        .buttonStyle(.plain)
        .opacity(isEnabled ? 1 : 0.4)
    }
}
