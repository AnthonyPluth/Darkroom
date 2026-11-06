import SwiftUI

struct Screen<Content: View>: View {
    private let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                content
            }
            .padding(.horizontal, Layout.gutter)
            .padding(.top, 20)
            .padding(.bottom, 28)
            .frame(maxWidth: Layout.columnWidth, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Color.canvas.ignoresSafeArea())
    }
}

struct SectionLabel: View {
    private let text: String

    init(_ text: String) {
        self.text = text
    }

    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .tracking(0.72)
            .textCase(.uppercase)
            .foregroundStyle(Color.inkMuted)
    }
}
