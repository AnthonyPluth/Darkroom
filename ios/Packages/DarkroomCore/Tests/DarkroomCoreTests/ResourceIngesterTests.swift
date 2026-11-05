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
    func setLocal(_ key: ResourceKey) { cloudOnly.remove(key) }
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

    func testNotLocalResourceIsIngestedOnceItBecomesLocalWithTheSameVersion() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let a = photo("IMG_1")
        await exporter.setCloudOnly(a.key)
        let first = try await make([a], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(first, IngestSummary(notLocal: 1))
        let stillCloud = try await make([a], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(stillCloud, IngestSummary(notLocal: 1))

        await exporter.setLocal(a.key)
        await exporter.set(contents: Data("abc".utf8), for: a.key)
        let downloaded = try await make([a], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(downloaded, IngestSummary(hashed: 1))
        let record = try await store.record(for: a.key)
        XCTAssertEqual(record?.resource.state, .hashed)
        XCTAssertEqual(record?.resource.sha1, "a9993e364706816aba3e25717850c26c9cd0d89d")
    }

    func testRetryingANotLocalResourceDoesNotTouchTheVersions() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let versions = CountingVersions()
        let a = photo("IMG_1")
        await exporter.setCloudOnly(a.key)
        for _ in 0..<3 {
            let summary = try await ResourceIngester(
                library: FakeLibrary(resources: [a]), exporter: exporter, store: store, versions: versions
            ).run()
            XCTAssertEqual(summary, IngestSummary(notLocal: 1))
        }
        let writes = await versions.writes
        XCTAssertEqual(writes, 1)
    }

    func testUnacceptableUploadNamesFailBeforeAnythingIsExportedOrSaved() async throws {
        for name in [".hidden.HEIC", "a/b.HEIC", "a\\b.HEIC", "IMG_1.XMP", "", String(repeating: "a", count: 201), "a\u{0}b.HEIC"] {
            XCTAssertFalse(UploadFilename.isAcceptable(name), name)
        }
        XCTAssertTrue(UploadFilename.isAcceptable("IMG 0001 \u{00E9}.HEIC"))
        XCTAssertTrue(UploadFilename.isAcceptable(String(repeating: "a", count: 200)))

        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let versions = CountingVersions()
        let bad = PhotoResource(assetID: "x", kind: .photo, originalFilename: ".hidden.HEIC", version: "v1")
        let summary = try await ResourceIngester(
            library: FakeLibrary(resources: [bad]), exporter: exporter, store: store, versions: versions
        ).run()
        XCTAssertEqual(summary, IngestSummary(failed: 1))
        let exports = await exporter.exports
        let writes = await versions.writes
        let record = try await store.record(for: bad.key)
        XCTAssertEqual(exports, 0)
        XCTAssertEqual(writes, 0)
        XCTAssertNil(record)
    }

    func testCancellationAfterTheStoreWriteLeavesTheVersionAbsentForARetry() async throws {
        let exporter = FakeExporter()
        let inner = InMemoryResourceStore()
        let old = LocalResource(key: photo("IMG_1").key, sha1: "old", bytes: 1, filename: "IMG_1.HEIC", state: .uploaded)
        try await inner.upsert([old])
        await exporter.set(contents: Data("new".utf8), for: old.key)
        let versions = InMemoryIngestVersions()
        try await versions.setVersion("v1", for: old.key)
        let library = FakeLibrary(resources: [photo("IMG_1", "v2")])
        let ingester = ResourceIngester(library: library, exporter: exporter, store: CancellingStore(inner: inner), versions: versions)
        let task = Task { try await ingester.run() }
        do {
            _ = try await task.value
            XCTFail("the run should have been cancelled")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        let version = await versions.version(for: old.key)
        XCTAssertNil(version)
        let retry = try await ResourceIngester(library: library, exporter: exporter, store: inner, versions: versions).run()
        XCTAssertEqual(retry, IngestSummary(hashed: 1))
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

    func testResourcesGoneFromALaterEnumerationAreForgottenAndOthersKept() async throws {
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let kept = photo("IMG_1")
        let gone = photo("IMG_2")
        let live = PhotoResource(assetID: "IMG_1", kind: .liveVideo, originalFilename: "IMG_1.MOV", version: "v1")
        await exporter.set(contents: Data("one".utf8), for: kept.key)
        await exporter.set(contents: Data("two".utf8), for: gone.key)
        await exporter.set(contents: Data("vid".utf8), for: live.key)
        let first = try await make([kept, live, gone], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(first, IngestSummary(hashed: 3))

        let second = try await make([kept], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(second, IngestSummary(skipped: 1, removed: 2))
        let remaining = try await store.resources(in: Set(ResourceState.allCases))
        XCTAssertEqual(remaining.map(\.key), [kept.key])
        for key in [gone.key, live.key] {
            let version = await versions.version(for: key)
            XCTAssertNil(version)
        }
        let keptVersion = await versions.version(for: kept.key)
        XCTAssertEqual(keptVersion, "v1")

        let back = try await make([kept, gone], exporter: exporter, store: store, versions: versions).run()
        XCTAssertEqual(back, IngestSummary(hashed: 1, skipped: 1))
    }

    func testGoneResourcesInEveryStateLeaveTheStore() async throws {
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let stale = ResourceState.allCases.enumerated().map { index, state in
            LocalResource(key: ResourceKey(assetID: "gone-\(index)", kind: .photo), sha1: "s", bytes: 1, filename: "f.HEIC", state: state)
        }
        try await store.upsert(stale)
        for resource in stale { try await versions.setVersion("v1", for: resource.key) }
        let summary = try await make([], exporter: FakeExporter(), store: store, versions: versions).run()
        XCTAssertEqual(summary, IngestSummary(removed: stale.count))
        let counts = try await store.counts()
        XCTAssertTrue(counts.isEmpty)
    }

    func testAFailedEnumerationForgetsNothing() async throws {
        struct Unavailable: PhotoLibrary {
            struct Denied: Error {}
            func allResources() async throws -> [PhotoResource] { throw Denied() }
        }
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let a = photo("IMG_1")
        try await store.upsert([LocalResource(key: a.key, sha1: "s", bytes: 1, filename: "IMG_1.HEIC", state: .uploaded)])
        try await versions.setVersion("v1", for: a.key)
        let ingester = ResourceIngester(library: Unavailable(), exporter: FakeExporter(), store: store, versions: versions)
        do {
            _ = try await ingester.run()
            XCTFail("the enumeration failed")
        } catch {}
        let record = try await store.record(for: a.key)
        let version = await versions.version(for: a.key)
        XCTAssertNotNil(record)
        XCTAssertEqual(version, "v1")
    }

    func testTheVersionIsForgottenBeforeTheRecordSoAnInterruptedCleanupIsRepeated() async throws {
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let gone = photo("IMG_9")
        try await store.upsert([LocalResource(key: gone.key, sha1: "s", bytes: 1, filename: "IMG_9.HEIC", state: .hashed)])
        try await versions.setVersion("v1", for: gone.key)
        let failing = ResourceIngester(
            library: FakeLibrary(resources: []), exporter: FakeExporter(), store: RemovalRefusingStore(inner: store), versions: versions
        )
        do {
            _ = try await failing.run()
            XCTFail("the removal was refused")
        } catch {}
        let versionAfter = await versions.version(for: gone.key)
        let recordAfter = try await store.record(for: gone.key)
        XCTAssertNil(versionAfter)
        XCTAssertNotNil(recordAfter)

        let retry = try await make([], exporter: FakeExporter(), store: store, versions: versions).run()
        XCTAssertEqual(retry, IngestSummary(removed: 1))
        let recordGone = try await store.record(for: gone.key)
        XCTAssertNil(recordGone)
    }

    func testForgettingAppendsToTheJournalAndSurvivesAReopen() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("versions-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let a = photo("IMG_1")
        let b = photo("IMG_2")
        _ = try await make([a, b], exporter: exporter, store: store, versions: FileIngestVersions(url: url)).run()
        let before = try Data(contentsOf: url)
        let summary = try await make([a], exporter: exporter, store: store, versions: FileIngestVersions(url: url)).run()
        XCTAssertEqual(summary, IngestSummary(skipped: 1, removed: 1))
        let after = try Data(contentsOf: url)
        XCTAssertTrue(after.starts(with: before))
        XCTAssertGreaterThan(after.count, before.count)
        let reopened = FileIngestVersions(url: url)
        let forgotten = await reopened.version(for: b.key)
        let kept = await reopened.version(for: a.key)
        XCTAssertNil(forgotten)
        XCTAssertEqual(kept, "v1")
    }

    func testCancellationAfterTheLastUnchangedResourceIsNotASuccessfulRun() async throws {
        let store = InMemoryResourceStore()
        let inner = InMemoryIngestVersions()
        let a = photo("IMG_1")
        try await store.upsert([LocalResource(key: a.key, sha1: "s", bytes: 1, filename: "IMG_1.HEIC", state: .uploaded)])
        try await inner.setVersion("v1", for: a.key)
        let ingester = ResourceIngester(
            library: FakeLibrary(resources: [a]), exporter: FakeExporter(), store: store, versions: CancellingVersions(inner: inner)
        )
        let task = Task { try await ingester.run() }
        do {
            let summary = try await task.value
            XCTFail("the run should have been cancelled, not returned \(summary)")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
    }

    func testCancellationDuringAnUnchangedNotLocalExportIsNotASuccessfulRun() async throws {
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let a = photo("IMG_1")
        let cloud = FakeExporter()
        await cloud.setCloudOnly(a.key)
        _ = try await make([a], exporter: cloud, store: store, versions: versions).run()

        let ingester = ResourceIngester(
            library: FakeLibrary(resources: [a]), exporter: CancellingCloudExporter(), store: store, versions: versions
        )
        let task = Task { try await ingester.run() }
        do {
            let summary = try await task.value
            XCTFail("the run should have been cancelled, not returned \(summary)")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
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
        try await FileIngestVersions(url: url).setVersion("v9", for: key)
        let reopened = await FileIngestVersions(url: url).version(for: key)
        XCTAssertEqual(reopened, "v9")
    }

    func testSavingAVersionAppendsInsteadOfRewritingTheFile() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("versions-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let versions = FileIngestVersions(url: url)
        var previous = Data()
        for index in 0..<20 {
            try await versions.setVersion("v\(index)", for: ResourceKey(assetID: "asset-\(index)", kind: .photo))
            let current = try Data(contentsOf: url)
            XCTAssertTrue(current.starts(with: previous))
            XCTAssertGreaterThan(current.count, previous.count)
            previous = current
        }
        let reopened = FileIngestVersions(url: url)
        let first = await reopened.version(for: ResourceKey(assetID: "asset-0", kind: .photo))
        let last = await reopened.version(for: ResourceKey(assetID: "asset-19", kind: .photo))
        XCTAssertEqual(first, "v0")
        XCTAssertEqual(last, "v19")
    }

    func testForgottenAndTornJournalEntriesAreHandledOnReopen() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("versions-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let key = ResourceKey(assetID: "a", kind: .photo)
        let other = ResourceKey(assetID: "b", kind: .photo)
        let versions = FileIngestVersions(url: url)
        try await versions.setVersion("v1", for: key)
        try await versions.setVersion(nil, for: key)
        try await versions.setVersion("v2", for: other)
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        try handle.write(contentsOf: Data("\n{\"key\":\"photo|c\",\"ver".utf8))
        try handle.close()
        try await FileIngestVersions(url: url).setVersion("v3", for: ResourceKey(assetID: "d", kind: .photo))
        let reopened = FileIngestVersions(url: url)
        let forgotten = await reopened.version(for: key)
        let kept = await reopened.version(for: other)
        let afterTear = await reopened.version(for: ResourceKey(assetID: "d", kind: .photo))
        XCTAssertNil(forgotten)
        XCTAssertEqual(kept, "v2")
        XCTAssertEqual(afterTear, "v3")
    }

    func testVersionIsNotAcceptedWhenTheFileCannotBeWritten() async throws {
        let missing = FileManager.default.temporaryDirectory
            .appendingPathComponent("no-such-\(UUID().uuidString)").appendingPathComponent("versions.json")
        let versions = FileIngestVersions(url: missing)
        let key = ResourceKey(assetID: "a", kind: .photo)
        do {
            try await versions.setVersion("v1", for: key)
            XCTFail("the write should have failed")
        } catch {}
        let stored = await versions.version(for: key)
        XCTAssertNil(stored)
    }

    func testFailedVersionSaveLeavesTheStoreUntouchedAndCountsFailed() async throws {
        struct Unwritable: IngestVersions {
            struct Full: Error {}
            func version(for key: ResourceKey) async -> String? { "v1" }
            func setVersion(_ version: String?, for key: ResourceKey) async throws { throw Full() }
        }
        let exporter = FakeExporter()
        let store = InMemoryResourceStore()
        let old = LocalResource(key: photo("IMG_1").key, sha1: "old", bytes: 1, filename: "IMG_1.HEIC", state: .uploaded)
        try await store.upsert([old])
        await exporter.set(contents: Data("new".utf8), for: old.key)
        let ingester = ResourceIngester(
            library: FakeLibrary(resources: [photo("IMG_1", "v2")]), exporter: exporter, store: store, versions: Unwritable()
        )
        let summary = try await ingester.run()
        XCTAssertEqual(summary, IngestSummary(failed: 1))
        let record = try await store.record(for: old.key)
        XCTAssertEqual(record?.resource, old)
    }

    func testInterruptionBetweenTheVersionAndStoreWritesIsRetriedNotSkipped() async throws {
        let exporter = FakeExporter()
        let inner = InMemoryResourceStore()
        let old = LocalResource(key: photo("IMG_1").key, sha1: "old", bytes: 1, filename: "IMG_1.HEIC", state: .uploaded)
        try await inner.upsert([old])
        await exporter.set(contents: Data("new".utf8), for: old.key)
        let versions = DyingVersions(initial: "v1")
        let library = FakeLibrary(resources: [photo("IMG_1", "v2")])

        let crashing = ResourceIngester(library: library, exporter: exporter, store: CrashingStore(inner: inner), versions: versions)
        let interrupted = try await crashing.run()
        XCTAssertEqual(interrupted, IngestSummary(failed: 1))
        let afterCrash = try await inner.record(for: old.key)
        XCTAssertEqual(afterCrash?.resource, old)
        let versionAfterCrash = await versions.version(for: old.key)
        XCTAssertNotEqual(versionAfterCrash, "v2")

        await versions.revive()
        let relaunch = ResourceIngester(library: library, exporter: exporter, store: inner, versions: versions)
        let summary = try await relaunch.run()
        XCTAssertEqual(summary, IngestSummary(hashed: 1))
        let record = try await inner.record(for: old.key)
        XCTAssertEqual(record?.resource.sha1, "c2a6b03f190dfb2b4aa91f8af8d477a9bc3401dc")
    }

    func testCancellationDuringHashingLeavesTheStoreAndVersionsUntouched() async throws {
        let exporter = CancellingExporter()
        let store = InMemoryResourceStore()
        let versions = InMemoryIngestVersions()
        let a = photo("IMG_1")
        let ingester = ResourceIngester(
            library: FakeLibrary(resources: [a]), exporter: exporter, store: store, versions: versions
        )
        let task = Task { try await ingester.run() }
        do {
            _ = try await task.value
            XCTFail("the run should have been cancelled")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        let record = try await store.record(for: a.key)
        XCTAssertNil(record)
        let version = await versions.version(for: a.key)
        XCTAssertNil(version)
        let destinations = await exporter.destinations
        XCTAssertFalse(FileManager.default.fileExists(atPath: destinations[0].path))
    }
}

/// Writes a multi-chunk file, then cancels the task that is exporting, so the cancellation lands before hashing starts.
private actor CancellingExporter: ResourceExporter {
    private(set) var destinations: [URL] = []

    func export(_ resource: PhotoResource, to destination: URL) async throws -> ExportOutcome {
        destinations.append(destination)
        try Data(count: 3 * SHA1Hasher.chunkSize).write(to: destination)
        withUnsafeCurrentTask { $0?.cancel() }
        return .exported
    }
}

private struct CrashingStore: ResourceStore {
    struct Crash: Error {}
    let inner: InMemoryResourceStore
    func upsert(_ resources: [LocalResource]) async throws { throw Crash() }
    func record(for key: ResourceKey) async throws -> ResourceRecord? { try await inner.record(for: key) }
    func records(in states: Set<ResourceState>) async throws -> [ResourceRecord] { try await inner.records(in: states) }
    func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws {
        try await inner.setState(state, for: keys, failureReason: failureReason)
    }
    func remove(_ keys: [ResourceKey]) async throws { try await inner.remove(keys) }
    func recordAttempt(for keys: [ResourceKey]) async throws { try await inner.recordAttempt(for: keys) }
    func recordRehash(for key: ResourceKey) async throws { try await inner.recordRehash(for: key) }
    func counts() async throws -> [ResourceState: Int] { try await inner.counts() }
}

private actor DyingVersions: IngestVersions {
    struct Dead: Error {}
    private var stored: String?
    private var writes = 0
    private var alive = false

    init(initial: String) { stored = initial }

    func revive() { alive = true }
    func version(for key: ResourceKey) -> String? { stored }
    func setVersion(_ version: String?, for key: ResourceKey) throws {
        writes += 1
        if !alive && writes > 1 { throw Dead() }
        stored = version
    }
}

private struct CancellingStore: ResourceStore {
    let inner: InMemoryResourceStore
    func upsert(_ resources: [LocalResource]) async throws {
        try await inner.upsert(resources)
        withUnsafeCurrentTask { $0?.cancel() }
    }
    func record(for key: ResourceKey) async throws -> ResourceRecord? { try await inner.record(for: key) }
    func records(in states: Set<ResourceState>) async throws -> [ResourceRecord] { try await inner.records(in: states) }
    func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws {
        try await inner.setState(state, for: keys, failureReason: failureReason)
    }
    func remove(_ keys: [ResourceKey]) async throws { try await inner.remove(keys) }
    func recordAttempt(for keys: [ResourceKey]) async throws { try await inner.recordAttempt(for: keys) }
    func recordRehash(for key: ResourceKey) async throws { try await inner.recordRehash(for: key) }
    func counts() async throws -> [ResourceState: Int] { try await inner.counts() }
}

private actor CountingVersions: IngestVersions {
    private var storage: [ResourceKey: String] = [:]
    private(set) var writes = 0

    func version(for key: ResourceKey) -> String? { storage[key] }
    func setVersion(_ version: String?, for key: ResourceKey) {
        writes += 1
        storage[key] = version
    }
}

private struct CancellingVersions: IngestVersions {
    let inner: InMemoryIngestVersions
    func version(for key: ResourceKey) async -> String? {
        let stored = await inner.version(for: key)
        withUnsafeCurrentTask { $0?.cancel() }
        return stored
    }
    func setVersion(_ version: String?, for key: ResourceKey) async throws { try await inner.setVersion(version, for: key) }
}

private struct CancellingCloudExporter: ResourceExporter {
    func export(_ resource: PhotoResource, to destination: URL) async throws -> ExportOutcome {
        withUnsafeCurrentTask { $0?.cancel() }
        return .notLocal
    }
}

private struct RemovalRefusingStore: ResourceStore {
    struct Refused: Error {}
    let inner: InMemoryResourceStore
    func upsert(_ resources: [LocalResource]) async throws { try await inner.upsert(resources) }
    func record(for key: ResourceKey) async throws -> ResourceRecord? { try await inner.record(for: key) }
    func records(in states: Set<ResourceState>) async throws -> [ResourceRecord] { try await inner.records(in: states) }
    func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws {
        try await inner.setState(state, for: keys, failureReason: failureReason)
    }
    func remove(_ keys: [ResourceKey]) async throws { throw Refused() }
    func recordAttempt(for keys: [ResourceKey]) async throws { try await inner.recordAttempt(for: keys) }
    func recordRehash(for key: ResourceKey) async throws { try await inner.recordRehash(for: key) }
    func counts() async throws -> [ResourceState: Int] { try await inner.counts() }
}
