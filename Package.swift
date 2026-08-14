// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "AgentsLight",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "AgentsLight", targets: ["AgentsLight"]),
    ],
    targets: [
        .executableTarget(
            name: "AgentsLight",
            resources: [.process("Resources")]
        ),
        .testTarget(name: "AgentsLightTests", dependencies: ["AgentsLight"]),
    ]
)
