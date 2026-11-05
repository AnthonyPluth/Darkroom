import Foundation
import XCTest
@testable import DarkroomCore

final class CleanupFlowTests: XCTestCase {
    private let listedAt = Date(timeIntervalSince1970: 1_000_000)

    private func asset(_ id: String, sha1: String? = nil, video: Bool = false) -> AssetDescriptor {
        var resources = [ResourceDescriptor(kind: .photo, sha1: sha1 ?? "sha-\(id)", bytes: 10, filename: "\(id).HEIC")]
        if video {
            resources.append(ResourceDescriptor(kind: .liveVideo, sha1: "video-\(id)", bytes: 5, filename: "\(id).MOV"))
        }
        return AssetDescriptor(id: id, resources: resources)
    }

    private func reply(_ ids: [String], enabled: Bool = true) -> EligibleReply {
        EligibleReply(cleanupEnabled: enabled, eligible: ids.map { EligibleAsset(assetID: $0, bytes: Int64($0.count) * 100) })
    }

    private func refusal(_ id: String, _ reason: String = "file_missing") -> RejectedAsset {
        RejectedAsset(assetID: id, reason: reason, message: "Refused: \(reason)")
    }

    private func fetched(_ ids: [String], asked: [AssetDescriptor]? = nil) throws -> CleanupFlow {
        var flow = CleanupFlow()
        XCTAssertNil(try flow.handle(.listed(reply(ids), asked: asked ?? ids.map { asset($0) }, at: listedAt)))
        XCTAssertEqual(flow.phase, .fetched)
        return flow
    }

    private func deleting(_ ids: [String]) throws -> CleanupFlow {
        var flow = try fetched(ids)
        _ = try flow.handle(.remove(shown: ids, current: ids.map { asset($0) }, at: listedAt))
        XCTAssertEqual(try flow.handle(.confirmed(ConfirmReply(confirmed: ids, rejected: []))), .delete(assetIDs: ids))
        return flow
    }

    private func report(of flow: CleanupFlow) -> CleanupReport? {
        switch flow.state {
        case let .finished(report), let .deleting(_, report), let .reporting(_, report), let .confirming(_, _, report):
            return report
        case .idle, .fetched:
            return nil
        }
    }

    func testTheListKeepsTheAskedDescriptorsInTheServersOrder() throws {
        let asked = [asset("a", video: true), asset("b"), asset("c")]
        var flow = CleanupFlow()
        let command = try flow.handle(.listed(reply(["c", "a", "zz", "c"]), asked: asked, at: listedAt))
        XCTAssertNil(command)
        guard case let .fetched(list) = flow.state else { return XCTFail("not fetched: \(flow.state)") }
        XCTAssertEqual(list.ids, ["c", "a"])
        XCTAssertEqual(list.entries.map(\.asset), [asked[2], asked[0]])
        XCTAssertEqual(list.totalBytes, 200)
        XCTAssertEqual(list.generatedAt, listedAt)
        XCTAssertEqual(flow.cleanupEnabled, true)
    }

