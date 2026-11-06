import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import DarkroomCore

private final class PushStub: URLProtocol {
    nonisolated(unsafe) static var status = 200
    nonisolated(unsafe) static var json = "{\"ok\":true}"
    nonisolated(unsafe) static var requests: [URLRequest] = []
    nonisolated(unsafe) static var bodies: [Data] = []
    nonisolated(unsafe) static var error: URLError?

    static func reset(status: Int = 200, json: String = "{\"ok\":true}", error: URLError? = nil) {
        self.status = status
        self.json = json
        self.error = error
        requests = []
        bodies = []
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(request)
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
            stream.close()
        }
        Self.bodies.append(data)
        if let error = Self.error {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: Self.status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(Self.json.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class PushClientTests: XCTestCase {
    private func client(token: String? = "device-secret", base: String = "https://darkroom.example.test") -> PushClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [PushStub.self]
        return PushClient(
            baseURL: URL(string: base)!,
            session: URLSession(configuration: configuration),
            tokenProvider: { token }
        )
    }

    override func setUp() {
        PushStub.reset()
    }

    func testRegisterPutsTheTokenWithTheBearerHeader() async throws {
        try await client().registerPushToken("ab12")
        let request = try XCTUnwrap(PushStub.requests.first)
        XCTAssertEqual(request.httpMethod, "PUT")
        XCTAssertEqual(request.url?.absoluteString, "https://darkroom.example.test/api/device/push-token")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer device-secret")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let sent = try JSONSerialization.jsonObject(with: try XCTUnwrap(PushStub.bodies.first)) as? [String: String]
        XCTAssertEqual(sent, ["token": "ab12"])
    }

    func testForgetDeletesWithoutABody() async throws {
        try await client().forgetPushToken()
        let request = try XCTUnwrap(PushStub.requests.first)
        XCTAssertEqual(request.httpMethod, "DELETE")
        XCTAssertEqual(request.url?.path, "/api/device/push-token")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer device-secret")
        XCTAssertEqual(PushStub.bodies.first, Data())
    }

    func testAPathPrefixAndATrailingSlashAreKept() async throws {
        try await client(base: "https://darkroom.example.test/darkroom/").forgetPushToken()
        XCTAssertEqual(PushStub.requests.first?.url?.path, "/darkroom/api/device/push-token")
    }

    func testWithoutATokenNothingIsSent() async {
        do {
            try await client(token: nil).registerPushToken("ab12")
            XCTFail("expected a refusal")
        } catch {
            XCTAssertEqual(error as? APIError, .notSignedIn)
        }
        XCTAssertTrue(PushStub.requests.isEmpty)
    }

    func testRefusalsKeepTheirMeaning() async {
        let cases: [(Int, String, APIError)] = [
            (401, "{\"reason\":\"device_revoked\"}", .revoked),
            (401, "{}", .unauthorized),
            (409, "{}", .rejected(status: 409)),
            (400, "{}", .rejected(status: 400)),
            (503, "{}", .server(status: 503)),
        ]
        for (status, json, expected) in cases {
            PushStub.reset(status: status, json: json)
            do {
                try await client().registerPushToken("ab12")
                XCTFail("expected \(expected)")
            } catch {
                XCTAssertEqual(error as? APIError, expected)
            }
        }
    }

    func testANetworkFailureIsATransportError() async {
        PushStub.reset(error: URLError(.notConnectedToInternet))
        do {
            try await client().forgetPushToken()
            XCTFail("expected a failure")
        } catch {
            guard case .transport = error as? APIError else { return XCTFail("\(error)") }
        }
    }
}
