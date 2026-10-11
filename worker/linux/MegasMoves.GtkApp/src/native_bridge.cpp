#include "native_bridge.hpp"
#include <iostream>

namespace megas {

namespace {
void MEGAS_RUNTIME_CALL on_runtime_event(const char* utf8_json, size_t len, void* /*user*/) {
    if (utf8_json && len > 0) {
        std::cout << "[Megas Native Event] " << std::string(utf8_json, len) << std::endl;
    }
}
} // namespace

NativeBridge& NativeBridge::instance() {
    static NativeBridge bridge;
    return bridge;
}

NativeBridge::NativeBridge() = default;

NativeBridge::~NativeBridge() {
    shutdown();
}

bool NativeBridge::initialize() {
    if (handle_) return true;

    megas_runtime_callbacks callbacks{};
    callbacks.struct_size = sizeof(callbacks);
    callbacks.abi_version = MEGAS_RUNTIME_ABI_VERSION;
    callbacks.on_event = on_runtime_event;
    callbacks.user = nullptr;

    int32_t rc = megas_runtime_create(&callbacks, &handle_);
    if (rc != MEGAS_RUNTIME_OK || !handle_) {
        return false;
    }

    rc = megas_runtime_start(handle_);
    return rc == MEGAS_RUNTIME_OK;
}

void NativeBridge::shutdown() {
    if (handle_) {
        megas_runtime_stop(handle_);
        megas_runtime_destroy(handle_);
        handle_ = nullptr;
    }
}

bool NativeBridge::contain_process(uint32_t pid) {
    if (!handle_ || pid == 0) return false;
    int32_t rc = megas_runtime_contain_process(handle_, pid);
    return rc == MEGAS_RUNTIME_OK;
}

} // namespace megas
