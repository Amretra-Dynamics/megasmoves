#include "megas_runtime.h"

#include <atomic>
#include <cstring>
#include <mutex>
#include <new>
#include <string>

#if defined(_WIN32)
#include <windows.h>
#if __has_include(<TraceLoggingProvider.h>)
#define MEGAS_HAS_TRACELOGGING 1
#include <TraceLoggingProvider.h>

TRACELOGGING_DEFINE_PROVIDER(
    g_megas_moves_provider,
    "Amretra.MegasMoves.Native",
    (0x3c6a0bae, 0xdf62, 0x4b89, 0xa4, 0xa8, 0x26, 0xf1, 0x31, 0xa6, 0xf0, 0xd7));
#else
#define MEGAS_HAS_TRACELOGGING 0
#endif
#else
#include <cerrno>
#include <csignal>
#include <sys/types.h>
#include <unistd.h>
#include <unordered_set>
#if defined(__APPLE__)
#include <os/log.h>
#elif defined(__linux__)
#include <syslog.h>
#endif
#endif

namespace {

thread_local std::string g_last_error;

#if defined(_WIN32) && MEGAS_HAS_TRACELOGGING
std::mutex g_etw_mutex;
uint32_t g_etw_users = 0;
bool g_etw_registered = false;

bool acquire_etw_provider() noexcept {
    std::lock_guard<std::mutex> guard(g_etw_mutex);
    if (g_etw_users == 0) {
        g_etw_registered = TraceLoggingRegister(g_megas_moves_provider) == ERROR_SUCCESS;
    }
    ++g_etw_users;
    return g_etw_registered;
}

void release_etw_provider() noexcept {
    std::lock_guard<std::mutex> guard(g_etw_mutex);
    if (g_etw_users == 0) {
        return;
    }
    --g_etw_users;
    if (g_etw_users == 0 && g_etw_registered) {
        TraceLoggingUnregister(g_megas_moves_provider);
        g_etw_registered = false;
    }
}
#endif

void set_last_error(const char* text) noexcept {
    try {
        g_last_error = text ? text : "";
    } catch (...) {
        g_last_error.clear();
    }
}

#if defined(_WIN32)
void set_win32_error(const char* operation, DWORD error) noexcept {
    try {
        g_last_error = std::string(operation ? operation : "Win32 operation") +
            " failed with Windows error " + std::to_string(static_cast<unsigned long>(error));
    } catch (...) {
        g_last_error = "Windows native operation failed";
    }
}
#else
void set_posix_error(const char* operation, int err) noexcept {
    try {
        g_last_error = std::string(operation ? operation : "POSIX operation") +
            " failed with errno " + std::to_string(err) + ": " + std::strerror(err);
    } catch (...) {
        g_last_error = "POSIX native operation failed";
    }
}
#endif

void emit_event(const megas_runtime_callbacks& callbacks, const char* json) noexcept {
    if (!callbacks.on_event || !json) {
        return;
    }
    const auto length = std::strlen(json);
    callbacks.on_event(json, length, callbacks.user);
}

void emit_process_contained(const megas_runtime_callbacks& callbacks, uint32_t process_id) noexcept {
    if (!callbacks.on_event) {
        return;
    }
    try {
        const std::string json =
            std::string("{\"protocol_version\":1,\"source\":\"Native\",\"type\":\"native.process.contained\",\"severity\":\"info\",\"process_id\":") +
            std::to_string(process_id) + "}";
        callbacks.on_event(json.c_str(), json.size(), callbacks.user);
    } catch (...) {
        // Native evidence must never destabilize the containment operation.
    }
}

} // namespace

struct megas_runtime_handle_s {
    megas_runtime_callbacks callbacks{};
    std::atomic<bool> started{false};
#if defined(_WIN32)
    HANDLE job{nullptr};
    bool etw_registered{false};
#else
    std::mutex pids_mutex;
    std::unordered_set<pid_t> contained_pids;
#endif
};

