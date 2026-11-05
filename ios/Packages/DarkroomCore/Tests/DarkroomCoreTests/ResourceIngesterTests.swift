import XCTest
@testable import DarkroomCore

private struct FakeLibrary: PhotoLibrary {
    var resources: [PhotoResource]
    func allResources() async throws -> [PhotoResource] { resources }
}

private actor FakeExporter: ResourceExporter {
    var contents: [ResourceKey: Data] = [:]
    var cloudOnly: Set<ResourceKey> = []
    var broken: Set<ResourceKey> = []
    private(set) var exports = 0
    private(set) var destinations: [URL] = []

    struct Broken: Error {}

    func set(contents: Data, for key: ResourceKey) { self.contents[key] = contents }
    func setCloudOnly(_ key: ResourceKey) { cloudOnly.insert(key) }
    func setBroken(_ key: ResourceKey) { broken.insert(key) }
    func setFixed(_ key: ResourceKey) { broken.remove(key) }

    func export(_ resource: PhotoResource, to destination: URL) async throws -> ExportOutcome {
        exports += 1
        destinations.append(destination)
        if broken.contains(resource.key) { throw Broken() }
        if cloudOnly.contains(resource.key) { return .notLocal }
        try (contents[resource.key] ?? Data()).write(to: destination)
        return .exported
    }
}

final class ResourceIngesterTests: XCTestCase {
    private func photo(_ id: String, _ version: String = "v1") -> PhotoResource {
        PhotoResource(assetID: id, kind: .photo, originalFilename: "\(id).HEIC", version: version)
    }

    private func make(_ resources: [PhotoResource], exporter: FakeExporter, store: InMemoryResourceStore,
                      versions: InMemoryIngestVersions = InMemoryIngestVersions()) -> ResourceIngester {
        ResourceIngester(library: FakeLibrary(resources: resources), exporter: exporter, store: store, versions: versions)
    }

    func testHashesAndRecordsEachResource() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let a = photo("IMG_1")
        await exporter.set(contents: Data("abc".utf8), for: a.key)
        let summary = try await make([a], exporter: exporter, store: store).run()
        XCTAssertEqual(summary, IngestSummary(hashed: 1))
        let record = try await store.record(for: a.key)
        XCTAssertEqual(record?.resource.sha1, "a9993e364706816aba3e25717850c26c9cd0d89d")
        XCTAssertEqual(record?.resource.bytes, 3)
        XCTAssertEqual(record?.resource.state, .hashed)
        XCTAssertEqual(record?.resource.filename, "IMG_1.HEIC")
    }

    func testNotLocalIsRecordedWithoutHashing() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let a = photo("IMG_1")
        await exporter.setCloudOnly(a.key)
        let summary = try await make([a], exporter: exporter, store: store).run()
        XCTAssertEqual(summary, IngestSummary(notLocal: 1))
        let record = try await store.record(for: a.key)
        XCTAssertEqual(record?.resource.state, .notLocal)
    }

    func testUnchangedResourcesAreSkippedAndChangedOnesRehashed() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let a = photo("IMG_1")
        await exporter.set(contents: Data("one".utf8), for: a.key)
        _ = try await make([a], exporter: exporter, store: store, versions: versions).run()
        let again = try await make([a], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(again, IngestSummary(skipped: 1))
        let exportsAfterSkip = await exporter.exports
        XCTAssertEqual(exportsAfterSkip, 1)

        await exporter.set(contents: Data("two!".utf8), for: a.key)
        let changed = try await make([photo("IMG_1", "v2")], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(changed, IngestSummary(hashed: 1))
        let record = try await store.record(for: a.key)
        XCTAssertEqual(record?.resource.bytes, 4)
    }

    func testFailedExportIsCountedNotRecordedAndRetried() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let a = photo("IMG_1")
        let b = photo("IMG_2")
        await exporter.setBroken(a.key)
        let first = try await make([a, b], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(first, IngestSummary(hashed: 1, failed: 1))
        let missing = try await store.record(for: a.key)
        XCTAssertNil(missing)
        await exporter.setFixed(a.key)
        let second = try await make([a, b], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(second, IngestSummary(hashed: 1, skipped: 1))
    }

    func testTemporaryFilesAreRemoved() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let a = photo("IMG_1")
        await exporter.set(contents: Data("abc".utf8), for: a.key)
        _ = try await make([a], exporter: exporter, store: store).run()
        let destinations = await exporter.destinations
        XCTAssertEqual(destinations.count, 1)
        XCTAssertFalse(FileManager.default.fileExists(atPath: destinations[0].path))
    }

    func testUploadFilenamesFollowThePhoto() async throws {
        let photoResource = PhotoResource(assetID: "x", kind: .photo, originalFilename: "IMG_0007.HEIC", version: "v")
        let video = PhotoResource(assetID: "x", kind: .liveVideo, originalFilename: "IMG_0007.MOV", version: "v")
        let adjusted = PhotoResource(assetID: "x", kind: .adjusted, originalFilename: "FullSizeRender.jpg", version: "v")
        let all = [adjusted, video, photoResource]
        XCTAssertEqual(UploadFilename.make(for: photoResource, assetResources: all), "IMG_0007.HEIC")
        XCTAssertEqual(UploadFilename.make(for: video, assetResources: all), "IMG_0007.MOV")
        XCTAssertEqual(UploadFilename.make(for: adjusted, assetResources: all), "IMG_0007_adjusted.jpg")
        let bare = PhotoResource(assetID: "y", kind: .adjusted, originalFilename: "Render", version: "v")
        XCTAssertEqual(UploadFilename.make(for: bare, assetResources: [bare]), "Render_adjusted.jpg")
    }

    func testFileVersionsSurviveAReopen() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("versions-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let key = ResourceKey(assetID: "a/b|c", kind: .liveVideo)
        await FileIngestVersions(url: url).setVersion("v9", for: key)
        let reopened = await FileIngestVersions(url: url).version(for: key)
        XCTAssertEqual(reopened, "v9")
    }
}
