#pragma once
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <limits>
#include <mutex>
#include <sched.h>
#include <stdexcept>
#include <thread>
#include <vector>

namespace tracebench {
inline unsigned preparation_workers(size_t files) {
    if (!files) return 0;
    cpu_set_t affinity;
    CPU_ZERO(&affinity);
    if (sched_getaffinity(0, sizeof(affinity), &affinity) || !CPU_COUNT(&affinity))
        throw std::runtime_error("cannot establish preparation CPU budget");
    size_t requested = files;
    const char *setting = std::getenv("TRACEBENCH_PREPARE_JOBS");
    if (setting && *setting && std::strcmp(setting, "auto")) {
        for (const char *p = setting; *p; ++p)
            if (*p < '0' || *p > '9')
                throw std::invalid_argument("TRACEBENCH_PREPARE_JOBS must be positive or auto");
        errno = 0;
        char *end = nullptr;
        auto value = std::strtoul(setting, &end, 10);
        if (errno || *end || !value || value > std::numeric_limits<unsigned>::max())
            throw std::invalid_argument("invalid TRACEBENCH_PREPARE_JOBS");
        requested = value;
    }
    return static_cast<unsigned>(std::min({files, requested, size_t(CPU_COUNT(&affinity))}));
}

template <class Function>
void parallel_prepare(size_t files, unsigned jobs, Function &&prepare) {
    if (!files) return;
    if (!jobs) throw std::invalid_argument("zero preparation workers");
    jobs = static_cast<unsigned>(std::min(files, size_t(jobs)));
    if (jobs == 1) {
        for (size_t i = 0; i < files; ++i) prepare(i);
        return;
    }
    std::atomic<size_t> next{0};
    std::atomic<bool> stop{false};
    std::mutex errorMutex;
    std::exception_ptr error;
    auto worker = [&] {
        while (!stop.load()) {
            size_t i = next.fetch_add(1);
            if (i >= files) return;
            try { prepare(i); }
            catch (...) {
                std::lock_guard<std::mutex> lock(errorMutex);
                if (!error) error = std::current_exception();
                stop.store(true);
                return;
            }
        }
    };
    std::vector<std::thread> threads;
    threads.reserve(jobs);
    try {
        for (unsigned i = 0; i < jobs; ++i) threads.emplace_back(worker);
    } catch (...) {
        stop.store(true);
        for (auto &thread : threads) thread.join();
        throw;
    }
    for (auto &thread : threads) thread.join();
    if (error) std::rethrow_exception(error);
}
}
