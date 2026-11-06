import SwiftUI

struct KeyValueRow: View {
    let label: String
    let value: String
    var monospaced = false
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        Group {
            if typeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: 4) { texts }
            } else {
                HStack(alignment: .firstTextBaseline) {
                    texts
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label), \(value)")
    }

    @ViewBuilder
    private var texts: some View {
        Text(label)
            .font(.subheadline)
            .foregroundStyle(Color.inkMuted)
        if !typeSize.isAccessibilitySize {
            Spacer(minLength: 12)
        }
        Text(value)
            .font(monospaced ? .footnote.monospaced() : .subheadline)
            .foregroundStyle(Color.ink)
            .multilineTextAlignment(typeSize.isAccessibilitySize ? .leading : .trailing)
    }
}
