import XCTest
@testable import DarkroomCore

private actor Gate {
    private var open = false
    private var waiting: [CheckedContinuation<Void, Never>] = []
    private(set) var started = 0
    private(set) var cancelled = 0

    func pass() async {
        started += 1
        if open { return }
        await withTaskCancellationHandler {
            await withCheckedContinuation { waiting.append($0) }
        } onCancel: {
            Task { await self.noteCancel() }
        }
    }

    func release() {
        open = true
        for continuation in waiting { continuation.resume() }
        waiting = []
    }

    private func noteCancel() {
        cancelled += 1
        release()
    }
}

final class RunRegistryTests: XCTestCase {
    func testARunReturnsItsWork() async {
        let registry = RunRegistry()
        let value = await registry.run { 7 }
        XCTAssertEqual(value, 7)
        XCTAssertFalse(registry.isStopped)
    }

    func testStopCancelsRunningWorkAndWaitsForIt() async {
        let registry = RunRegistry()
        let gate = Gate()
        let running = Task { await registry.run { await gate.pass(); return "done" } }
        for _ in 0..<10_000 {
            if await gate.started == 1 { break }
            await Task.yield()
        }
        await registry.stop()
        let cancelled = await gate.cancelled
        XCTAssertEqual(cancelled, 1)
        let value = await running.value
        XCTAssertEqual(value, "done")
        XCTAssertTrue(registry.isStopped)
    }

    func testNothingRunsAfterStop() async {
        let registry = RunRegistry()
        await registry.stop()
        let ran = Counter()
        let value = await registry.run { ran.bump(); return "done" }
        XCTAssertNil(value)
        XCTAssertEqual(ran.count, 0)
    }

    func testCancellingTheCallerCancelsTheWork() async {
        let registry = RunRegistry()
        let gate = Gate()
        let running = Task { await registry.run { await gate.pass(); return 1 } }
        for _ in 0..<10_000 {
            if await gate.started == 1 { break }
            await Task.yield()
        }
        running.cancel()
        _ = await running.value
        let cancelled = await gate.cancelled
        XCTAssertEqual(cancelled, 1)
    }
}

private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func bump() {
        lock.lock()
        value += 1
        lock.unlock()
    }
}
