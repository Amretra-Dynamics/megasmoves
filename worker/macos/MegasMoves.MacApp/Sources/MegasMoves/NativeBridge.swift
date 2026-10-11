import Foundation
import MegasNativeC

public final class NativeBridge: @unchecked Sendable {
    public static let shared = NativeBridge()

    private var handle: megas_runtime_handle?
    private let lock = NSLock()

    private init() {
        var callbacks = megas_runtime_callbacks(
            struct_size: UInt32(MemoryLayout<megas_runtime_callbacks>.size),
            abi_version: MEGAS_RUNTIME_ABI_VERSION,
            on_event: { jsonPtr, len, _ in
                guard let jsonPtr = jsonPtr else { return }
                let eventString = String(cString: jsonPtr)
                NotificationCenter.default.post(
                    name: .megasNativeEvent,
                    object: nil,
                    userInfo: ["json": eventString]
                )
            },
            user: nil
        )

        var outHandle: megas_runtime_handle?
        let result = megas_runtime_create(&callbacks, &outHandle)
        if result == MEGAS_RUNTIME_OK.rawValue {
            self.handle = outHandle
            _ = megas_runtime_start(outHandle)
        }
    }

    deinit {
        if let handle = handle {
            megas_runtime_stop(handle)
            megas_runtime_destroy(handle)
        }
    }

    public func containProcess(pid: pid_t) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let handle = handle, pid > 0 else { return false }
        let result = megas_runtime_contain_process(handle, UInt32(pid))
        return result == MEGAS_RUNTIME_OK.rawValue
    }
}

public extension Notification.Name {
    static let megasNativeEvent = Notification.Name("MegasNativeEventReceived")
}
