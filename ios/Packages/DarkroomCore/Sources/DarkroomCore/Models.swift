import Foundation

public enum ResourceKind: String, Codable, Sendable, CaseIterable {
    case photo
    case liveVideo = "live_video"
    case adjusted

    var order: Int {
        switch self {
        case .photo: return 0
        case .liveVideo: return 1
        case .adjusted: return 2
        }
    }
}

public struct ResourceKey: Hashable, Codable, Sendable {
    public let assetID: String
    public let kind: ResourceKind

    public init(assetID: String, kind: ResourceKind) {
        self.assetID = assetID
        self.kind = kind
    }
}

public struct ResourceDescriptor: Codable, Equatable, Sendable {
    public let kind: ResourceKind
    public let sha1: String
    public let bytes: Int64
    public let filename: String

    public init(kind: ResourceKind, sha1: String, bytes: Int64, filename: String) {
        self.kind = kind
        self.sha1 = sha1
        self.bytes = bytes
        self.filename = filename
    }
}

public struct AssetDescriptor: Codable, Equatable, Sendable {
    public let id: String
    public let resources: [ResourceDescriptor]

    public init(id: String, resources: [ResourceDescriptor]) {
        self.id = id
        self.resources = resources
    }
}

public struct CheckRequest: Codable, Equatable, Sendable {
    public let assets: [AssetDescriptor]

    public init(assets: [AssetDescriptor]) {
        self.assets = assets
    }
}

public struct NeededResource: Codable, Hashable, Sendable {
    public let id: String
    public let kind: ResourceKind

    public init(id: String, kind: ResourceKind) {
        self.id = id
        self.kind = kind
    }

    public var key: ResourceKey {
        ResourceKey(assetID: id, kind: kind)
    }
}

public struct CheckReply: Codable, Equatable, Sendable {
    public let needed: [NeededResource]

    public init(needed: [NeededResource]) {
        self.needed = needed
    }
}

public struct CheckinRequest: Codable, Equatable, Sendable {
    public let pendingUploads: Int
    public let appVersion: String

    public init(pendingUploads: Int, appVersion: String) {
        self.pendingUploads = pendingUploads
        self.appVersion = appVersion
    }

    enum CodingKeys: String, CodingKey {
        case pendingUploads = "pending_uploads"
        case appVersion = "app_version"
    }
}

public struct DeviceMeReply: Codable, Equatable, Sendable {
    public let person: String
    public let device: String

    public init(person: String, device: String) {
        self.person = person
        self.device = device
    }
}
