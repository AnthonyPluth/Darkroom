import Foundation
import XCTest
@testable import DarkroomCore

final class SHA1HasherTests: XCTestCase {
    private func temporaryFile(_ contents: Data) throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try contents.write(to: url)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testEmptyFileVector() throws {
        let url = try temporaryFile(Data())
        XCTAssertEqual(try SHA1Hasher.hash(ofFileAt: url), "da39a3ee5e6b4b0d3255bfef95601890afd80709")
    }

    func testAbcVector() throws {
        let url = try temporaryFile(Data("abc".utf8))
        XCTAssertEqual(try SHA1Hasher.hash(ofFileAt: url), "a9993e364706816aba3e25717850c26c9cd0d89d")
    }

    func testQuickBrownFoxVector() throws {
        let url = try temporaryFile(Data("The quick brown fox jumps over the lazy dog".utf8))
        XCTAssertEqual(try SHA1Hasher.hash(ofFileAt: url), "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12")
    }

    func testMillionAVector() throws {
        let url = try temporaryFile(Data(repeating: 0x61, count: 1_000_000))
        XCTAssertEqual(try SHA1Hasher.hash(ofFileAt: url), "34aa973cd4c4daa4f61eeb2bdbad27316534016f")
    }

    func testChunkSizeDoesNotChangeTheDigest() throws {
        let url = try temporaryFile(Data((0..<10_000).map { UInt8($0 % 251) }))
        let whole = try SHA1Hasher.hash(ofFileAt: url)
        XCTAssertEqual(try SHA1Hasher.hash(ofFileAt: url, chunkSize: 7), whole)
        XCTAssertEqual(try SHA1Hasher.hash(ofFileAt: url, chunkSize: 4096), whole)
    }

    func testMissingFileThrows() {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        XCTAssertThrowsError(try SHA1Hasher.hash(ofFileAt: url))
    }
}
