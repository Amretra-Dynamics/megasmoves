import SwiftUI

public struct OverviewView: View {
    public init() {}

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("MegasMoves Operator Overview")
                    .font(.largeTitle)
                    .bold()

                Text("macOS Native Operator Console · Universal Fabric")
                    .font(.subheadline)
                    .foregroundColor(.secondary)

                HStack(spacing: 16) {
                    MetricCard(title: "Trusted Folders", value: "3", icon: "folder.badge.gear")
                    MetricCard(title: "Active Runtimes", value: "2", icon: "bolt.horizontal.fill")
                    MetricCard(title: "Connected Devices", value: "1 (ADB)", icon: "candybarphone")
                    MetricCard(title: "Native ABI", value: "v1 (C++20)", icon: "cpu.fill")
                }

                Divider()

                VStack(alignment: .leading, spacing: 8) {
                    Text("Host Computable Surface & Capabilities")
                        .font(.headline)

                    Text("Device #1 Host: macOS Apple Silicon (Native AppKit / SwiftUI)")
                        .font(.callout)
                    Text("• macOS & iOS Simulator: Native Execution (Rank: 100)")
                        .font(.caption)
                    Text("• Linux Targets: Lightweight Apple Virtualization Framework (Rank: 80)")
                        .font(.caption)
                    Text("• Android Mobile: Fast local ADB over USB / Wireless Pairing")
                        .font(.caption)
                    Text("• Windows Targets: Bounded LAN Bridge to remote Windows node")
                        .font(.caption)
                }
                .padding()
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(8)
            }
            .padding()
        }
    }
}

struct MetricCard: View {
    let title: String
    let value: String
    let icon: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: icon)
                    .font(.title2)
                    .foregroundColor(.accentColor)
                Spacer()
            }
            Text(value)
                .font(.title)
                .bold()
            Text(title)
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .padding()
        .frame(maxWidth: .infinity)
        .background(Color(NSColor.controlBackgroundColor))
        .cornerRadius(8)
    }
}
