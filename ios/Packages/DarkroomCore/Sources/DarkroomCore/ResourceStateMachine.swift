import Foundation

extension ResourceState {
    public var next: Set<ResourceState> {
        switch self {
        case .hashed: return [.checked, .uploaded, .failed]
        case .checked: return [.uploading, .hashed, .failed]
        case .uploading: return [.uploaded, .checked, .hashed, .failed]
        case .uploaded: return []
        case .failed: return [.hashed]
        case .notLocal: return []
        }
    }

    public func canMove(to target: ResourceState) -> Bool {
        self == target || next.contains(target)
    }
}

public enum ResourceStoreError: Error, Equatable, Sendable {
    case illegalTransition(key: ResourceKey, from: ResourceState, to: ResourceState)
    case database(String)
}
