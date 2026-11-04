import Foundation

public enum ResourceState: String, Codable, Sendable, CaseIterable {
    case hashed
    case checked
    case uploading
    case uploaded
    case failed
    case notLocal
}

public struct LocalResource: Equatable, Sendable {
    public let key: ResourceKey
    public let sha1: String
    public let bytes: Int64
    public let filename: String
    public var state: ResourceState

    public init(key: ResourceKey, sha1: String, bytes: Int64, filename: String, state: ResourceState) {
        self.key = key
        self.sha1 = sha1
        self.bytes = bytes
        self.filename = filename
        self.state = state
    }

    var descriptor: ResourceDescriptor {
        ResourceDescriptor(kind: key.kind, sha1: sha1, bytes: bytes, filename: filename)
    }
}

public struct UploadPlan: Equatable, Sendable {
    public let queue: [ResourceKey]
    public let markUploaded: [ResourceKey]

    public init(queue: [ResourceKey], markUploaded: [ResourceKey]) {
        self.queue = queue
        self.markUploaded = markUploaded
    }
}

public enum UploadPlanner {
    public static let defaultBatchSize = 200

    public static func checkBatches(
        from resources: [LocalResource],
        batchSize: Int = UploadPlanner.defaultBatchSize
    ) -> [CheckRequest] {
        precondition(batchSize > 0)
        var assetOrder: [String] = []
        var byAsset: [String: [LocalResource]] = [:]
        for resource in resources where resource.state == .hashed {
            if byAsset[resource.key.assetID] == nil { assetOrder.append(resource.key.assetID) }
            byAsset[resource.key.assetID, default: []].append(resource)
        }
        let assets = assetOrder.map { id in
            AssetDescriptor(
                id: id,
                resources: (byAsset[id] ?? []).sorted { $0.key.kind.order < $1.key.kind.order }.map(\.descriptor)
            )
        }
        return stride(from: 0, to: assets.count, by: batchSize).map {
            CheckRequest(assets: Array(assets[$0..<min($0 + batchSize, assets.count)]))
        }
    }

    public static func plan(resources: [LocalResource], reply: CheckReply) -> UploadPlan {
        let needed = Set(reply.needed.map(\.key))
        var assetOrder: [String: Int] = [:]
        for resource in resources where assetOrder[resource.key.assetID] == nil {
            assetOrder[resource.key.assetID] = assetOrder.count
        }
        let hashed = resources.filter { $0.state == .hashed }
        let queue = hashed
            .filter { needed.contains($0.key) }
            .sorted {
                let left = (assetOrder[$0.key.assetID] ?? 0, $0.key.kind.order)
                let right = (assetOrder[$1.key.assetID] ?? 0, $1.key.kind.order)
                return left < right
            }
            .map(\.key)
        let markUploaded = hashed.filter { !needed.contains($0.key) }.map(\.key)
        return UploadPlan(queue: queue, markUploaded: markUploaded)
    }
}
