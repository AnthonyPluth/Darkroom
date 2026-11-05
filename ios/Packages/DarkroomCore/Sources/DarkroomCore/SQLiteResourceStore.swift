#if canImport(SQLite3)
import Foundation
import SQLite3

public actor SQLiteResourceStore: ResourceStore {
    public static let schemaVersion = 1

    private static let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    private static let columns = "asset_id, kind, sha1, bytes, filename, state, failure_reason, attempts, rehashes"

    private let db: OpaquePointer

    public init(path: String) throws {
        var handle: OpaquePointer?
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
        guard sqlite3_open_v2(path, &handle, flags, nil) == SQLITE_OK, let opened = handle else {
            let message = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "out of memory"
            sqlite3_close(handle)
            throw ResourceStoreError.database(message)
        }
        do {
            try Self.prepareSchema(opened)
        } catch {
            sqlite3_close(opened)
            throw error
        }
        db = opened
    }

    deinit {
        sqlite3_close(db)
    }

    public func upsert(_ resources: [LocalResource]) async throws {
        let sql = """
        INSERT INTO resources (asset_id, kind, sha1, bytes, filename, state)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id, kind) DO UPDATE SET
            sha1 = excluded.sha1, bytes = excluded.bytes, filename = excluded.filename, state = excluded.state,
            failure_reason = NULL, attempts = 0, rehashes = 0
        """
        try transaction {
            try withStatement(sql) { statement in
                for resource in resources {
                    sqlite3_reset(statement)
                    sqlite3_clear_bindings(statement)
                    bind(resource.key.assetID, at: 1, in: statement)
                    bind(resource.key.kind.rawValue, at: 2, in: statement)
                    bind(resource.sha1, at: 3, in: statement)
                    sqlite3_bind_int64(statement, 4, resource.bytes)
                    bind(resource.filename, at: 5, in: statement)
                    bind(resource.state.rawValue, at: 6, in: statement)
                    _ = try step(statement)
                }
            }
        }
    }

    public func record(for key: ResourceKey) async throws -> ResourceRecord? {
        let sql = "SELECT \(Self.columns) FROM resources WHERE asset_id = ? AND kind = ?"
        return try withStatement(sql) { statement in
            bind(key.assetID, at: 1, in: statement)
            bind(key.kind.rawValue, at: 2, in: statement)
            return try step(statement) ? Self.record(from: statement) : nil
        }
    }

    public func records(in states: Set<ResourceState>) async throws -> [ResourceRecord] {
        guard !states.isEmpty else { return [] }
        let names = states.map(\.rawValue).sorted()
        let marks = Array(repeating: "?", count: names.count).joined(separator: ", ")
        let sql = "SELECT \(Self.columns) FROM resources WHERE state IN (\(marks)) ORDER BY rowid"
        return try withStatement(sql) { statement in
            for (index, name) in names.enumerated() { bind(name, at: Int32(index + 1), in: statement) }
            var found: [ResourceRecord] = []
            while try step(statement) {
                if let record = Self.record(from: statement) { found.append(record) }
            }
            return found
        }
    }

    public func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws {
        try transaction {
            for key in keys {
                guard let current = try currentState(of: key) else { continue }
                guard current.canMove(to: state) else {
                    throw ResourceStoreError.illegalTransition(key: key, from: current, to: state)
                }
                try withStatement("UPDATE resources SET state = ?, failure_reason = ? WHERE asset_id = ? AND kind = ?") { statement in
                    bind(state.rawValue, at: 1, in: statement)
                    if state == .failed, let failureReason {
                        bind(failureReason, at: 2, in: statement)
                    } else {
                        sqlite3_bind_null(statement, 2)
                    }
                    bind(key.assetID, at: 3, in: statement)
                    bind(key.kind.rawValue, at: 4, in: statement)
                    _ = try step(statement)
                }
            }
        }
    }

    @discardableResult
    public func setState(_ state: ResourceState, ifUnchanged resources: [LocalResource], failureReason: String?) async throws -> Int {
        var current: [ResourceKey] = []
        for resource in resources {
            let sql = "SELECT \(Self.columns) FROM resources WHERE asset_id = ? AND kind = ?"
            let stored: ResourceRecord? = try withStatement(sql) { statement in
                bind(resource.key.assetID, at: 1, in: statement)
                bind(resource.key.kind.rawValue, at: 2, in: statement)
                return try step(statement) ? Self.record(from: statement) : nil
            }
            if let stored, stored.resource.hasSameContent(as: resource), stored.resource.state.canMove(to: state) {
                current.append(resource.key)
            }
        }
        try await setState(state, for: current, failureReason: failureReason)
        return current.count
    }

    @discardableResult
    public func replace(_ old: LocalResource, with fresh: LocalResource) async throws -> Bool {
        guard let stored = try await record(for: old.key), stored.resource.hasSameContent(as: old) else { return false }
        try await upsert([fresh])
        return true
    }

    public func remove(_ keys: [ResourceKey]) async throws {
        try transaction {
            try withStatement("DELETE FROM resources WHERE asset_id = ? AND kind = ?") { statement in
                for key in keys {
                    sqlite3_reset(statement)
                    sqlite3_clear_bindings(statement)
                    bind(key.assetID, at: 1, in: statement)
                    bind(key.kind.rawValue, at: 2, in: statement)
                    _ = try step(statement)
                }
            }
        }
    }

    public func recordAttempt(for keys: [ResourceKey]) async throws {
        try transaction {
            for key in keys { try increment("attempts", for: key) }
        }
    }

    public func recordRehash(for key: ResourceKey) async throws {
        try increment("rehashes", for: key)
    }

    public func counts() async throws -> [ResourceState: Int] {
        try withStatement("SELECT state, COUNT(*) FROM resources GROUP BY state") { statement in
            var found: [ResourceState: Int] = [:]
            while try step(statement) {
                if let state = ResourceState(rawValue: Self.text(statement, 0)) {
                    found[state] = Int(sqlite3_column_int64(statement, 1))
                }
            }
            return found
        }
    }

    private func increment(_ column: String, for key: ResourceKey) throws {
        try withStatement("UPDATE resources SET \(column) = \(column) + 1 WHERE asset_id = ? AND kind = ?") { statement in
            bind(key.assetID, at: 1, in: statement)
            bind(key.kind.rawValue, at: 2, in: statement)
            _ = try step(statement)
        }
    }

    private func currentState(of key: ResourceKey) throws -> ResourceState? {
        try withStatement("SELECT state FROM resources WHERE asset_id = ? AND kind = ?") { statement in
            bind(key.assetID, at: 1, in: statement)
            bind(key.kind.rawValue, at: 2, in: statement)
            return try step(statement) ? ResourceState(rawValue: Self.text(statement, 0)) : nil
        }
    }

    private func transaction<T>(_ body: () throws -> T) throws -> T {
        try Self.exec(db, "BEGIN IMMEDIATE")
        do {
            let value = try body()
            try Self.exec(db, "COMMIT")
            return value
        } catch {
            try? Self.exec(db, "ROLLBACK")
            throw error
        }
    }

    private func withStatement<T>(_ sql: String, _ body: (OpaquePointer) throws -> T) throws -> T {
        var prepared: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &prepared, nil) == SQLITE_OK, let statement = prepared else {
            sqlite3_finalize(prepared)
            throw failure()
        }
        defer { sqlite3_finalize(statement) }
        return try body(statement)
    }

    private func step(_ statement: OpaquePointer) throws -> Bool {
        switch sqlite3_step(statement) {
        case SQLITE_ROW: return true
        case SQLITE_DONE: return false
        default: throw failure()
        }
    }

    private func bind(_ value: String, at index: Int32, in statement: OpaquePointer) {
        sqlite3_bind_text(statement, index, value, -1, Self.transient)
    }

    private func failure() -> ResourceStoreError {
        ResourceStoreError.database(String(cString: sqlite3_errmsg(db)))
    }

    private static func text(_ statement: OpaquePointer, _ column: Int32) -> String {
        sqlite3_column_text(statement, column).map { String(cString: $0) } ?? ""
    }

    private static func record(from statement: OpaquePointer) -> ResourceRecord? {
        guard let kind = ResourceKind(rawValue: text(statement, 1)),
              let state = ResourceState(rawValue: text(statement, 5))
        else { return nil }
        let resource = LocalResource(
            key: ResourceKey(assetID: text(statement, 0), kind: kind),
            sha1: text(statement, 2),
            bytes: sqlite3_column_int64(statement, 3),
            filename: text(statement, 4),
            state: state
        )
        let reason = sqlite3_column_type(statement, 6) == SQLITE_NULL ? nil : text(statement, 6)
        return ResourceRecord(
            resource: resource,
            failureReason: reason,
            attempts: Int(sqlite3_column_int64(statement, 7)),
            rehashes: Int(sqlite3_column_int64(statement, 8))
        )
    }

    private static func exec(_ db: OpaquePointer, _ sql: String) throws {
        var message: UnsafeMutablePointer<CChar>?
        guard sqlite3_exec(db, sql, nil, nil, &message) == SQLITE_OK else {
            let text = message.map { String(cString: $0) } ?? "unknown error"
            sqlite3_free(message)
            throw ResourceStoreError.database(text)
        }
    }

    private static func userVersion(_ db: OpaquePointer) throws -> Int {
        var prepared: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA user_version", -1, &prepared, nil) == SQLITE_OK, let statement = prepared else {
            sqlite3_finalize(prepared)
            throw ResourceStoreError.database(String(cString: sqlite3_errmsg(db)))
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_step(statement) == SQLITE_ROW else {
            throw ResourceStoreError.database(String(cString: sqlite3_errmsg(db)))
        }
        return Int(sqlite3_column_int(statement, 0))
    }

    private static func prepareSchema(_ db: OpaquePointer) throws {
        try exec(db, "PRAGMA busy_timeout = 5000")
        try exec(db, "PRAGMA journal_mode = WAL")
        let version = try userVersion(db)
        guard version <= schemaVersion else {
            throw ResourceStoreError.database("the resource database was written by a newer version of the app")
        }
        guard version < 1 else { return }
        try exec(db, """
        BEGIN;
        CREATE TABLE resources (
            asset_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            sha1 TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            filename TEXT NOT NULL,
            state TEXT NOT NULL,
            failure_reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            rehashes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (asset_id, kind)
        );
        CREATE INDEX resources_by_state ON resources (state);
        PRAGMA user_version = 1;
        COMMIT;
        """)
    }
}
#endif
