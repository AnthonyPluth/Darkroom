import DarkroomCore
import Foundation

struct KeychainSessionStorage: SessionStorage {
    let keychain: Keychain

    init(keychain: Keychain = Keychain()) {
        self.keychain = keychain
    }

    func load(_ item: SessionItem) throws -> String? {
        try keychain.load(account: item.rawValue)
    }

    func save(_ value: String, as item: SessionItem) throws {
        try keychain.save(value, account: item.rawValue)
    }

    func delete(_ item: SessionItem) throws {
        try keychain.delete(account: item.rawValue)
    }
}
