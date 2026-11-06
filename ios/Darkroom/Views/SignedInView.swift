import DarkroomCore
import SwiftUI

/// The signed-in app shell: the two tabs of the design. Their contents come in the next pull requests.
struct SignedInView: View {
    let server: ServerAddress

    var body: some View {
        TabView {
            Screen {
                Text("Uploads")
                    .font(.title.bold())
                    .foregroundStyle(Color.ink)
                    .accessibilityAddTraits(.isHeader)
            }
            .tabItem { Label("Uploads", systemImage: "arrow.up.circle") }
            SettingsPlaceholder(server: server)
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .tint(Color.accent)
    }
}

private struct SettingsPlaceholder: View {
    let server: ServerAddress
    private let version = BuildVersion.current

    private var authority: String {
        let host = server.url.host ?? server.text
        return server.url.port.map { "\(host):\($0)" } ?? host
    }

    var body: some View {
        Screen {
            Text("Settings")
                .font(.title.bold())
                .foregroundStyle(Color.ink)
                .accessibilityAddTraits(.isHeader)
            VStack(alignment: .leading, spacing: 8) {
                SectionLabel("Server")
                Card {
                    KeyValueRow(label: "Address", value: authority, monospaced: true)
                }
            }
            Text("Build \(version.label)")
                .font(.footnote.monospaced())
                .foregroundStyle(Color.inkMuted)
                .frame(maxWidth: .infinity)
                .accessibilityLabel("Build \(version.marketing), \(version.build)")
                .accessibilityIdentifier("build-version")
        }
    }
}
