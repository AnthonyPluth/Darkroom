// swift-tools-version:5.9

import PackageDescription

let package = Package(
    name: "DarkroomCore",
    platforms: [.iOS(.v17), .macOS(.v13)],
    products: [
        .library(name: "DarkroomCore", targets: ["DarkroomCore"]),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-crypto.git", from: "3.0.0"),
    ],
    targets: [
        .target(
            name: "DarkroomCore",
            dependencies: [.product(name: "Crypto", package: "swift-crypto")]
        ),
        .testTarget(name: "DarkroomCoreTests", dependencies: ["DarkroomCore"]),
    ]
)