    func testCleanupOffOnTheServerLeavesTheFlowIdleWhateverTheListSays() throws {
        var flow = CleanupFlow()
        XCTAssertNil(try flow.handle(.listed(reply(["a"], enabled: false), asked: [asset("a")], at: listedAt)))
        XCTAssertEqual(flow.state, .idle)
        XCTAssertEqual(flow.cleanupEnabled, false)
        XCTAssertThrowsError(try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: listedAt)))
    }

    func testCleanupTurnedOffReplacesAFetchedList() throws {
        var flow = try fetched(["a"])
        XCTAssertNil(try flow.handle(.listed(reply(["a"], enabled: false), asked: [asset("a")], at: listedAt)))
        XCTAssertEqual(flow.state, .idle)
        XCTAssertEqual(flow.cleanupEnabled, false)
    }

    func testAnEmptyListStaysIdle() throws {
        var flow = CleanupFlow()
        XCTAssertNil(try flow.handle(.listed(reply([]), asked: [asset("a")], at: listedAt)))
        XCTAssertEqual(flow.state, .idle)
        XCTAssertEqual(flow.cleanupEnabled, true)
    }

    func testABatchHoldsAtMostFiveHundredAssets() throws {
        let ids = (0..<(EligibleList.maxAssets + 20)).map { "id\($0)" }
        let flow = try fetched(ids)
        guard case let .fetched(list) = flow.state else { return XCTFail("not fetched") }
        XCTAssertEqual(list.ids, Array(ids.prefix(EligibleList.maxAssets)))
    }

    func testAListAnHourOldIsRefetchedBeforeAnythingIsConfirmed() throws {
        for age in [EligibleList.maxAge, EligibleList.maxAge + 1, 86_400, -1] {
            var flow = try fetched(["a"])
            let command = try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: listedAt.addingTimeInterval(age)))
            XCTAssertEqual(command, .fetchEligible, "age \(age)")
            XCTAssertEqual(flow.state, .idle, "age \(age)")
            XCTAssertThrowsError(try flow.handle(.confirmed(ConfirmReply(confirmed: ["a"], rejected: []))))
        }
    }

    func testAListUnderAnHourOldIsConfirmed() throws {
        var flow = try fetched(["a"])
        let command = try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: listedAt.addingTimeInterval(EligibleList.maxAge - 1)))
        XCTAssertEqual(command, .confirm(ConfirmRequest(assets: [asset("a")])))
        XCTAssertEqual(flow.phase, .confirming)
    }

    func testARefetchedListStartsTheHourAgain() throws {
        var flow = try fetched(["a"])
        let later = listedAt.addingTimeInterval(2 * EligibleList.maxAge)
        XCTAssertEqual(try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: later)), .fetchEligible)
        _ = try flow.handle(.listed(reply(["a"]), asked: [asset("a")], at: later))
        XCTAssertEqual(try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: later)), .confirm(ConfirmRequest(assets: [asset("a")])))
    }

    func testConfirmSendsOnlyTheShownIdsOfTheList() throws {
        var flow = try fetched(["a", "b", "c"])
        let command = try flow.handle(.remove(shown: ["c", "a", "x", "a"], current: ["a", "b", "c", "x"].map { asset($0) }, at: listedAt))
        XCTAssertEqual(command, .confirm(ConfirmRequest(assets: [asset("a"), asset("c")])))
        guard case let .confirming(_, sent, _) = flow.state else { return XCTFail("not confirming") }
        XCTAssertEqual(sent, ["a", "c"])
    }

    func testAShownPhotoThatChangedOrIsGoneIsNotSent() throws {
        var flow = try fetched(["a", "b", "c", "d"], asked: [asset("a"), asset("b"), asset("c"), asset("d", video: true)])
        let current = [asset("a"), asset("b", sha1: "edited"), asset("d")]
        let command = try flow.handle(.remove(shown: ["a", "b", "c", "d"], current: current, at: listedAt))
        XCTAssertEqual(command, .confirm(ConfirmRequest(assets: [asset("a")])))
        XCTAssertEqual(report(of: flow)?.changed, ["b", "c", "d"])
    }

    func testTheResourceOrderOnThePhoneDoesNotCountAsAChange() throws {
        let listed = asset("a", video: true)
        var flow = try fetched(["a"], asked: [listed])
        let reordered = AssetDescriptor(id: "a", resources: listed.resources.reversed())
        XCTAssertEqual(try flow.handle(.remove(shown: ["a"], current: [reordered], at: listedAt)), .confirm(ConfirmRequest(assets: [listed])))
    }

    func testWhenEveryShownPhotoChangedNothingIsConfirmed() throws {
        var flow = try fetched(["a"])
        XCTAssertNil(try flow.handle(.remove(shown: ["a"], current: [asset("a", sha1: "new")], at: listedAt)))
        var expected = CleanupReport()
        expected.changed = ["a"]
        XCTAssertEqual(flow.state, .finished(expected))
    }

    func testShowingNothingFromTheListDoesNothing() throws {
        var flow = try fetched(["a"])
        let before = flow
        XCTAssertNil(try flow.handle(.remove(shown: [], current: [asset("a")], at: listedAt)))
        XCTAssertNil(try flow.handle(.remove(shown: ["x"], current: [asset("x")], at: listedAt)))
        XCTAssertEqual(flow, before)
    }

    func testOnlyConfirmedIdsAreDeleted() throws {
        var flow = try fetched(["a", "b", "c"])
        _ = try flow.handle(.remove(shown: ["a", "b", "c"], current: ["a", "b", "c"].map { asset($0) }, at: listedAt))
        let reply = ConfirmReply(confirmed: ["zz", "a", "a"], rejected: [refusal("b"), refusal("zz")])
        XCTAssertEqual(try flow.handle(.confirmed(reply)), .delete(assetIDs: ["a"]))
        XCTAssertEqual(report(of: flow)?.refused, [
            refusal("b"),
            RejectedAsset(assetID: "c", reason: CleanupReport.notConfirmedReason, message: CleanupReport.notConfirmedMessage),
        ])
    }

    func testNothingConfirmedDeletesNothing() throws {
        var flow = try fetched(["a"])
        _ = try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: listedAt))
        XCTAssertNil(try flow.handle(.confirmed(ConfirmReply(confirmed: [], rejected: [refusal("a", "not_confirmed")]))))
        XCTAssertEqual(flow.phase, .finished)
        XCTAssertEqual(report(of: flow)?.refused, [refusal("a", "not_confirmed")])
    }

    func testAFailedConfirmGoesBackToTheListAndDeletesNothing() throws {
        var flow = try fetched(["a"])
        let listed = flow.state
        _ = try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: listedAt))
        XCTAssertNil(try flow.handle(.confirmFailed))
        XCTAssertEqual(flow.state, listed)
        XCTAssertThrowsError(try flow.handle(.deletionEnded(removed: ["a"])))
        let late = listedAt.addingTimeInterval(EligibleList.maxAge)
        XCTAssertEqual(try flow.handle(.remove(shown: ["a"], current: [asset("a")], at: late)), .fetchEligible)
    }

    func testCancellingTheSystemDialogPostsNothing() throws {
        var flow = try deleting(["a", "b"])
        XCTAssertNil(try flow.handle(.deletionEnded(removed: [])))
        XCTAssertEqual(flow.phase, .finished)
        XCTAssertEqual(report(of: flow)?.removed, [])
        XCTAssertEqual(report(of: flow)?.notRemoved, ["a", "b"])
        XCTAssertThrowsError(try flow.handle(.retryReport))
        XCTAssertThrowsError(try flow.handle(.reported(DeletedReply(recorded: ["a"], rejected: []))))
    }

    func testAPartialDeletionReportsOnlyWhatIOSRemoved() throws {
        var flow = try deleting(["a", "b", "c"])
        let command = try flow.handle(.deletionEnded(removed: ["c", "x", "a"]))
        XCTAssertEqual(command, .report(DeletedRequest(assetIDs: ["a", "c"])))
        XCTAssertEqual(report(of: flow)?.removed, ["a", "c"])
        XCTAssertEqual(report(of: flow)?.notRemoved, ["b"])
    }

    func testAConfirmedPhotoThatChangedBeforeTheDeletionIsChangedNotNotRemoved() throws {
        var flow = try deleting(["a", "b", "c"])
        let command = try flow.handle(.deletionEnded(removed: ["a"], changed: ["b", "x"]))
        XCTAssertEqual(command, .report(DeletedRequest(assetIDs: ["a"])))
        XCTAssertEqual(report(of: flow)?.changed, ["b"])
        XCTAssertEqual(report(of: flow)?.removed, ["a"])
        XCTAssertEqual(report(of: flow)?.notRemoved, ["c"])
    }

    func testRemovalsOfIdsThatWereNeverConfirmedAreNotReported() throws {
        var flow = try deleting(["a"])
        XCTAssertNil(try flow.handle(.deletionEnded(removed: ["b", "c"])))
        XCTAssertEqual(report(of: flow)?.removed, [])
        XCTAssertEqual(flow.phase, .finished)
    }

    func testTheServersAnswerToTheReportFinishesTheRound() throws {
        var flow = try deleting(["a", "b"])
        _ = try flow.handle(.deletionEnded(removed: ["a", "b"]))
        XCTAssertNil(try flow.handle(.reported(DeletedReply(recorded: ["a", "q"], rejected: [refusal("b", "already_deleted"), refusal("q")]))))
        let finished = try XCTUnwrap(report(of: flow))
        XCTAssertEqual(flow.phase, .finished)
        XCTAssertEqual(finished.removed, ["a", "b"])
        XCTAssertEqual(finished.recorded, ["a"])
        XCTAssertEqual(finished.unrecorded, [refusal("b", "already_deleted")])
    }

    func testAFailedReportStaysPendingAndARetrySendsTheSameIds() throws {
        var flow = try deleting(["a", "b"])
        let first = try flow.handle(.deletionEnded(removed: ["b"]))
        XCTAssertNil(try flow.handle(.reportFailed))
        XCTAssertEqual(flow.phase, .reporting)
        XCTAssertEqual(try flow.handle(.retryReport), first)
        XCTAssertEqual(first, .report(DeletedRequest(assetIDs: ["b"])))
        for event: CleanupEvent in [.reset, .listFailed, .listed(reply(["a"]), asked: [asset("a")], at: listedAt)] {
            XCTAssertThrowsError(try flow.handle(event))
            XCTAssertEqual(flow.phase, .reporting)
        }
    }

    func testAFinishedRoundCanStartAgain() throws {
        var flow = try deleting(["a"])
        _ = try flow.handle(.deletionEnded(removed: ["a"]))
        _ = try flow.handle(.reported(DeletedReply(recorded: ["a"], rejected: [])))
        XCTAssertNil(try flow.handle(.listed(reply(["b"]), asked: [asset("b")], at: listedAt)))
        XCTAssertEqual(flow.phase, .fetched)
        XCTAssertNil(try flow.handle(.reset))
        XCTAssertEqual(flow.state, .idle)
    }

    func testNoPathReachesDeletingWithoutAConfirm() throws {
        var states: [CleanupFlow] = [CleanupFlow(), try fetched(["a"])]
        var finished = try fetched(["a"])
        _ = try finished.handle(.remove(shown: ["a"], current: [asset("a", sha1: "new")], at: listedAt))
        states.append(finished)
        var reporting = try deleting(["a"])
        _ = try reporting.handle(.deletionEnded(removed: ["a"]))
        states.append(reporting)
        var confirming = try fetched(["a"])
        _ = try confirming.handle(.remove(shown: ["a"], current: [asset("a")], at: listedAt))
        for var flow in states {
            let before = flow
            XCTAssertThrowsError(try flow.handle(.deletionEnded(removed: ["a"]))) {
                XCTAssertEqual($0 as? CleanupFlowError, .notNow(before.phase))
            }
            XCTAssertThrowsError(try flow.handle(.confirmed(ConfirmReply(confirmed: ["a"], rejected: []))))
            XCTAssertEqual(flow, before)
        }
        XCTAssertThrowsError(try confirming.handle(.deletionEnded(removed: ["a"])))
        XCTAssertEqual(confirming.phase, .confirming)
    }

    func testARoundInFlightCannotBeReplacedOrReset() throws {
        var confirming = try fetched(["a"])
        _ = try confirming.handle(.remove(shown: ["a"], current: [asset("a")], at: listedAt))
        let deleting = try deleting(["a"])
        for start in [confirming, deleting] {
            for event: CleanupEvent in [.reset, .listFailed, .listed(reply(["b"]), asked: [asset("b")], at: listedAt),
                                        .remove(shown: ["a"], current: [asset("a")], at: listedAt)] {
                var flow = start
                XCTAssertThrowsError(try flow.handle(event))
                XCTAssertEqual(flow, start)
            }
        }
    }

    func testRandomEventSequencesNeverDeleteOrReportMoreThanTheRulesAllow() throws {
        let ids = ["a", "b", "c", "d", "e"]
        var random = SplitMix(seed: 38)
        for _ in 0..<2000 {
            var flow = CleanupFlow()
            var clock = listedAt
            var shownAtRemove: Set<String> = []
            var listAtRemove: Set<String> = []
            var sentToConfirm: Set<String> = []
            var confirmedByServer: Set<String> = []
            var sentToDelete: Set<String> = []
            var removedByIOS: Set<String> = []
            for _ in 0..<12 {
                clock = clock.addingTimeInterval(Double(random.below(3)) * 1_800)
                let pick = { (count: Int) in Array(ids.shuffled(using: &random).prefix(count)) }
                let event: CleanupEvent
                switch random.below(9) {
                case 0:
                    event = .listed(reply(pick(random.below(6)), enabled: random.below(4) > 0), asked: pick(random.below(6)).map { asset($0) }, at: clock)
                case 1:
                    let current = pick(random.below(6)).map { random.below(5) == 0 ? asset($0, sha1: "edited") : asset($0) }
                    event = .remove(shown: pick(random.below(6)), current: current, at: clock)
                    if case let .fetched(list) = flow.state {
                        listAtRemove = Set(list.ids)
                        if case let .remove(shown, _, _) = event { shownAtRemove = Set(shown) }
                    }
                case 2:
                    let confirmed = pick(random.below(6))
                    event = .confirmed(ConfirmReply(confirmed: confirmed, rejected: pick(random.below(3)).map { refusal($0) }))
                    if case .confirming = flow.phase { confirmedByServer = Set(confirmed) }
                case 3:
                    let removed = pick(random.below(6))
                    event = .deletionEnded(removed: removed)
                    if case .deleting = flow.phase { removedByIOS = Set(removed) }
                case 4: event = .confirmFailed
                case 5: event = .reported(DeletedReply(recorded: pick(random.below(6)), rejected: []))
                case 6: event = .reportFailed
                case 7: event = .retryReport
                default: event = random.below(2) == 0 ? .reset : .listFailed
                }
                let before = flow
                let command: CleanupCommand?
                do {
                    command = try flow.handle(event)
                } catch {
                    XCTAssertEqual(flow, before, "a refused event changed the state")
                    continue
                }
                switch command {
                case let .confirm(request):
                    guard case let .fetched(list) = before.state else { return XCTFail("confirm outside fetched") }
                    XCTAssertFalse(list.isStale(at: clock))
                    sentToConfirm = Set(request.assets.map(\.id))
                    XCTAssertTrue(sentToConfirm.isSubset(of: shownAtRemove.intersection(listAtRemove)))
                    for sent in request.assets {
                        XCTAssertEqual(list.entries.first { $0.id == sent.id }?.asset, sent)
                    }
                case let .delete(assetIDs):
                    guard case .confirming = before.phase else { return XCTFail("delete outside confirming") }
                    sentToDelete = Set(assetIDs)
                    XCTAssertTrue(sentToDelete.isSubset(of: sentToConfirm.intersection(confirmedByServer)))
                case let .report(request):
                    if case .deleting = before.phase {
                        XCTAssertTrue(Set(request.assetIDs).isSubset(of: sentToDelete.intersection(removedByIOS)))
                    } else {
                        guard case let .reporting(pending, _) = before.state else { return XCTFail("report outside a round") }
                        XCTAssertEqual(request, pending)
                    }
                    XCTAssertFalse(request.assetIDs.isEmpty)
                case .fetchEligible:
                    guard case let .fetched(list) = before.state else { return XCTFail("refetch outside fetched") }
                    XCTAssertTrue(list.isStale(at: clock))
                case nil:
                    break
                }
            }
        }
    }
}

private struct SplitMix: RandomNumberGenerator {
    var state: UInt64

    init(seed: UInt64) {
        state = seed
    }

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var mixed = state
        mixed = (mixed ^ (mixed >> 30)) &* 0xBF58_476D_1CE4_E5B9
        mixed = (mixed ^ (mixed >> 27)) &* 0x94D0_49BB_1331_11EB
        return mixed ^ (mixed >> 31)
    }

    mutating func below(_ bound: Int) -> Int {
        Int(next() % UInt64(bound))
    }
}
