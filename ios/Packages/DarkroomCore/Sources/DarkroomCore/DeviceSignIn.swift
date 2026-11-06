import Foundation

public enum DeviceSignIn {
    public static let callbackScheme = "darkroom"
    public static let callbackHost = "signed-in"
    public static let maxDeviceNameLength = 100
    public static let maxTokenLength = 1024
    public static let fallbackDeviceName = "iPhone"

    public struct Callback: Equatable, Sendable {
        public let token: String
        public let state: String
    }

    public struct Refusal: Equatable, Sendable {
        public enum Reason: String, Sendable {
            case noPerson = "no_person"
            case notAllowed = "not_allowed"
        }

        public let reason: Reason
        public let state: String
    }

    public static func newState() -> String {
        var generator = SystemRandomNumberGenerator()
        let bytes = (0..<32).map { _ in UInt8.random(in: .min ... .max, using: &generator) }
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    public static func startURL(server: ServerAddress, deviceName: String, state: String) -> URL? {
        guard var components = URLComponents(url: server.url, resolvingAgainstBaseURL: false) else { return nil }
        components.percentEncodedPath += "/auth/device/start"
        components.percentEncodedQuery = "name=" + percentEncoded(cleanedDeviceName(deviceName)) + "&state=" + percentEncoded(state)
        return components.url
    }

    public static func cleanedDeviceName(_ raw: String) -> String {
        let result = cleaned(raw)
        return result.isEmpty ? fallbackDeviceName : result
    }

    public static func hasName(_ raw: String) -> Bool {
        !cleaned(raw).isEmpty
    }

    private static func cleaned(_ raw: String) -> String {
        let printable = String.UnicodeScalarView(raw.unicodeScalars.compactMap(printableForm))
        let trimmed = String(printable).trimmingCharacters(in: .whitespaces)
        let capped = String.UnicodeScalarView(trimmed.unicodeScalars.prefix(maxDeviceNameLength))
        return String(capped).trimmingCharacters(in: .whitespaces)
    }

    public static func callback(from url: URL) -> Callback? {
        guard let values = fragmentValues(of: url) else { return nil }
        guard let token = values["token"], let state = values["state"], isUsableToken(token), isUsableToken(state) else { return nil }
        return Callback(token: token, state: state)
    }

    public static func refusal(from url: URL) -> Refusal? {
        guard let values = fragmentValues(of: url), values["token"] == nil else { return nil }
        guard let code = values["error"], let reason = Refusal.Reason(rawValue: code) else { return nil }
        guard let state = values["state"], isUsableToken(state) else { return nil }
        return Refusal(reason: reason, state: state)
    }

    private static func fragmentValues(of url: URL) -> [String: String]? {
        guard url.scheme?.lowercased() == callbackScheme, url.host?.lowercased() == callbackHost else { return nil }
        guard url.path.isEmpty || url.path == "/" else { return nil }
        guard let fragment = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedFragment else { return nil }
        var values: [String: String] = [:]
        for pair in fragment.split(separator: "&") {
            let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else { continue }
            let key = String(parts[0])
            if values[key] == nil { values[key] = String(parts[1]).removingPercentEncoding ?? "" }
        }
        return values
    }

    private static func isUsableToken(_ value: String) -> Bool {
        guard !value.isEmpty, value.unicodeScalars.count <= maxTokenLength else { return false }
        return value.unicodeScalars.allSatisfy { $0.value > 0x20 && $0.value < 0x7F }
    }

    private static func printableForm(of scalar: Unicode.Scalar) -> Unicode.Scalar? {
        if scalar == " " { return scalar }
        switch scalar.properties.generalCategory {
        case .spaceSeparator:
            return " "
        case .control, .format, .surrogate, .privateUse, .unassigned, .lineSeparator, .paragraphSeparator:
            return nil
        default:
            return scalar
        }
    }

    private static func percentEncoded(_ value: String) -> String {
        let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return value.addingPercentEncoding(withAllowedCharacters: unreserved) ?? ""
    }
}
