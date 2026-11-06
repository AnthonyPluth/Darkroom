import DarkroomCore
import SwiftUI

struct UploadsView: View {
    @ObservedObject var model: UploadsModel
    let server: ServerAddress
    let person: String?
    let problem: String?
    let signInAgain: () -> Void
    let prepareStorage: () -> Void
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        NavigationStack {
            Screen {
                if let status = model.status {
                    content(status)
                } else {
                    ProgressView().tint(Color.inkMuted).frame(maxWidth: .infinity)
                }
            }
            .refreshable { await model.run(.syncNow) }
            .toolbar(.hidden, for: .navigationBar)
        }
        .task {
            await model.load()
            await model.run(.refresh)
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.run(.refresh) } }
        }
    }

    @ViewBuilder
    private func content(_ status: UploadStatus) -> some View {
        let card = UploadsPresentation.card(for: status)
        VStack(alignment: .leading, spacing: 8) {
            Text("Uploads")
                .font(.title.bold())
                .foregroundStyle(Color.ink)
                .accessibilityAddTraits(.isHeader)
            Text(card.sentence).font(.subheadline).foregroundStyle(Color.inkMuted)
            if let person {
                Text("Uploading as \(person)").font(.subheadline).foregroundStyle(Color.inkMuted)
            }
        }
        stateCard(card)
        if let problem {
            Text(problem).font(.footnote).foregroundStyle(Color.danger)
        }
        syncButton(secondary: card.action != .none)
        files(status)
        details(status)
        if !status.failures.isEmpty {
            attention(status.failures)
        }
    }

    private func stateCard(_ card: UploadsCard) -> some View {
        Card {
            StatusChip(tone: tone(card.tone), label: card.chip)
            Text(card.title).font(.title3.bold()).foregroundStyle(Color.ink)
            Text(card.detail).font(.subheadline).foregroundStyle(Color.inkMuted)
            if card.showsProgress {
                ProgressView().tint(Color.accent).accessibilityLabel("Syncing")
            }
            if let title = actionTitle(card.action) {
                PrimaryButton(action: { perform(card.action) }) { Text(title) }
                    .disabled(model.syncing && card.action != .signInAgain && card.action != .openSettings)
            }
        }
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private func syncButton(secondary: Bool) -> some View {
        let label = HStack(spacing: 8) {
            if model.syncing { ProgressView().tint(secondary ? Color.ink : Color.accentInk) }
            Text(model.syncing ? "Syncing" : "Sync now")
        }
        if secondary {
            SecondaryButton(action: { Task { await model.run(.syncNow) } }) { label }.disabled(model.syncing)
        } else {
            PrimaryButton(action: { Task { await model.run(.syncNow) } }) { label }.disabled(model.syncing)
        }
    }

    private func files(_ status: UploadStatus) -> some View {
        let columns = typeSize.isAccessibilitySize
            ? [GridItem(.flexible())]
            : [GridItem(.flexible(), spacing: 16), GridItem(.flexible(), spacing: 16)]
        return VStack(alignment: .leading, spacing: 8) {
            SectionLabel("Files")
            LazyVGrid(columns: columns, spacing: 16) {
                ForEach(UploadsPresentation.tiles(for: status), id: \.id) { tile in
                    Card {
                        Text(tile.count.formatted()).font(.title.bold()).monospacedDigit().foregroundStyle(Color.ink)
                        Text(tile.label).font(.subheadline).foregroundStyle(Color.inkMuted)
                        if let note = tile.note {
                            Text(note).font(.footnote).foregroundStyle(Color.inkMuted)
                        }
                    }
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("\(tile.label), \(UploadsPresentation.files(tile.count))")
                }
            }
            Text("Counts are files. A Live Photo is two files and an edited photo is three.")
                .font(.footnote)
                .foregroundStyle(Color.inkMuted)
        }
    }

    private func details(_ status: UploadStatus) -> some View {
        Card {
            TimelineView(.periodic(from: .now, by: 60)) { context in
                KeyValueRow(label: "Last synced", value: UploadsPresentation.lastSynced(status.lastSync, now: context.date))
            }
            Divider().overlay(Color.raised)
            KeyValueRow(label: "Server", value: server.authority, monospaced: true)
        }
    }

    private func attention(_ failures: [FailedResource]) -> some View {
        let shown = UploadsPresentation.rows(failures, limit: 5)
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                SectionLabel("Needs attention, \(failures.count)")
                Spacer()
                if failures.contains(where: { $0.canRetry }) {
                    Button("Try all again") { Task { await model.run(.syncNow) } }
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Color.accent)
                        .disabled(model.syncing)
                }
            }
            Card {
                ForEach(Array(shown.enumerated()), id: \.element.key) { index, failure in
                    if index > 0 { Divider().overlay(Color.raised) }
                    FailureRow(failure: failure, busy: model.syncing) { Task { await model.run(.retry([failure.key])) } }
                }
                if failures.count > shown.count {
                    Divider().overlay(Color.raised)
                    NavigationLink {
                        FailureList(model: model, failures: failures)
                    } label: {
                        Text("Show all \(failures.count)").font(.subheadline.weight(.semibold)).foregroundStyle(Color.accent)
                    }
                }
            }
        }
    }

    private func tone(_ tone: UploadsCard.Tone) -> StatusChip.Tone {
        switch tone {
        case .ok: .ok
        case .warn: .warn
        case .danger: .danger
        case .neutral: .neutral
        }
    }

    private func actionTitle(_ action: UploadsCard.Action) -> String? {
        switch action {
        case .none: nil
        case .signInAgain: "Sign in again"
        case .tryAgain: "Try again"
        case .tryAllAgain: "Try all again"
        case .openSettings: "Open Settings"
        }
    }

    private func perform(_ action: UploadsCard.Action) {
        switch action {
        case .none: break
        case .signInAgain: signInAgain()
        case .tryAgain:
            Task {
                if model.status?.counts.isEmpty == true { prepareStorage() }
                await model.run(.syncNow)
            }
        case .tryAllAgain: Task { await model.run(.syncNow) }
        case .openSettings:
            if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
        }
    }
}

private struct FailureRow: View {
    let failure: FailedResource
    let busy: Bool
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 4) {
                Text(failure.filename.isEmpty ? "Unnamed file" : failure.filename)
                    .font(.footnote.monospaced())
                    .foregroundStyle(Color.ink)
                Text(failure.reason).font(.footnote).foregroundStyle(Color.inkMuted)
                if failure.recoverable {
                    Text("Will try again").font(.caption).foregroundStyle(Color.inkMuted)
                }
            }
            .accessibilityElement(children: .combine)
            if failure.canRetry {
                SecondaryButton(action: retry) { Text("Try again") }.disabled(busy)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct FailureList: View {
    @ObservedObject var model: UploadsModel
    let failures: [FailedResource]

    var body: some View {
        Screen {
            Card {
                ForEach(Array(failures.enumerated()), id: \.element.key) { index, failure in
                    if index > 0 { Divider().overlay(Color.raised) }
                    FailureRow(failure: failure, busy: model.syncing) { Task { await model.run(.retry([failure.key])) } }
                }
            }
        }
        .navigationTitle("Needs attention")
        .navigationBarTitleDisplayMode(.inline)
    }
}
