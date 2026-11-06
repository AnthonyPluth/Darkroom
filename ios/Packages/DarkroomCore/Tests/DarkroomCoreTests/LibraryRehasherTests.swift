import XCTest
@testable import DarkroomCore

private struct StubLibrary: PhotoLibrary {
    var resources: [PhotoResource]
    func allResources() async throws -> [PhotoResource] { resources }
}

private struct StubExporter: ResourceExporter {
    var contents: [ResourceKey: Data] = [:]
    var cloudOnly: Set<ResourceKey> = []

    func export(_ resource: PhotoResource, to destination: URL) async throws -> ExportOutcome {
        if cloudOnly.contains(resource.key) { return .notLocal }
        try (contents[resource.key] ?? Data()).write(to: destination)
        return .exported
    }
}

final class LibraryRehasherTests: XCTestCase {
    private let photo = PhotoResource(assetID: "A", kind: .photo, originalFilename: "IMG_1.HEIC", version: "v1")
    private let clip = PhotoResource(assetID: "A", kind: .liveVideo, originalFilename: "x.mov", version: "v1")
    private let other = PhotoResource(assetID: "B", kind: .photo, originalFilename: "IMG_2.HEIC", version: "v1")

    func testRehashingReadsTheCurrentBytesUnderTheUploadName() async throws {
        let rehasher = LibraryRehasher(
            library: StubLibrary(resources: [photo, clip, other]),
            exporter: StubExporter(contents: [clip.key: Data("abc".utf8)])
        )
        let fresh = try await rehasher.rehash(clip.key)
        XCTAssertEqual(fresh?.sha1, "a9993e364706816aba3e25717850c26c9cd0d89d")
        XCTAssertEqual(fresh?.bytes, 3)
        XCTAssertEqual(fresh?.filename, "IMG_1.MOV")
        XCTAssertEqual(fresh?.state, .hashed)
    }

    func testAResourceGoneFromTheLibraryOrWithAnUnusableNameHasNothingToRehash() async throws {
        let gone = try await LibraryRehasher(library: StubLibrary(resources: [other]), exporter: StubExporter()).rehash(photo.key)
        XCTAssertNil(gone)
        let hidden = PhotoResource(assetID: "C", kind: .photo, originalFilename: ".hidden", version: "v1")
        let unusable = try await LibraryRehasher(library: StubLibrary(resources: [hidden]), exporter: StubExporter()).rehash(hidden.key)
        XCTAssertNil(unusable)
    }

    func testAResourceThatMovedToICloudRehashesAsNotLocal() async throws {
        let rehasher = LibraryRehasher(library: StubLibrary(resources: [photo]), exporter: StubExporter(cloudOnly: [photo.key]))
        let fresh = try await rehasher.rehash(photo.key)
        XCTAssertEqual(fresh?.state, .notLocal)
        XCTAssertEqual(fresh?.hasDescriptor, false)
    }

    func testTheDefaultAssetLookupFiltersTheWholeLibrary() async throws {
        let found = try await StubLibrary(resources: [photo, clip, other]).resources(ofAsset: "A")
        XCTAssertEqual(found, [photo, clip])
    }

    func testLocalExportFailuresAreFilesNotOutages() {
        struct WriteFailed: Error {}
        XCTAssertEqual(APIError.forLocalExport(ExportError.resourceGone) as? APIError, .rejected(status: 409))
        XCTAssertEqual(APIError.forLocalExport(WriteFailed()) as? APIError, .rejected(status: 422))
        XCTAssertTrue(APIError.forLocalExport(CancellationError()) is CancellationError)
        XCTAssertEqual(APIError.forLocalExport(APIError.notSignedIn) as? APIError, .notSignedIn)
    }

    func testUploadStatusesMapToTheErrorsTheCoordinatorUnderstands() {
        XCTAssertNil(APIError.forUpload(status: 200))
        XCTAssertEqual(APIError.forUpload(status: 401), .unauthorized)
        XCTAssertEqual(APIError.forUpload(status: 422), .rejected(status: 422))
        XCTAssertEqual(APIError.forUpload(status: 408), .rejected(status: 408))
        XCTAssertEqual(APIError.forUpload(status: 503), .server(status: 503))
        XCTAssertEqual(APIError.forUpload(status: 0), .invalidResponse)
    }
}
