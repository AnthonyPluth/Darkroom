import SwiftUI

struct Card<Content: View>: View {
    private let border: Color
    private let content: Content

    init(border: Color = .line, @ViewBuilder content: () -> Content) {
        self.border = border
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.surface, in: RoundedRectangle(cornerRadius: Layout.cornerRadius, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Layout.cornerRadius, style: .continuous).stroke(border, lineWidth: 1))
    }
}
