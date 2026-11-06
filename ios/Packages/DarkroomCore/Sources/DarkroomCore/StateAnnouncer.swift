import Foundation

public struct StateAnnouncer: Sendable {
    private var last: String?

    public init() {}

    public mutating func announcement(for card: UploadsCard) -> String? {
        let previous = last
        last = card.chip
        guard let previous, previous != card.chip else { return nil }
        return card.title
    }
}
