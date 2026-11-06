import DarkroomCore
import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: AppModel
    let server: ServerAddress
    @State private var confirming = false
    private let version = BuildVersion.current

    var body: some View {
        Screen {
            Text("Settings")
                .font(.title.bold())
                .foregroundStyle(Color.ink)
                .accessibilityAddTraits(.isHeader)
            account
            serverSection
            signOut
            Text("Build \(version.label)")
                .font(.footnote.monospaced())
                .foregroundStyle(Color.inkMuted)
                .frame(maxWidth: .infinity)
                .accessibilityLabel("Build \(version.marketing), \(version.build)")
                .accessibilityIdentifier("build-version")
        }
        .task { await model.loadIdentity() }
        .onChange(of: model.signOutProblem) { _, problem in
            if let problem { AccessibilityNotification.Announcement(problem).post() }
        }
    }

    @ViewBuilder
    private var account: some View {
        let person = model.identity?.person
        let device = model.identity?.device ?? model.deviceLabel
        if person != nil || device != nil {
            VStack(alignment: .leading, spacing: 8) {
                SectionLabel("Account")
                Card {
                    if let person {
                        KeyValueRow(label: "Signed in as", value: person)
                    }
                    if person != nil, device != nil {
                        Divider().overlay(Color.raised)
                    }
                    if let device {
                        KeyValueRow(label: "This iPhone", value: device)
                    }
                }
            }
        }
    }

    private var serverSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel("Server")
            Card {
                KeyValueRow(label: "Address", value: server.authority, monospaced: true)
                Text(SettingsMessages.differentServer).font(.footnote).foregroundStyle(Color.inkMuted)
            }
        }
    }

    private var signOut: some View {
        VStack(alignment: .leading, spacing: 8) {
            SecondaryButton(action: { confirming = true }) {
                Text("Sign out").foregroundStyle(Color.danger)
            }
            .disabled(model.signingOut)
            .accessibilityHint("Asks to confirm")
            .confirmationDialog(SettingsMessages.signOutTitle, isPresented: $confirming, titleVisibility: .visible) {
                Button("Sign out", role: .destructive) { Task { await model.signOut() } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(SettingsMessages.signOutDetail)
            }
            if let problem = model.signOutProblem {
                Text(problem).font(.footnote).foregroundStyle(Color.danger)
            }
        }
    }
}