extern "C" {

uint32_t MEGAS_RUNTIME_CALL megas_runtime_abi_version(void) {
    return MEGAS_RUNTIME_ABI_VERSION;
}

int32_t MEGAS_RUNTIME_CALL megas_runtime_create(
    const megas_runtime_callbacks* callbacks,
    megas_runtime_handle* out_handle) {
    if (!callbacks || !out_handle) {
        set_last_error("callbacks and out_handle are required");
        return MEGAS_RUNTIME_E_INVALID_ARGUMENT;
    }
    *out_handle = nullptr;
    if (callbacks->struct_size < sizeof(megas_runtime_callbacks)) {
        set_last_error("callback structure is smaller than ABI v1 requires");
        return MEGAS_RUNTIME_E_ABI_MISMATCH;
    }
    if (callbacks->abi_version != MEGAS_RUNTIME_ABI_VERSION) {
        set_last_error("native runtime ABI version mismatch");
        return MEGAS_RUNTIME_E_ABI_MISMATCH;
    }

    auto* handle = new (std::nothrow) megas_runtime_handle_s{};
    if (!handle) {
        set_last_error("unable to allocate native runtime handle");
        return MEGAS_RUNTIME_E_OUT_OF_MEMORY;
    }
    handle->callbacks = *callbacks;
    *out_handle = handle;
    set_last_error("");
    return MEGAS_RUNTIME_OK;
}

int32_t MEGAS_RUNTIME_CALL megas_runtime_start(megas_runtime_handle handle) {
    if (!handle) {
        set_last_error("runtime handle is required");
        return MEGAS_RUNTIME_E_INVALID_ARGUMENT;
    }
    bool expected = false;
    if (!handle->started.compare_exchange_strong(expected, true)) {
        set_last_error("");
        return MEGAS_RUNTIME_OK;
    }

#if defined(_WIN32)
    handle->job = CreateJobObjectW(nullptr, nullptr);
    if (!handle->job) {
        handle->started.store(false);
        set_win32_error("CreateJobObjectW", GetLastError());
        return MEGAS_RUNTIME_E_INTERNAL;
    }

    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(
            handle->job,
            JobObjectExtendedLimitInformation,
            &limits,
            sizeof(limits))) {
        const auto error = GetLastError();
        CloseHandle(handle->job);
        handle->job = nullptr;
        handle->started.store(false);
        set_win32_error("SetInformationJobObject", error);
        return MEGAS_RUNTIME_E_INTERNAL;
    }

#if MEGAS_HAS_TRACELOGGING
    handle->etw_registered = acquire_etw_provider();
    if (handle->etw_registered) {
        TraceLoggingWrite(
            g_megas_moves_provider,
            "RuntimeStarted",
            TraceLoggingUInt32(MEGAS_RUNTIME_ABI_VERSION, "AbiVersion"));
    }
#endif
#endif

    emit_event(
        handle->callbacks,
        R"({"protocol_version":1,"source":"Native","type":"native.runtime.started","severity":"info"})");
    set_last_error("");
    return MEGAS_RUNTIME_OK;
}

int32_t MEGAS_RUNTIME_CALL megas_runtime_stop(megas_runtime_handle handle) {
    if (!handle) {
        set_last_error("runtime handle is required");
        return MEGAS_RUNTIME_E_INVALID_ARGUMENT;
    }
    bool expected = true;
    if (!handle->started.compare_exchange_strong(expected, false)) {
        set_last_error("");
        return MEGAS_RUNTIME_OK;
    }

    emit_event(
        handle->callbacks,
        R"({"protocol_version":1,"source":"Native","type":"native.runtime.stopped","severity":"info"})");

#if defined(_WIN32)
#if MEGAS_HAS_TRACELOGGING
    if (handle->etw_registered) {
        TraceLoggingWrite(
            g_megas_moves_provider,
            "RuntimeStopped",
            TraceLoggingUInt32(MEGAS_RUNTIME_ABI_VERSION, "AbiVersion"));
        release_etw_provider();
        handle->etw_registered = false;
    }
#endif
    if (handle->job) {
        CloseHandle(handle->job);
        handle->job = nullptr;
    }
#else
    std::unordered_set<pid_t> pids;
    {
        std::lock_guard<std::mutex> guard(handle->pids_mutex);
        pids = std::move(handle->contained_pids);
        handle->contained_pids.clear();
    }
    for (pid_t pid : pids) {
        if (kill(pid, 0) == 0) {
            kill(pid, SIGTERM);
        }
    }
#endif

    set_last_error("");
    return MEGAS_RUNTIME_OK;
}

