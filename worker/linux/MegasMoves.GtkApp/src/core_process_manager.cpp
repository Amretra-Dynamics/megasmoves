#include "core_process_manager.hpp"
#include "native_bridge.hpp"

#if !defined(_WIN32)
#include <unistd.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#endif

#include <iostream>

namespace megas {

CoreProcessManager& CoreProcessManager::instance() {
    static CoreProcessManager manager;
    return manager;
}

CoreProcessManager::CoreProcessManager() = default;

CoreProcessManager::~CoreProcessManager() {
    stop_core();
}

bool CoreProcessManager::start_core(const std::string& custom_binary) {
#if !defined(_WIN32)
    if (core_pid_ > 0) return true;

    int in_pipe[2];
    int out_pipe[2];
    if (pipe(in_pipe) != 0 || pipe(out_pipe) != 0) {
        return false;
    }

    pid_t pid = fork();
    if (pid < 0) {
        return false;
    }

    if (pid == 0) {
        // Child process: execute embedded MegasMoves.Core
        close(in_pipe[1]);
        close(out_pipe[0]);
        dup2(in_pipe[0], STDIN_FILENO);
        dup2(out_pipe[1], STDOUT_FILENO);

        std::string bin = custom_binary.empty() ? "MegasMoves.Core" : custom_binary;
        char* argv[] = {const_cast<char*>(bin.c_str()), nullptr};
        execvp(bin.c_str(), argv);
        execv("./MegasMoves.Core", argv);
        _exit(127);
    }

    // Parent process
    close(in_pipe[0]);
    close(out_pipe[1]);
    stdin_fd_ = in_pipe[1];
    stdout_fd_ = out_pipe[0];
    core_pid_ = pid;

    NativeBridge::instance().contain_process(static_cast<uint32_t>(pid));
    return true;
#else
    return false;
#endif
}

void CoreProcessManager::stop_core() {
#if !defined(_WIN32)
    if (core_pid_ > 0) {
        kill(core_pid_, SIGTERM);
        waitpid(core_pid_, nullptr, WNOHANG);
        core_pid_ = 0;
    }
    if (stdin_fd_ >= 0) {
        close(stdin_fd_);
        stdin_fd_ = -1;
    }
    if (stdout_fd_ >= 0) {
        close(stdout_fd_);
        stdout_fd_ = -1;
    }
#endif
}

std::string CoreProcessManager::dispatch_action(const std::string& target_id, const std::string& action) {
    return "{\"ok\": true, \"target\": \"" + target_id + "\", \"action\": \"" + action + "\"}";
}

} // namespace megas
