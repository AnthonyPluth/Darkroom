import XCTest
@testable import DarkroomCore

final class ServerAddressTests: XCTestCase {
    func testAHostAloneBecomesAnHTTPSAddress() throws {
        XCTAssertEqual(try ServerAddress(parsing: "darkroom.example.net").text, "https://darkroom.example.net")
    }

    func testAPortAndAPathPrefixAreKept() throws {
        XCTAssertEqual(try ServerAddress(parsing: "https://darkroom.example.net:8443/photos").text, "https://darkroom.example.net:8443/photos")
        XCTAssertEqual(try ServerAddress(parsing: "darkroom.example.net:8443").text, "https://darkroom.example.net:8443")
    }

    func testSurroundingSpaceAndTrailingSlashesAreDropped() throws {
        XCTAssertEqual(try ServerAddress(parsing: "  https://darkroom.example.net/photos//\n").text, "https://darkroom.example.net/photos")
        XCTAssertEqual(try ServerAddress(parsing: "https://darkroom.example.net/").text, "https://darkroom.example.net")
    }

    func testTheSchemeIsMatchedWithoutRegardToCase() throws {
        XCTAssertEqual(try ServerAddress(parsing: "HTTPS://darkroom.example.net").url.scheme, "https")
    }

    func testBlankTextIsRefused() {
        assertProblem(.empty, for: "")
        assertProblem(.empty, for: "   \n")
    }

    func testPlainHTTPAndOtherSchemesAreRefused() {
        assertProblem(.notHTTPS, for: "http://darkroom.example.net")
        assertProblem(.notHTTPS, for: "ftp://darkroom.example.net")
    }

    func testAddressesThatCannotBeServersAreRefused() {
        assertProblem(.invalid, for: "https://")
        assertProblem(.invalid, for: "https:///photos")
        assertProblem(.invalid, for: "dark room")
        assertProblem(.invalid, for: "https://user:secret@darkroom.example.net")
        assertProblem(.invalid, for: "https://darkroom.example.net?x=1")
        assertProblem(.invalid, for: "https://darkroom.example.net#top")
    }

    private func assertProblem(_ expected: ServerAddress.Problem, for text: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertThrowsError(try ServerAddress(parsing: text), file: file, line: line) {
            XCTAssertEqual($0 as? ServerAddress.Problem, expected, file: file, line: line)
        }
    }
}
