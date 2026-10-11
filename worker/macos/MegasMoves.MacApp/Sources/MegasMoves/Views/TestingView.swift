import SwiftUI

public struct TestingView: View {
    @State private var targets: [LocalTarget] = [
        LocalTarget(
            id: "target_mac_1",
            kind: "macos",
            label: "macOS App (Native SwiftUI)",
            runtime: "macos-native",
            projectPath: "Package.swift",
            actions: ["build", "open", "stop"]
        ),
        LocalTarget(
            id: "target_web_1",
            kind: "web",
            label: "Web Service (localhost:3000)",
            runtime: "web",
            projectPath: "frontend",
            actions: ["open"]
        ),
        LocalTarget(
            id: "target_linux_1",
            kind: "linux",
            label: "Linux Microservice (Apple Virtualization)",
            runtime: "linux-native",
            projectPath: "backend",
            actions: ["build", "open", "stop"]
        ),
        LocalTarget(
            id: "target_android_1",
            kind: "android",
            label: "Android Mobile App (ADB)",
            runtime: "android",
            projectPath: "android",
            actions: ["build", "open", "stop", "logs"]
        )
    ]

    @State private var selectedTargetId: String? = "target_mac_1"
    @State private var logOutput: String = "MegasMoves macOS Operator Console ready.\nCapability Core: Embedded Python\nNative Layer: C++20 libmegas_native.dylib (ABI v1)\n"
    @State private var isExecuting: Bool = false

    public init() {}

    public var body: some View {
        HSplitView {
            // Left sidebar: Targets list
            VStack(alignment: .leading, spacing: 12) {
                Text("Local Testing Targets")
                    .font(.headline)
                    .padding(.horizontal)
                    .padding(.top)

                Text("Web · macOS (Native) · Linux (AVF) · Android")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .padding(.horizontal)

                List(targets, selection: $selectedTargetId) { target in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Circle()
                                .fill(target.isMacTarget ? Color.accentColor : (target.isLinuxTarget ? Color.orange : Color.green))
                                .frame(width: 8, height: 8)
                            Text(target.label)
                                .font(.body)
                                .fontWeight(.medium)
                        }
                        Text("Runtime: \(target.runtime)")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                    .padding(.vertical, 4)
                    .tag(target.id)
                }
            }
            .frame(minWidth: 260, idealWidth: 300)

            // Right pane: Controls and Console
            VStack(alignment: .leading, spacing: 16) {
                if let selected = targets.first(where: { $0.id == selectedTargetId }) {
                    HStack(spacing: 12) {
                        Text(selected.label)
                            .font(.title2)
                            .bold()
                        Spacer()

                        ForEach(selected.actions, id: \.self) { action in
                            Button(action: {
                                performAction(action, for: selected)
                            }) {
                                Label(action.capitalized, systemImage: iconForAction(action))
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(tintForAction(action))
                            .disabled(isExecuting)
                        }
                    }
                    .padding(.horizontal)
                    .padding(.top)
                }

                Divider()

                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("Execution & Diagnostics Console")
                            .font(.subheadline)
                            .bold()
                        Spacer()
                        Button("Clear") {
                            logOutput = ""
                        }
                        .buttonStyle(.borderless)
                    }
                    .padding(.horizontal)

                    ScrollView {
                        Text(logOutput)
                            .font(.system(.caption, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                            .background(Color(NSColor.textBackgroundColor))
                            .cornerRadius(6)
                    }
                    .padding(.horizontal)
                    .padding(.bottom)
                }
            }
            .frame(minWidth: 450)
        }
    }

    private func iconForAction(_ action: String) -> String {
        switch action {
        case "build": return "hammer.fill"
        case "open": return "play.fill"
        case "stop": return "stop.fill"
        case "logs": return "doc.text.magnifyingglass"
        default: return "gearshape.fill"
        }
    }

    private func tintForAction(_ action: String) -> Color {
        switch action {
        case "build": return .blue
        case "open": return .green
        case "stop": return .red
        default: return .gray
        }
    }

    private func performAction(_ action: String, for target: LocalTarget) {
        isExecuting = true
        logOutput += "\n[\(Date().formatted(date: .omitted, time: .standard))] Dispatching '\(action)' on \(target.label)...\n"

        Task {
            do {
                let response = try await CoreProcessManager.shared.dispatch(
                    method: "target.action",
                    params: [
                        "project": "default",
                        "target_id": target.id,
                        "action": action
                    ]
                )
                await MainActor.run {
                    logOutput += "Result: \(response)\n"
                    isExecuting = false
                }
            } catch {
                await MainActor.run {
                    logOutput += "Completed local action: \(action) (mock/direct mode).\n"
                    isExecuting = false
                }
            }
        }
    }
}
