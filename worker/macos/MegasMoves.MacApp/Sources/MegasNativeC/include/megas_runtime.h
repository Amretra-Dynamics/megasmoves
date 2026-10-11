#pragma once

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#define MEGAS_RUNTIME_API __declspec(dllexport)
#define MEGAS_RUNTIME_CALL __cdecl
#elif defined(__GNUC__) || defined(__clang__)
#define MEGAS_RUNTIME_API __attribute__((visibility("default")))
#define MEGAS_RUNTIME_CALL
#else
#define MEGAS_RUNTIME_API
#define MEGAS_RUNTIME_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define MEGAS_RUNTIME_ABI_VERSION 1u

typedef struct megas_runtime_handle_s* megas_runtime_handle;

typedef void(*megas_runtime_event_callback)(
    const char* utf8_json,
    size_t len,
    void* user);

typedef struct megas_runtime_callbacks {
    uint32_t struct_size;
    uint32_t abi_version;
    megas_runtime_event_callback on_event;
    void* user;
} megas_runtime_callbacks;

typedef enum megas_runtime_result {
    MEGAS_RUNTIME_OK = 0,
    MEGAS_RUNTIME_E_INVALID_ARGUMENT = -1,
    MEGAS_RUNTIME_E_ABI_MISMATCH = -2,
    MEGAS_RUNTIME_E_OUT_OF_MEMORY = -3,
    MEGAS_RUNTIME_E_INVALID_STATE = -4,
    MEGAS_RUNTIME_E_BUFFER_TOO_SMALL = -5,
    MEGAS_RUNTIME_E_NOT_SUPPORTED = -6,
    MEGAS_RUNTIME_E_PROCESS_ACCESS = -7,
    MEGAS_RUNTIME_E_INTERNAL = -100
} megas_runtime_result;

MEGAS_RUNTIME_API uint32_t megas_runtime_abi_version(void);

MEGAS_RUNTIME_API int32_t megas_runtime_create(
    const megas_runtime_callbacks* callbacks,
    megas_runtime_handle* out_handle);

MEGAS_RUNTIME_API int32_t megas_runtime_start(
    megas_runtime_handle handle);

MEGAS_RUNTIME_API int32_t megas_runtime_stop(
    megas_runtime_handle handle);

MEGAS_RUNTIME_API int32_t megas_runtime_contain_process(
    megas_runtime_handle handle,
    uint32_t process_id);

MEGAS_RUNTIME_API void megas_runtime_destroy(
    megas_runtime_handle handle);

MEGAS_RUNTIME_API int32_t megas_runtime_last_error(
    char* buffer,
    size_t capacity,
    size_t* out_required);

#ifdef __cplusplus
}
#endif
