import Foundation

public final class CoreProcessManager: @unchecked Sendable {
    public static let shared = CoreProcessManager()

    private var process: Process?
    private var stdinPipe: Pipe?
    private var stdoutPipe: Pipe?
    private let queue = DispatchQueue(label: "io.megasmoves.core.queue")

    private init() {}

    public func startCore(coreBinaryPath: String? = nil) {
        guard process == nil else { return }

        let resolvedBinary: String
        if let custom = coreBinaryPath, FileManager.default.fileExists(atPath: custom) {
            resolvedBinary = custom
        } else {
            let bundleDir = Bundle.main.bundlePath
            let candidate = (bundleDir as NSString).appendingPathComponent("Contents/MacOS/MegasMoves.Core")
            if FileManager.default.fileExists(atPath: candidate) {
                resolvedBinary = candidate
            } else {
                resolvedBinary = "MegasMoves.Core"
            }
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: resolvedBinary)
        let inPipe = Pipe()
        let outPipe = Pipe()
        proc.standardInput = inPipe
        proc.standardOutput = outPipe

        do {
            try proc.run()
            self.process = proc
            self.stdinPipe = inPipe
            self.stdoutPipe = outPipe
            _ = NativeBridge.shared.containProcess(pid: proc.processIdentifier)
        } catch {
            print("Failed to start embedded MegasMoves.Core: \(error)")
        }
    }

    public func dispatch(method: String, params: [String: Any]) async throws -> [String: Any] {
        guard let inPipe = stdinPipe, let outPipe = stdoutPipe else {
            throw NSError(domain: "MegasMoves", code: -1, userInfo: [NSLocalizedDescriptionKey: "Core process is not active"])
        }

        let request: [String: Any] = [
            "method": method,
            "params": params,
            "id": UUID().uuidString
        ]

        let data = try JSONSerialization.data(withJSONObject: request)
        inPipe.fileHandleForWriting.write(data)
        inPipe.fileHandleForWriting.write("\n".data(using: .utf8)!)

        let available = outPipe.fileHandleForReading.availableData
        if let responseObj = try? JSONSerialization.jsonObject(with: available) as? [String: Any] {
            return responseObj
        }
        return ["ok": false, "error": "Invalid response from Core"]
    }

    public func stopCore() {
        if let proc = process {
            proc.terminate()
            process = nil
        }
    }
}
