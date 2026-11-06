import Foundation

public struct SyncState: Codable, Equatable, Sendable {
    public var firstScanFinished: Bool
    public var lastSync: Date?

    public init(firstScanFinished: Bool = false, lastSync: Date? = nil) {
        self.firstScanFinished = firstScanFinished
        self.lastSync = lastSync
    }
}

public protocol SyncStateStore: Sendable {
    func load() async -> SyncState
    func save(_ state: SyncState) async throws
}

public actor InMemorySyncStateStore: SyncStateStore {
    private var state = SyncState()

    public init() {}

    public func load() -> SyncState {
        state
    }

    public func save(_ state: SyncState) {
        self.state = state
    }
}

public actor FileSyncStateStore: SyncStateStore {
    private let url: URL

    public init(url: URL) {
        self.url = url
    }

    public func load() -> SyncState {
        guard let data = try? Data(contentsOf: url), let state = try? JSONDecoder().decode(SyncState.self, from: data) else {
            return SyncState()
        }
        return state
    }

    public func save(_ state: SyncState) throws {
        try JSONEncoder().encode(state).write(to: url, options: .atomic)
    }
}

public enum NewDeviceSession {
    public static func reset(store: ResourceStore, syncState: SyncStateStore) async throws {
        try await store.resetForNewDeviceSession()
        var state = await syncState.load()
        state.lastSync = nil
        try await syncState.save(state)
    }
}
