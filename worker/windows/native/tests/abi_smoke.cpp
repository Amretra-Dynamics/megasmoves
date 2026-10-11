#include "megas_runtime.h"

#include <cassert>
#include <string>
#include <vector>

namespace {

void MEGAS_RUNTIME_CALL on_event(const char* json, size_t len, void* user) {
    auto* events = static_cast<std::vector<std::string>*>(user);
    events->emplace_back(json, len);
}

} // namespace

int main() {
    assert(megas_runtime_abi_version() == MEGAS_RUNTIME_ABI_VERSION);

    std::vector<std::string> events;
    megas_runtime_callbacks callbacks{};
    callbacks.struct_size = sizeof(callbacks);
    callbacks.abi_version = MEGAS_RUNTIME_ABI_VERSION;
    callbacks.on_event = on_event;
    callbacks.user = &events;

    megas_runtime_handle handle = nullptr;
    assert(megas_runtime_create(&callbacks, &handle) == MEGAS_RUNTIME_OK);
    assert(handle != nullptr);

    assert(megas_runtime_contain_process(handle, 0) == MEGAS_RUNTIME_E_INVALID_ARGUMENT);
    assert(megas_runtime_contain_process(handle, 1) == MEGAS_RUNTIME_E_INVALID_STATE);

    assert(megas_runtime_start(handle) == MEGAS_RUNTIME_OK);
    assert(megas_runtime_start(handle) == MEGAS_RUNTIME_OK); // idempotent
    assert(events.size() == 1);
    assert(events[0].find("native.runtime.started") != std::string::npos);

#if defined(_WIN32) || defined(__linux__) || defined(__APPLE__)
    assert(megas_runtime_contain_process(handle, 0xFFFFFFFEu) == MEGAS_RUNTIME_E_PROCESS_ACCESS);
#else
    assert(megas_runtime_contain_process(handle, 0xFFFFFFFEu) == MEGAS_RUNTIME_E_NOT_SUPPORTED);
#endif

    assert(megas_runtime_stop(handle) == MEGAS_RUNTIME_OK);
    assert(megas_runtime_stop(handle) == MEGAS_RUNTIME_OK); // idempotent
    assert(events.size() == 2);
    assert(events[1].find("native.runtime.stopped") != std::string::npos);

    megas_runtime_destroy(handle);

    megas_runtime_callbacks wrong = callbacks;
    wrong.abi_version = MEGAS_RUNTIME_ABI_VERSION + 1;
    handle = nullptr;
    assert(megas_runtime_create(&wrong, &handle) == MEGAS_RUNTIME_E_ABI_MISMATCH);
    assert(handle == nullptr);

    return 0;
}
