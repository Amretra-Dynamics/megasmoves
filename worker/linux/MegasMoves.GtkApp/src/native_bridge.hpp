#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <functional>
#include "megas_runtime.h"

namespace megas {

class NativeBridge {
public:
    static NativeBridge& instance();

    bool initialize();
    void shutdown();
    bool contain_process(uint32_t pid);

private:
    NativeBridge();
    ~NativeBridge();

    megas_runtime_handle handle_{nullptr};
};

} // namespace megas
