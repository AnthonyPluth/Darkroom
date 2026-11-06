import DarkroomCore
import SwiftUI

/// The signed-in app shell: the two tabs of the design.
struct SignedInView: View {
    @ObservedObject var model: AppModel
    let server: ServerAddress

    var body: some View {
        TabView {
            UploadsContainer(model: model, server: server)
                .id(model.uploadEngine.map { ObjectIdentifier($0) })
                .tabItem { Label("Uploads", systemImage: "arrow.up.circle") }
            SettingsView(model: model, server: server)
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .tint(Color.accent)
        .task { await model.loadIdentity() }
    }
}

private struct UploadsContainer: View {
    @ObservedObject var model: AppModel
    @StateObject private var uploads: UploadsModel

    init(model: AppModel, server: ServerAddress) {
        self.model = model
        self.server = server
        _uploads = StateObject(wrappedValue: UploadsModel(engine: model.uploadEngine))
    }

    private let server: ServerAddress

    var body: some View {
        UploadsView(
            model: uploads,
            server: server,
            person: model.identity?.person,
            signInAgain: { Task { await model.signInAgain() } },
            prepareStorage: { Task { await model.prepareUploads() } }
        )
    }
}
