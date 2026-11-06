import Foundation

public struct UploadsCard: Equatable, Sendable {
    public enum Tone: Equatable, Sendable {
        case ok
        case warn
        case danger
        case neutral
    }

    public enum Action: Equatable, Sendable {
        case none
        case signInAgain
        case tryAgain
        case tryAllAgain
        case openSettings
    }

    public let sentence: String
    public let tone: Tone
    public let chip: String
    public let title: String
    public let detail: String
    public let action: Action
    public let showsProgress: Bool

    public init(sentence: String, tone: Tone, chip: String, title: String, detail: String, action: Action = .none, showsProgress: Bool = false) {
        self.sentence = sentence
        self.tone = tone
        self.chip = chip
        self.title = title
        self.detail = detail
        self.action = action
        self.showsProgress = showsProgress
    }
}

public struct UploadsTile: Equatable, Sendable {
    public let id: String
    public let label: String
    public let count: Int
    public let note: String?
}

public enum UploadsPresentation {
    public static func waiting(_ status: UploadStatus) -> Int {
        status.count(.hashed) + status.count(.checked) + status.count(.uploading)
    }

    public static func files(_ count: Int) -> String {
        count == 1 ? "1 file" : "\(count) files"
    }

    public static func card(for status: UploadStatus) -> UploadsCard {
        let waiting = waiting(status)
        let attention = status.count(.failed)
        let cloud = status.count(.notLocal)
        switch status.phase {
        case .photosAccessNeeded:
            return UploadsCard(
                sentence: "Darkroom can't see your photos.", tone: .danger, chip: "Photos access needed",
                title: "Photos access needed", detail: "Allow access to all photos so Darkroom can upload them.", action: .openSettings
            )
        case .needsSignIn:
            return UploadsCard(
                sentence: "Sign in again to keep uploading.", tone: .danger, chip: "Sign-in needed", title: "Sign-in needed",
                detail: "Darkroom no longer accepts this iPhone. Sign in again to keep uploading. Files already on Darkroom stay there.",
                action: .signInAgain
            )
        case .error(let message):
            return UploadsCard(
                sentence: "Uploads stopped.", tone: .danger, chip: "Stopped", title: "Uploads stopped",
                detail: message + " Try again.", action: .tryAgain
            )
        case .uploading:
            return UploadsCard(
                sentence: "Uploading. \(files(waiting)) left.", tone: .neutral, chip: "Syncing", title: "Syncing",
                detail: "Uploads finish even if you leave the app.", showsProgress: true
            )
        case .waitingForHomeNetwork:
            return UploadsCard(
                sentence: "Waiting for your home network. \(files(waiting)) \(waiting == 1 ? "is" : "are") waiting.", tone: .warn,
                chip: "Waiting for home network", title: "Waiting for home network",
                detail: "Darkroom can't be reached right now. Uploads start again by themselves on home Wi-Fi or over Tailscale. Nothing is wrong."
            )
        case .idle:
            break
        }
        if !status.firstScanFinished {
            return UploadsCard(
                sentence: "Darkroom is reading this iPhone's photos.", tone: .neutral, chip: "Starting", title: "Getting ready",
                detail: "Darkroom is reading this iPhone's photos. This can take a while the first time."
            )
        }
        if waiting > 0 {
            return UploadsCard(
                sentence: "\(files(waiting)) \(waiting == 1 ? "is" : "are") waiting to upload.", tone: .neutral, chip: "Waiting",
                title: "Waiting to upload",
                detail: "Uploads start when iOS gives Darkroom background time. Tap Sync now to start them now."
            )
        }
        if attention > 0 {
            let retryable = status.failures.contains { $0.canRetry }
            return UploadsCard(
                sentence: "\(files(attention)) \(attention == 1 ? "needs" : "need") attention.", tone: .warn, chip: "Needs attention",
                title: "\(files(attention)) \(attention == 1 ? "needs" : "need") attention",
                detail: "Darkroom or this iPhone couldn't handle these files, including ones the app couldn't read. The list below says why.",
                action: retryable ? .tryAllAgain : .none
            )
        }
        if cloud > 0 {
            return UploadsCard(
                sentence: "Everything stored on this iPhone is on Darkroom.", tone: .ok, chip: "Up to date", title: "Up to date",
                detail: "\(files(cloud)) \(cloud == 1 ? "is" : "are") only in iCloud, so Darkroom can't upload \(cloud == 1 ? "it" : "them") from this iPhone."
            )
        }
        return UploadsCard(
            sentence: "All photos stored on this iPhone are on Darkroom.", tone: .ok, chip: "Up to date", title: "Up to date",
            detail: "New photos upload on their own when iOS gives Darkroom background time, and whenever you open the app."
        )
    }

    public static func tiles(for status: UploadStatus) -> [UploadsTile] {
        let sending = status.count(.uploading)
        return [
            UploadsTile(id: "uploaded", label: "On Darkroom", count: status.count(.uploaded), note: nil),
            UploadsTile(id: "waiting", label: "Waiting to upload", count: waiting(status), note: sending > 0 ? "\(sending) sending now" : nil),
            UploadsTile(id: "cloud", label: "Only in iCloud", count: status.count(.notLocal), note: nil),
            UploadsTile(id: "attention", label: "Needs attention", count: status.count(.failed), note: nil),
        ]
    }

    public static func lastSynced(_ date: Date?, now: Date, calendar: Calendar = .current) -> String {
        guard let date else { return "Not yet" }
        let seconds = now.timeIntervalSince(date)
        if seconds < 60 { return "Just now" }
        if seconds < 3600 {
            let minutes = Int(seconds / 60)
            return minutes == 1 ? "1 minute ago" : "\(minutes) minutes ago"
        }
        if seconds < 86400 {
            let hours = Int(seconds / 3600)
            return hours == 1 ? "1 hour ago" : "\(hours) hours ago"
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        if calendar.isDate(date, inSameDayAs: now.addingTimeInterval(-86400)) {
            formatter.dateFormat = "'Yesterday' HH:mm"
        } else {
            formatter.dateFormat = "d MMM HH:mm"
        }
        return formatter.string(from: date)
    }

    public static func rows(_ failures: [FailedResource], limit: Int?) -> [FailedResource] {
        guard let limit else { return failures }
        return Array(failures.prefix(limit))
    }
}

extension ServerAddress {
    public var authority: String {
        let host = url.host ?? text
        return url.port.map { "\(host):\($0)" } ?? host
    }
}