int32_t MEGAS_RUNTIME_CALL megas_runtime_contain_process(
    megas_runtime_handle handle,
    uint32_t process_id) {
    if (!handle || process_id == 0) {
        set_last_error("runtime handle and non-zero process_id are required");
        return MEGAS_RUNTIME_E_INVALID_ARGUMENT;
    }
    if (!handle->started.load()) {
        set_last_error("native runtime must be started before containing a process");
        return MEGAS_RUNTIME_E_INVALID_STATE;
    }

#if !defined(_WIN32)
    const auto pid = static_cast<pid_t>(process_id);
    if (pid <= 0 || (kill(pid, 0) != 0 && errno == ESRCH)) {
        set_posix_error("kill(0)", errno);
        return MEGAS_RUNTIME_E_PROCESS_ACCESS;
    }

    {
        std::lock_guard<std::mutex> guard(handle->pids_mutex);
        handle->contained_pids.insert(pid);
    }

#if defined(__APPLE__)
    os_log(OS_LOG_DEFAULT, "MegasMoves Native: contained process %u", process_id);
#elif defined(__linux__)
    syslog(LOG_INFO, "MegasMoves Native: contained process %u", process_id);
#endif

    emit_process_contained(handle->callbacks, process_id);
    set_last_error("");
    return MEGAS_RUNTIME_OK;
#else
    if (!handle->job) {
        set_last_error("Windows Job Object is unavailable");
        return MEGAS_RUNTIME_E_INVALID_STATE;
    }

    const DWORD access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION;
    HANDLE process = OpenProcess(access, FALSE, static_cast<DWORD>(process_id));
    if (!process) {
        set_win32_error("OpenProcess", GetLastError());
        return MEGAS_RUNTIME_E_PROCESS_ACCESS;
    }

    const BOOL assigned = AssignProcessToJobObject(handle->job, process);
    const DWORD error = assigned ? ERROR_SUCCESS : GetLastError();
    CloseHandle(process);

    if (!assigned) {
        set_win32_error("AssignProcessToJobObject", error);
        return MEGAS_RUNTIME_E_PROCESS_ACCESS;
    }

#if MEGAS_HAS_TRACELOGGING
    if (handle->etw_registered) {
        TraceLoggingWrite(
            g_megas_moves_provider,
            "ProcessContained",
            TraceLoggingUInt32(process_id, "ProcessId"));
    }
#endif
    emit_process_contained(handle->callbacks, process_id);
    set_last_error("");
    return MEGAS_RUNTIME_OK;
#endif
}

void MEGAS_RUNTIME_CALL megas_runtime_destroy(megas_runtime_handle handle) {
    if (!handle) {
        return;
    }
    if (handle->started.exchange(false)) {
        emit_event(
            handle->callbacks,
            R"({"protocol_version":1,"source":"Native","type":"native.runtime.stopped","severity":"info","reason":"destroy"})");
    }
#if defined(_WIN32)
#if MEGAS_HAS_TRACELOGGING
    if (handle->etw_registered) {
        TraceLoggingWrite(
            g_megas_moves_provider,
            "RuntimeDestroyed",
            TraceLoggingUInt32(MEGAS_RUNTIME_ABI_VERSION, "AbiVersion"));
        release_etw_provider();
        handle->etw_registered = false;
    }
#endif
    if (handle->job) {
        CloseHandle(handle->job);
        handle->job = nullptr;
    }
#else
    std::unordered_set<pid_t> pids;
    {
        std::lock_guard<std::mutex> guard(handle->pids_mutex);
        pids = std::move(handle->contained_pids);
        handle->contained_pids.clear();
    }
    for (pid_t pid : pids) {
        if (kill(pid, 0) == 0) {
            kill(pid, SIGTERM);
        }
    }
#endif
    delete handle;
}

int32_t MEGAS_RUNTIME_CALL megas_runtime_last_error(
    char* buffer,
    size_t capacity,
    size_t* out_required) {
    if (!out_required) {
        return MEGAS_RUNTIME_E_INVALID_ARGUMENT;
    }
    const size_t required = g_last_error.size() + 1;
    *out_required = required;
    if (!buffer || capacity < required) {
        return required == 1 && !buffer ? MEGAS_RUNTIME_OK : MEGAS_RUNTIME_E_BUFFER_TOO_SMALL;
    }
    std::memcpy(buffer, g_last_error.c_str(), required);
    return MEGAS_RUNTIME_OK;
}

} // extern "C"
