import Foundation

public enum DeviceSignIn {
    public static let callbackScheme = "darkroom"
    public static let callbackHost = "signed-in"
    public static let maxDeviceNameLength = 100
    public static let maxTokenLength = 1024
    public static let fallbackDeviceName = "iPhone"

    public static func startURL(server: ServerAddress, deviceName: String) -> URL? {
        guard var components = URLComponents(url: server.url, resolvingAgainstBaseURL: false) else { return nil }
        components.percentEncodedPath += "/auth/device/start"
        components.percentEncodedQuery = "name=" + percentEncoded(cleanedDeviceName(deviceName))
        return components.url
    }

    public static func cleanedDeviceName(_ raw: String) -> String {
        let printable = String.UnicodeScalarView(raw.unicodeScalars.compactMap(printableForm))
        let trimmed = String(printable).trimmingCharacters(in: .whitespaces)
        let capped = String.UnicodeScalarView(trimmed.unicodeScalars.prefix(maxDeviceNameLength))
        let result = String(capped).trimmingCharacters(in: .whitespaces)
        return result.isEmpty ? fallbackDeviceName : result
    }

    public static func token(fromCallback url: URL) -> String? {
        guard url.scheme?.lowercased() == callbackScheme, url.host?.lowercased() == callbackHost else { return nil }
        guard url.path.isEmpty || url.path == "/" else { return nil }
        guard let fragment = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedFragment else { return nil }
        for pair in fragment.split(separator: "&") {
            let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2, parts[0] == "token" else { continue }
            let value = String(parts[1]).removingPercentEncoding ?? ""
            return isUsableToken(value) ? value : nil
        }
        return nil
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
