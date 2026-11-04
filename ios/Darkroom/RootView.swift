import SwiftUI

struct RootView: View {
    private let version = BuildVersion.current

    var body: some View {
        VStack(spacing: 8) {
            Text("Darkroom")
                .font(.title2.weight(.semibold))
            Text("Build \(version.label)")
                .font(.footnote.monospaced())
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("build-version")
        }
        .padding()
    }
}

#Preview {
    RootView()
}
