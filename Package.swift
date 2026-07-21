// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "CodexStatusLight",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "CodexStatusLight", targets: ["CodexStatusLight"]),
    ],
    targets: [
        .executableTarget(name: "CodexStatusLight"),
        .testTarget(name: "CodexStatusLightTests", dependencies: ["CodexStatusLight"]),
    ]
)
