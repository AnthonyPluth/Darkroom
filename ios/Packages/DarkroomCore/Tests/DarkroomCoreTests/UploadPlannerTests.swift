import XCTest
@testable import DarkroomCore

final class UploadPlannerTests: XCTestCase {
    private func resource(_ asset: String, _ kind: ResourceKind, state: ResourceState = .hashed) -> LocalResource {
        LocalResource(
            key: ResourceKey(assetID: asset, kind: kind),
            sha1: "\(asset)-\(kind.rawValue)",
            bytes: 10,
            filename: "\(asset).\(kind.rawValue)",
            state: state
        )
    }

    func testBatchesHoldWholeAssetsInOrder() {
        let resources = [
            resource("a", .adjusted), resource("a", .photo),
            resource("b", .photo), resource("c", .photo), resource("c", .liveVideo),
        ]
        let batches = UploadPlanner.checkBatches(from: resources, batchSize: 2)
        XCTAssertEqual(batches.map { $0.assets.map(\.id) }, [["a", "b"], ["c"]])
        XCTAssertEqual(batches[0].assets[0].resources.map(\.kind), [.photo, .adjusted])
    }

    func testBatchesSkipResourcesAlreadyChecked() {
        let resources = [resource("a", .photo, state: .uploaded), resource("b", .photo), resource("c", .photo, state: .notLocal)]
        let batches = UploadPlanner.checkBatches(from: resources)
        XCTAssertEqual(batches.flatMap { $0.assets.map(\.id) }, ["b"])
    }

    func testBatchSizeIsTwoHundredByDefault() {
        let resources = (0..<450).map { resource("asset-\($0)", .photo) }
        let batches = UploadPlanner.checkBatches(from: resources)
        XCTAssertEqual(batches.map { $0.assets.count }, [200, 200, 50])
    }

    func testNoResourcesMakeNoBatches() {
        XCTAssertTrue(UploadPlanner.checkBatches(from: []).isEmpty)
    }

    func testNeededResourcesAreQueuedAndTheRestMarkedUploaded() {
        let resources = [resource("a", .photo), resource("a", .liveVideo), resource("b", .photo)]
        let reply = CheckReply(needed: [NeededResource(id: "a", kind: .liveVideo)])
        let plan = UploadPlanner.plan(resources: resources, reply: reply)
        XCTAssertEqual(plan.queue, [ResourceKey(assetID: "a", kind: .liveVideo)])
        XCTAssertEqual(plan.markUploaded, [ResourceKey(assetID: "a", kind: .photo), ResourceKey(assetID: "b", kind: .photo)])
    }

    func testQueueFollowsAssetOrderThenKind() {
        let resources = [
            resource("b", .adjusted), resource("b", .photo),
            resource("a", .liveVideo), resource("a", .photo),
        ]
        let reply = CheckReply(needed: resources.map { NeededResource(id: $0.key.assetID, kind: $0.key.kind) })
        let plan = UploadPlanner.plan(resources: resources, reply: reply)
        XCTAssertEqual(plan.queue.map { "\($0.assetID).\($0.kind.rawValue)" }, ["b.photo", "b.adjusted", "a.photo", "a.live_video"])
        XCTAssertTrue(plan.markUploaded.isEmpty)
    }

    func testResourcesNotInHashedStateAreNeverPlanned() {
        let resources = [
            resource("a", .photo, state: .notLocal),
            resource("b", .photo, state: .failed),
            resource("c", .photo, state: .uploading),
            resource("d", .photo, state: .uploaded),
        ]
        let reply = CheckReply(needed: resources.map { NeededResource(id: $0.key.assetID, kind: $0.key.kind) })
        let plan = UploadPlanner.plan(resources: resources, reply: reply)
        XCTAssertTrue(plan.queue.isEmpty)
        XCTAssertTrue(plan.markUploaded.isEmpty)
    }

    func testNeededEntryForAnUnknownResourceIsIgnored() {
        let resources = [resource("a", .photo)]
        let reply = CheckReply(needed: [NeededResource(id: "zzz", kind: .photo)])
        let plan = UploadPlanner.plan(resources: resources, reply: reply)
        XCTAssertTrue(plan.queue.isEmpty)
        XCTAssertEqual(plan.markUploaded, [ResourceKey(assetID: "a", kind: .photo)])
    }
}
