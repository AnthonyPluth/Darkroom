import XCTest
@testable import DarkroomCore

final class DeviceSignInTests: XCTestCase {
    private func server(_ text: String = "https://darkroom.example.net") throws -> ServerAddress {
        try ServerAddress(parsing: text)
    }

    func testTheStartURLCarriesTheDeviceName() throws {
        let url = try XCTUnwrap(DeviceSignIn.startURL(server: server(), deviceName: "Sam's iPhone", state: "st-1"))
        XCTAssertEqual(url.absoluteString, "https://darkroom.example.net/auth/device/start?name=Sam%27s%20iPhone&state=st-1")
    }

    func testCharactersThatSplitAQueryAreEncoded() throws {
        let url = try XCTUnwrap(DeviceSignIn.startURL(server: server(), deviceName: "A&B=C+D#E", state: "st-1"))
        XCTAssertEqual(url.query, "name=A%26B%3DC%2BD%23E&state=st-1")
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

    func testGeneratedStatesAreLongDistinctAndAcceptedByTheServersShape() {
        let states = (0..<20).map { _ in DeviceSignIn.newState() }
        XCTAssertEqual(Set(states).count, 20)
        for state in states {
            XCTAssertTrue((22...128).contains(state.count), state)
            XCTAssertNil(state.range(of: "[^A-Za-z0-9_-]", options: .regularExpression), state)
        }
    }

    func testTheCallbackCarriesTheTokenAndTheState() throws {
        let url = try XCTUnwrap(URL(string: "darkroom://signed-in#token=abc-DEF_123&state=st-1"))
        XCTAssertEqual(DeviceSignIn.callback(from: url), DeviceSignIn.Callback(token: "abc-DEF_123", state: "st-1"))
    }

    func testThePairsMayComeInAnyOrderAmongOthers() throws {
        let url = try XCTUnwrap(URL(string: "darkroom://signed-in#x=1&state=st-1&token=abc&extra=2"))
        XCTAssertEqual(DeviceSignIn.callback(from: url), DeviceSignIn.Callback(token: "abc", state: "st-1"))
    }

    func testOtherCallbacksYieldNothing() throws {
        let rejected = [
            "darkroom://signed-in",
            "darkroom://signed-in#",
            "darkroom://signed-in#token=abc",
            "darkroom://signed-in#state=st-1",
            "darkroom://signed-in#token=&state=st-1",
            "darkroom://signed-in#token=abc&state=",
            "darkroom://signed-in#token&state=st-1",
            "darkroom://signed-in#tokens=abc&state=st-1",
            "darkroom://signed-in?token=abc&state=st-1",
            "darkroom://signed-in/extra#token=abc&state=st-1",
            "darkroom://other#token=abc&state=st-1",
            "https://signed-in#token=abc&state=st-1",
            "otherapp://signed-in#token=abc&state=st-1",
            "darkroom://signed-in#token=a%20b&state=st-1",
            "darkroom://signed-in#token=a%00b&state=st-1",
            "darkroom://signed-in#token=abc&state=a%20b",
            "darkroom://signed-in#token=" + String(repeating: "a", count: DeviceSignIn.maxTokenLength + 1) + "&state=st-1",
        ]
        for text in rejected {
            let url = try XCTUnwrap(URL(string: text), text)
            XCTAssertNil(DeviceSignIn.callback(from: url), text)
        }
    }

    func testARefusalCarriesTheReasonAndTheState() throws {
        let noPerson = try XCTUnwrap(URL(string: "darkroom://signed-in#error=no_person&state=st-1"))
        XCTAssertEqual(DeviceSignIn.refusal(from: noPerson), DeviceSignIn.Refusal(reason: .noPerson, state: "st-1"))
        let notAllowed = try XCTUnwrap(URL(string: "darkroom://signed-in#state=st-1&error=not_allowed"))
        XCTAssertEqual(DeviceSignIn.refusal(from: notAllowed), DeviceSignIn.Refusal(reason: .notAllowed, state: "st-1"))
        XCTAssertNil(DeviceSignIn.callback(from: noPerson))
    }

    func testOtherRefusalsYieldNothing() throws {
        let rejected = [
            "darkroom://signed-in#error=no_person",
            "darkroom://signed-in#error=other&state=st-1",
            "darkroom://signed-in#error=&state=st-1",
            "darkroom://signed-in#error=no_person&state=",
            "darkroom://signed-in#error=no_person&state=a%20b",
            "darkroom://signed-in#error=no_person&token=abc&state=st-1",
            "darkroom://signed-in#token=abc&state=st-1",
            "darkroom://signed-in?error=no_person&state=st-1",
            "darkroom://other#error=no_person&state=st-1",
            "https://signed-in#error=no_person&state=st-1",
        ]
        for text in rejected {
            let url = try XCTUnwrap(URL(string: text), text)
            XCTAssertNil(DeviceSignIn.refusal(from: url), text)
        }
    }

    func testANameIsRequiredBeforeTheFallbackApplies() {
        XCTAssertTrue(DeviceSignIn.hasName("Sam's iPhone"))
        XCTAssertTrue(DeviceSignIn.hasName("iPhone"))
        XCTAssertFalse(DeviceSignIn.hasName(""))
        XCTAssertFalse(DeviceSignIn.hasName("   "))
        XCTAssertFalse(DeviceSignIn.hasName("\n\u{200B}"))
    }
}
