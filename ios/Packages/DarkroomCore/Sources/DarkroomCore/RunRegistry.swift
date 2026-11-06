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
        if isStopped { return nil }
        let id = UUID()
        let box = ResultBox<Value>()
        let task = Task {
            box.set(await work())
        }
        guard add(task, id: id) else {
            task.cancel()
            return nil
        }
        await withTaskCancellationHandler {
            await task.value
        } onCancel: {
            task.cancel()
        }
        remove(id)
        return box.value
    }

    public func stop() async {
        let tasks = markStopped()
        for task in tasks { task.cancel() }
        for task in tasks { await task.value }
    }

    private func add(_ task: Task<Void, Never>, id: UUID) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if stopped { return false }
        running[id] = task
        return true
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
