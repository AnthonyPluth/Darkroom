import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import DarkroomCore

final class StubProtocol: URLProtocol {
    struct Stub {
        var status: Int
        var body: Data
    }

    nonisolated(unsafe) static var stub = Stub(status: 200, body: Data())
    nonisolated(unsafe) static var requests: [URLRequest] = []
    nonisolated(unsafe) static var bodies: [Data] = []
    nonisolated(unsafe) static var error: URLError?

    static func reset(status: Int = 200, json: String = "{}", error: URLError? = nil) {
        stub = Stub(status: status, body: Data(json.utf8))
        requests = []
        bodies = []
        self.error = error
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(request)
        if let stream = request.httpBodyStream {
            stream.open()
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
            stream.close()
            Self.bodies.append(data)
        } else {
            Self.bodies.append(request.httpBody ?? Data())
        }
        if let error = Self.error {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: Self.stub.status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Self.stub.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class TimingOutProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { client?.urlProtocol(self, didFailWithError: URLError(.timedOut)) }
    override func stopLoading() {}
}

final class APIClientTests: XCTestCase {
    private func client(token: String? = "secret-token", base: String = "https://darkroom.example.test") -> APIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubProtocol.self]
        return APIClient(
            baseURL: URL(string: base)!,
            session: URLSession(configuration: configuration),
            tokenProvider: { token }
        )
    }

    override func setUp() {
        super.setUp()
        StubProtocol.reset()
    }

