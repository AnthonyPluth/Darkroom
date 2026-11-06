import XCTest
@testable import DarkroomCore

final class DeviceSignInTests: XCTestCase {
    private func server(_ text: String = "https://darkroom.example.net") throws -> ServerAddress {
        try ServerAddress(parsing: text)
    }

    func testTheStartURLCarriesTheDeviceName() throws {
        let url = try XCTUnwrap(DeviceSignIn.startURL(server: server(), deviceName: "Sam's iPhone"))
        XCTAssertEqual(url.absoluteString, "https://darkroom.example.net/auth/device/start?name=Sam%27s%20iPhone")
    }

    func testTheStartURLKeepsAPathPrefix() throws {
        let url = try XCTUnwrap(DeviceSignIn.startURL(server: server("https://darkroom.example.net:8443/photos"), deviceName: "Phone"))
        XCTAssertEqual(url.absoluteString, "https://darkroom.example.net:8443/photos/auth/device/start?name=Phone")
    }

    func testCharactersThatSplitAQueryAreEncoded() throws {
        let url = try XCTUnwrap(DeviceSignIn.startURL(server: server(), deviceName: "A&B=C+D#E"))
        XCTAssertEqual(url.query, "name=A%26B%3DC%2BD%23E")
    }

    func testTheNameIsCleanedTheWayTheServerWantsIt() {
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName("  Sam\u{00A0}iPhone\n"), "Sam iPhone")
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName("Sam\tiPhone"), "SamiPhone")
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName("Sam iPhone"), "Sam iPhone")
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName(""), DeviceSignIn.fallbackDeviceName)
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName("\n\u{200B}"), DeviceSignIn.fallbackDeviceName)
    }

    func testALongNameIsCutToTheServersLimit() {
        let long = String(repeating: "a", count: 150)
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName(long).count, DeviceSignIn.maxDeviceNameLength)
        let emoji = String(repeating: "\u{1F4F1}", count: 150)
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName(emoji).unicodeScalars.count, DeviceSignIn.maxDeviceNameLength)
        let spaced = String(repeating: "a", count: 99) + "  b"
        XCTAssertEqual(DeviceSignIn.cleanedDeviceName(spaced), String(repeating: "a", count: 99))
    }

    func testTheTokenComesFromTheCallbackFragment() throws {
        let url = try XCTUnwrap(URL(string: "darkroom://signed-in#token=abc-DEF_123"))
        XCTAssertEqual(DeviceSignIn.token(fromCallback: url), "abc-DEF_123")
    }

    func testTheTokenIsFoundAmongOtherFragmentPairs() throws {
        let url = try XCTUnwrap(URL(string: "darkroom://signed-in#state=1&token=abc&extra=2"))
        XCTAssertEqual(DeviceSignIn.token(fromCallback: url), "abc")
    }

    func testSchemeAndHostAreMatchedWithoutRegardToCase() throws {
        let url = try XCTUnwrap(URL(string: "Darkroom://Signed-In#token=abc"))
        XCTAssertEqual(DeviceSignIn.token(fromCallback: url), "abc")
    }

    func testOtherCallbacksYieldNoToken() throws {
        let rejected = [
            "darkroom://signed-in",
            "darkroom://signed-in#",
            "darkroom://signed-in#token=",
            "darkroom://signed-in#token",
            "darkroom://signed-in#tokens=abc",
            "darkroom://signed-in?token=abc",
            "darkroom://signed-in/extra#token=abc",
            "darkroom://other#token=abc",
            "https://signed-in#token=abc",
            "otherapp://signed-in#token=abc",
            "darkroom://signed-in#token=a%20b",
            "darkroom://signed-in#token=a%00b",
            "darkroom://signed-in#token=" + String(repeating: "a", count: DeviceSignIn.maxTokenLength + 1),
        ]
        for text in rejected {
            let url = try XCTUnwrap(URL(string: text), text)
            XCTAssertNil(DeviceSignIn.token(fromCallback: url), text)
        }
    }

    func testATokenAtTheLengthLimitIsAccepted() throws {
        let token = String(repeating: "a", count: DeviceSignIn.maxTokenLength)
        let url = try XCTUnwrap(URL(string: "darkroom://signed-in#token=\(token)"))
        XCTAssertEqual(DeviceSignIn.token(fromCallback: url), token)
    }
}
