#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace megas {

class CoreProcessManager {
public:
    static CoreProcessManager& instance();

    bool start_core(const std::string& custom_binary = "");
    void stop_core();
    std::string dispatch_action(const std::string& target_id, const std::string& action);

private:
    CoreProcessManager();
    ~CoreProcessManager();

    pid_t core_pid_{0};
    int stdin_fd_{-1};
    int stdout_fd_{-1};
};

} // namespace megas
