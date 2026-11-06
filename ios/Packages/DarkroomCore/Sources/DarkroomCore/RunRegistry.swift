import Foundation

public final class RunRegistry: @unchecked Sendable {
    private let lock = NSLock()
    private var stopped = false
    private var running: [UUID: Task<Void, Never>] = [:]

    public init() {}

    public var isStopped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopped
    }

    public func run<Value: Sendable>(_ work: @escaping @Sendable () async -> Value) async -> Value? {
        let box = ResultBox<Value>()
        guard let started = start({ box.set(await work()) }) else { return nil }
        await withTaskCancellationHandler {
            await started.task.value
        } onCancel: {
            started.task.cancel()
        }
        remove(started.id)
        return box.value
    }

    public func stop() async {
        let tasks = markStopped()
        for task in tasks { task.cancel() }
        for task in tasks { await task.value }
    }

    /// Creates the task and registers it under one lock, so `stop()` either sees it (and cancels and awaits it) or this
    /// returns nil and nothing runs.
    private func start(_ body: @escaping @Sendable () async -> Void) -> (id: UUID, task: Task<Void, Never>)? {
        lock.lock()
        defer { lock.unlock() }
        if stopped { return nil }
        let id = UUID()
        let task = Task { await body() }
        running[id] = task
        return (id, task)
    }

    private func remove(_ id: UUID) {
        lock.lock()
        running[id] = nil
        lock.unlock()
    }

    private func markStopped() -> [Task<Void, Never>] {
        lock.lock()
        defer { lock.unlock() }
        stopped = true
        let tasks = Array(running.values)
        running = [:]
        return tasks
    }
}

private final class ResultBox<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Value?

    func set(_ value: Value) {
        lock.lock()
        stored = value
        lock.unlock()
    }

    var value: Value? {
        lock.lock()
        defer { lock.unlock() }
        return stored
    }
}
