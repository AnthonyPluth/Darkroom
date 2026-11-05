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

    private func whole(_ resources: [LocalResource]) -> CheckRequest {
        UploadPlanner.checkBatches(from: resources, batchSize: Int.max).first ?? CheckRequest(assets: [])
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

    func testAnAssetWithANewResourceIsCheckedWithItsWholeCurrentSet() {
        let resources = [
            resource("a", .photo, state: .uploaded), resource("a", .liveVideo, state: .uploaded), resource("a", .adjusted),
            resource("b", .photo, state: .uploaded), resource("c", .photo, state: .notLocal), resource("c", .liveVideo),
        ]
        let batches = UploadPlanner.checkBatches(from: resources)
        XCTAssertEqual(batches.flatMap { $0.assets.map(\.id) }, ["a", "c"])
        XCTAssertEqual(batches[0].assets[0].resources.map(\.kind), [.photo, .liveVideo, .adjusted])
        XCTAssertEqual(batches[0].assets[1].resources.map(\.kind), [.liveVideo])
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
        let plan = UploadPlanner.plan(resources: resources, checked: whole(resources), reply: reply)
        XCTAssertEqual(plan.queue, [ResourceKey(assetID: "a", kind: .liveVideo)])
        XCTAssertEqual(plan.markUploaded, [ResourceKey(assetID: "a", kind: .photo), ResourceKey(assetID: "b", kind: .photo)])
    }

    func testQueueFollowsAssetOrderThenKind() {
        let resources = [
            resource("b", .adjusted), resource("b", .photo),
            resource("a", .liveVideo), resource("a", .photo),
        ]
        let reply = CheckReply(needed: resources.map { NeededResource(id: $0.key.assetID, kind: $0.key.kind) })
        let plan = UploadPlanner.plan(resources: resources, checked: whole(resources), reply: reply)
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
        let plan = UploadPlanner.plan(resources: resources, checked: whole(resources), reply: reply)
        XCTAssertTrue(plan.queue.isEmpty)
        XCTAssertTrue(plan.markUploaded.isEmpty)
    }

    func testNeededEntryForAnUnknownResourceIsIgnored() {
        let resources = [resource("a", .photo)]
        let reply = CheckReply(needed: [NeededResource(id: "zzz", kind: .photo)])
        let plan = UploadPlanner.plan(resources: resources, checked: whole(resources), reply: reply)
        XCTAssertTrue(plan.queue.isEmpty)
        XCTAssertEqual(plan.markUploaded, [ResourceKey(assetID: "a", kind: .photo)])
    }

    func testReplyToOneBatchNeverMarksLaterBatchesUploaded() {
        let resources = (0..<450).map { resource("asset-\($0)", .photo) }
        let batches = UploadPlanner.checkBatches(from: resources)
        XCTAssertEqual(batches.count, 3)
        let firstReply = CheckReply(needed: [NeededResource(id: "asset-5", kind: .photo)])
        let plan = UploadPlanner.plan(resources: resources, checked: batches[0], reply: firstReply)
        XCTAssertEqual(plan.queue, [ResourceKey(assetID: "asset-5", kind: .photo)])
        XCTAssertEqual(plan.markUploaded.count, 199)
        let firstBatchIDs = Set(batches[0].assets.map(\.id))
        XCTAssertTrue(plan.markUploaded.allSatisfy { firstBatchIDs.contains($0.assetID) })
    }

    func testResourceRehashedAfterTheRequestIsLeftForTheNextCheck() {
        let original = [resource("a", .photo), resource("b", .photo)]
        let batch = whole(original)
        var changed = original
        changed[0] = LocalResource(key: original[0].key, sha1: "new-bytes", bytes: 99, filename: "a.photo", state: .hashed)
        let plan = UploadPlanner.plan(resources: changed, checked: batch, reply: CheckReply(needed: []))
        XCTAssertEqual(plan.markUploaded, [ResourceKey(assetID: "b", kind: .photo)])
        XCTAssertTrue(plan.queue.isEmpty)
    }

    func testResourceWithChangedFilenameOrSizeIsNotTreatedAsChecked() {
        let original = [resource("a", .photo), resource("b", .photo)]
        let batch = whole(original)
        var changed = original
        changed[0] = LocalResource(key: original[0].key, sha1: original[0].sha1, bytes: 11, filename: "a.photo", state: .hashed)
        changed[1] = LocalResource(key: original[1].key, sha1: original[1].sha1, bytes: 10, filename: "renamed", state: .hashed)
        let plan = UploadPlanner.plan(resources: changed, checked: batch, reply: CheckReply(needed: [NeededResource(id: "a", kind: .photo)]))
        XCTAssertTrue(plan.queue.isEmpty)
        XCTAssertTrue(plan.markUploaded.isEmpty)
    }
}
