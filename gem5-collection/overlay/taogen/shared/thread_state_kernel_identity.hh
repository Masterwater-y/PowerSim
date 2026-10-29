#pragma once
#include <cstdint>
#include <optional>
#include <string_view>

namespace fastsim {
// Exact-build semantic identity, not a guessed PID for an unreadable task.
// Linux's static boot/idle task retains PID 0 for its lifetime. The audited
// 5.4.49 ELF binds init_task to this address and its pid member to offset 0x758.
// Dynamic idle tasks and every other kernel/build still require a live read.
inline std::optional<std::int32_t> known_linux54_boot_task_pid(
    std::string_view kernel_sha256, std::uint64_t pointer)
{
    if (kernel_sha256 ==
            "da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174" &&
        pointer == 0xffffffff81e11780ULL)
        return 0;
    return std::nullopt;
}
}
