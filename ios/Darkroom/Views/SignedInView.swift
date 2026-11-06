import DarkroomCore
import SwiftUI

/// The signed-in app shell: the two tabs of the design.
struct SignedInView: View {
    @ObservedObject var model: AppModel
    let server: ServerAddress

    var body: some View {
        TabView {
            UploadsContainer(model: model, server: server)
                .id(model.uploadStorage == nil)
                .tabItem { Label("Uploads", systemImage: "arrow.up.circle") }
            SettingsPlaceholder(server: server)
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .tint(Color.accent)
    }
}

private struct UploadsContainer: View {
    @ObservedObject var model: AppModel
    @StateObject private var uploads: UploadsModel

    init(model: AppModel, server: ServerAddress) {
        self.model = model
        self.server = server
        _uploads = StateObject(wrappedValue: UploadsModel(server: server, session: model.deviceSession, storage: model.uploadStorage))
    }

    private let server: ServerAddress

    var body: some View {
        UploadsView(
            model: uploads,
            server: server,
            signInAgain: { Task { await model.signInAgain() } },
            prepareStorage: { Task { await model.prepareUploads() } }
        )
    }
}

private struct SettingsPlaceholder: View {
    let server: ServerAddress
    private let version = BuildVersion.current

    var body: some View {
        Screen {
            Text("Settings")
                .font(.title.bold())
                .foregroundStyle(Color.ink)
                .accessibilityAddTraits(.isHeader)
            VStack(alignment: .leading, spacing: 8) {
                SectionLabel("Server")
                Card {
                    KeyValueRow(label: "Address", value: server.authority, monospaced: true)
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
