import SwiftUI

struct RootView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Group {
            if !model.restored {
                ProgressView().tint(Color.inkMuted)
            } else if case .signedIn(let server) = model.phase {
                SignedInView(model: model, server: server)
            } else {
                SignInView(model: model)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.canvas.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .task { await model.restore(foreground: true) }
        .pushLifecycle(phase: model.restored ? model.phase : nil)
    }
}
