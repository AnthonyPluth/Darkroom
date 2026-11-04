import Foundation

struct BuildVersion: Equatable {
    let marketing: String
    let build: String

    init(infoDictionary: [String: Any]?) {
        marketing = infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown"
        build = infoDictionary?["CFBundleVersion"] as? String ?? "unknown"
    }

    static var current: BuildVersion {
        BuildVersion(infoDictionary: Bundle.main.infoDictionary)
    }

    var label: String {
        "\(marketing) (\(build))"
    }
}
