import SwiftUI

@main
struct MegasMovesApp: App {
    @State private var selectedTab: String? = "testing"

    var body: some Scene {
        WindowGroup {
            NavigationSplitView {
                List(selection: $selectedTab) {
                    NavigationLink(value: "overview") {
                        Label("Overview", systemImage: "chart.bar.xaxis")
                    }
                    NavigationLink(value: "testing") {
                        Label("Local Testing", systemImage: "play.laptopcomputer")
                    }
                    NavigationLink(value: "activity") {
                        Label("Activity", systemImage: "clock.arrow.circlepath")
                    }
                }
                .navigationTitle("MegasMoves")
                .frame(minWidth: 180)
            } detail: {
                switch selectedTab {
                case "overview":
                    OverviewView()
                case "testing":
                    TestingView()
                case "activity":
                    Text("Activity Feed (Authoritative Megas MCP Stream)")
                        .foregroundColor(.secondary)
                default:
                    TestingView()
                }
            }
            .frame(minWidth: 900, minHeight: 600)
        }
        .windowStyle(.titleBar)
    }
}