    func testCheckSendsBearerTokenAndDecodesNeeded() async throws {
        StubProtocol.reset(json: #"{"needed":[{"id":"A/1","kind":"live_video"}]}"#)
        let request = CheckRequest(assets: [
            AssetDescriptor(id: "A/1", resources: [ResourceDescriptor(kind: .photo, sha1: "abc", bytes: 5, filename: "IMG_1.HEIC")]),
        ])
        let reply = try await client().check(request)
        XCTAssertEqual(reply.needed, [NeededResource(id: "A/1", kind: .liveVideo)])
        let sent = try XCTUnwrap(StubProtocol.requests.first)
        XCTAssertEqual(sent.httpMethod, "POST")
        XCTAssertEqual(sent.url?.absoluteString, "https://darkroom.example.test/api/device/assets/check")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: StubProtocol.bodies[0]) as? [String: Any])
        let assets = try XCTUnwrap(json["assets"] as? [[String: Any]])
        let resources = try XCTUnwrap(assets[0]["resources"] as? [[String: Any]])
        XCTAssertEqual(resources[0]["kind"] as? String, "photo")
        XCTAssertEqual(resources[0]["sha1"] as? String, "abc")
        XCTAssertEqual(resources[0]["bytes"] as? Int, 5)
        XCTAssertEqual(resources[0]["filename"] as? String, "IMG_1.HEIC")
    }

    func testBaseURLWithTrailingSlashAndPathPrefix() async throws {
        StubProtocol.reset(json: #"{"needed":[]}"#)
        _ = try await client(base: "https://darkroom.example.test/prefix/").check(CheckRequest(assets: []))
        XCTAssertEqual(StubProtocol.requests[0].url?.absoluteString, "https://darkroom.example.test/prefix/api/device/assets/check")
    }

    func testCheckinBodyUsesSnakeCase() async throws {
        try await client().checkin(CheckinRequest(pendingUploads: 3, appVersion: "1.0 (7)"))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: StubProtocol.bodies[0]) as? [String: Any])
        XCTAssertEqual(json["pending_uploads"] as? Int, 3)
        XCTAssertEqual(json["app_version"] as? String, "1.0 (7)")
        XCTAssertEqual(StubProtocol.requests[0].url?.path, "/api/device/checkin")
    }

    func testProbeIsAnUnauthenticatedGetOnHealthz() async throws {
        StubProtocol.reset(json: #"{"ok":true,"version":"1"}"#)
        try await client().probe()
        XCTAssertEqual(StubProtocol.requests[0].httpMethod, "GET")
        XCTAssertEqual(StubProtocol.requests[0].url?.path, "/healthz")
        XCTAssertNil(StubProtocol.requests[0].value(forHTTPHeaderField: "Authorization"))
    }

    func testProbeWorksWithoutAToken() async throws {
        try await client(token: nil).probe()
        XCTAssertEqual(StubProtocol.requests.count, 1)
    }

    func testServerProbeReachableOn200() async {
        StubProtocol.reset()
        let reachable = await ServerProbe(client: client())()
        XCTAssertTrue(reachable)
    }

    func testServerProbeUnreachableOnNetworkError() async {
        StubProtocol.reset(error: URLError(.cannotConnectToHost))
        let reachable = await ServerProbe(client: client())()
        XCTAssertFalse(reachable)
    }

    func testServerProbeTreatsAnyNon2xxAsUnreachable() async {
        for status in [401, 403, 502, 503] {
            StubProtocol.reset(status: status)
            let reachable = await ServerProbe(client: client())()
            XCTAssertFalse(reachable, "status \(status)")
        }
    }

    func testEligibleConfirmAndDeletedRoundTrip() async throws {
        StubProtocol.reset(json: #"{"eligible":[{"asset_id":"a1","bytes":42}]}"#)
        let eligible = try await client().eligible(EligibleRequest(assets: []))
        XCTAssertEqual(eligible.eligible, [EligibleAsset(assetID: "a1", bytes: 42)])
        XCTAssertEqual(StubProtocol.requests[0].url?.path, "/api/device/eligible")

        StubProtocol.reset(json: #"{"confirmed":["a1"],"rejected":[{"asset_id":"a2","reason":"file_missing"}]}"#)
        let confirm = try await client().confirm(ConfirmRequest(assets: []))
        XCTAssertEqual(confirm.confirmed, ["a1"])
        XCTAssertEqual(confirm.rejected, [RejectedAsset(assetID: "a2", reason: "file_missing")])
        XCTAssertEqual(StubProtocol.requests[0].url?.path, "/api/device/eligible/confirm")

        StubProtocol.reset()
        try await client().deleted(DeletedRequest(assetIDs: ["a1"]))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: StubProtocol.bodies[0]) as? [String: Any])
        XCTAssertEqual(json["asset_ids"] as? [String], ["a1"])
        XCTAssertEqual(StubProtocol.requests[0].url?.path, "/api/device/deleted")
    }

    func testNoTokenFailsBeforeAnyRequest() async {
        do {
            _ = try await client(token: nil).check(CheckRequest(assets: []))
            XCTFail("expected notSignedIn")
        } catch {
            XCTAssertEqual(error as? APIError, .notSignedIn)
        }
        XCTAssertTrue(StubProtocol.requests.isEmpty)
    }

    func testStatusCodesMapToTypedErrors() async {
        let cases: [(Int, APIError)] = [
            (401, .unauthorized), (404, .rejected(status: 404)), (422, .rejected(status: 422)),
            (500, .server(status: 500)), (503, .server(status: 503)),
        ]
        for (status, expected) in cases {
            StubProtocol.reset(status: status)
            do {
                try await client().probe()
                XCTFail("expected \(expected)")
            } catch {
                XCTAssertEqual(error as? APIError, expected)
            }
        }
    }

    func testTransportFailureIsTyped() async {
        StubProtocol.reset(error: URLError(.notConnectedToInternet))
        do {
            try await client().probe()
            XCTFail("expected transport error")
        } catch {
            guard case .transport = error as? APIError else { return XCTFail("wrong error \(error)") }
        }
    }

    func testACancelledRequestThrowsCancellationNotATransportError() async {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [TimingOutProtocol.self]
        let api = APIClient(
            baseURL: URL(string: "https://darkroom.example.test")!,
            session: URLSession(configuration: configuration),
            tokenProvider: { "secret-token" }
        )
        let call = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await api.check(CheckRequest(assets: []))
        }
        switch await call.result {
        case .success: XCTFail("expected cancellation")
        case .failure(let error): XCTAssertTrue(error is CancellationError, "threw \(error)")
        }
    }

    func testMalformedReplyIsADecodingError() async {
        StubProtocol.reset(json: "not json")
        do {
            _ = try await client().check(CheckRequest(assets: []))
            XCTFail("expected decoding error")
        } catch {
            XCTAssertEqual(error as? APIError, .decoding)
        }
    }

    func testTokenIsNeverPartOfTheErrorDescription() async {
        StubProtocol.reset(status: 401)
        do {
            try await client().probe()
        } catch {
            XCTAssertFalse(String(describing: error).contains("secret-token"))
        }
    }

    func testUploadRequestCarriesTheHeadersTheServerChecks() async throws {
        let request = try await client().uploadRequest(assetID: "ABC/def", kind: .liveVideo, sha1: "0123", filename: "IMG 1é.MOV")
        XCTAssertEqual(request.httpMethod, "PUT")
        XCTAssertEqual(request.url?.absoluteString, "https://darkroom.example.test/api/device/assets/ABC%2Fdef/resources/live_video")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Darkroom-SHA1"), "0123")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Darkroom-Filename"), "IMG%201%C3%A9.MOV")
    }

    func testUploadRequestWithoutTokenFails() async {
        do {
            _ = try await client(token: nil).uploadRequest(assetID: "a", kind: .photo, sha1: "x", filename: "f")
            XCTFail("expected notSignedIn")
        } catch {
            XCTAssertEqual(error as? APIError, .notSignedIn)
        }
    }
}
