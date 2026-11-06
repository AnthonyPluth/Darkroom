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
        let printable = String.UnicodeScalarView(raw.unicodeScalars.compactMap(printableForm))
        let trimmed = String(printable).trimmingCharacters(in: .whitespaces)
        let capped = String.UnicodeScalarView(trimmed.unicodeScalars.prefix(maxDeviceNameLength))
        let result = String(capped).trimmingCharacters(in: .whitespaces)
        return result.isEmpty ? fallbackDeviceName : result
    }

    public static func callback(from url: URL) -> Callback? {
        guard url.scheme?.lowercased() == callbackScheme, url.host?.lowercased() == callbackHost else { return nil }
        guard url.path.isEmpty || url.path == "/" else { return nil }
        guard let fragment = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedFragment else { return nil }
        var foundToken: String?
        var foundState: String?
        for pair in fragment.split(separator: "&") {
            let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else { continue }
            let value = String(parts[1]).removingPercentEncoding ?? ""
            if parts[0] == "token", foundToken == nil { foundToken = value }
            if parts[0] == "state", foundState == nil { foundState = value }
        }
        guard let token = foundToken, let state = foundState, isUsableToken(token), isUsableToken(state) else { return nil }
        return Callback(token: token, state: state)
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
