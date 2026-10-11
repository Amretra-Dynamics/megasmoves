// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "MegasMoves",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "MegasMoves", targets: ["MegasMoves"])
    ],
    targets: [
        .target(
            name: "MegasNativeC",
            path: "Sources/MegasNativeC",
            publicHeadersPath: "include"
        ),
        .executableTarget(
            name: "MegasMoves",
            dependencies: ["MegasNativeC"],
            path: "Sources/MegasMoves"
        )
    ]
)
