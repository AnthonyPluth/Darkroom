import Crypto
import Foundation

public enum SHA1Hasher {
    public static let chunkSize = 1 << 20

    /// With `checkingCancellation` the loop stops with `CancellationError` between chunks once the calling task is cancelled.
    public static func hash(
        of handle: FileHandle,
        chunkSize: Int = SHA1Hasher.chunkSize,
        checkingCancellation: Bool = false
    ) throws -> String {
        var hasher = Insecure.SHA1()
        while true {
            if checkingCancellation { try Task.checkCancellation() }
            let chunk = try handle.read(upToCount: chunkSize) ?? Data()
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    public static func hash(
        ofFileAt url: URL,
        chunkSize: Int = SHA1Hasher.chunkSize,
        checkingCancellation: Bool = false
    ) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        return try hash(of: handle, chunkSize: chunkSize, checkingCancellation: checkingCancellation)
    }
}
