import Foundation

public struct LocalTarget: Identifiable, Codable, Sendable {
    public let id: String
    public let kind: String
    public let label: String
    public let runtime: String
    public let projectPath: String?
    public let actions: [String]

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case label
        case runtime
        case projectPath = "project_path"
        case actions
    }

    public var isMacTarget: Bool {
        kind == "macos"
    }

    public var isLinuxTarget: Bool {
        kind == "linux"
    }

    public var isAndroidTarget: Bool {
        kind == "android"
    }

    public var isWebTarget: Bool {
        kind == "web"
    }
}

public struct LocalProject: Identifiable, Codable, Sendable {
    public var id: String { name }
    public let name: String
    public let root: String
    public let targets: [LocalTarget]?
}

public struct ActionResult: Codable, Sendable {
    public let ok: Bool
    public let stage: String
    public let message: String?
    public let stdout: String?
    public let stderr: String?
    public let pid: Int?
}
