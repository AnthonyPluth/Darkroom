import SwiftUI

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }

    static let canvas = Color(hex: 0x16140F)
    static let rail = Color(hex: 0x1B1813)
    static let surface = Color(hex: 0x1F1C17)
    static let raised = Color(hex: 0x2B2721)
    static let ink = Color(hex: 0xF1ECE2)
    static let inkMuted = Color(hex: 0xB3AB9D)
    static let line = Color(hex: 0x3A352C)
    static let lineStrong = Color(hex: 0x7A7260)
    static let accent = Color(hex: 0xE2693F)
    static let accentInk = Color(hex: 0x1A0E08)
    static let ok = Color(hex: 0x7CC38F)
    static let okBg = Color(hex: 0x1D2B20)
    static let warn = Color(hex: 0xE0B253)
    static let warnBg = Color(hex: 0x332A12)
    static let danger = Color(hex: 0xF08A84)
    static let dangerBg = Color(hex: 0x3A1A1A)
}

enum Layout {
    static let columnWidth: CGFloat = 560
    static let gutter: CGFloat = 16
    static let cornerRadius: CGFloat = 12
    static let controlRadius: CGFloat = 8
}
