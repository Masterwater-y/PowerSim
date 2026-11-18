#pragma once
#include "fastsim/config.hpp"
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace fastsim {
enum class DeviceIoKind : std::uint8_t { Unknown, Uart, Apic };
struct DeviceIoService {
    DeviceIoKind kind = DeviceIoKind::Unknown;
    std::uint32_t cycles = 0;
};

// Current functional x86 physical-address encoding. No device values,
// interrupts, outcomes or native timings are consumed/generated here.
inline DeviceIoService device_io_service(const DeviceIoConfig& c,
                                        std::uint64_t address,
                                        std::uint64_t size) {
    if (!c.enabled || !size || address > UINT64_MAX - (size - 1)) return {};
    DeviceIoKind kind = DeviceIoKind::Unknown;
    std::uint64_t ticks = 0;
    if (address >= c.uart_address && address - c.uart_address < c.uart_size &&
        size <= c.uart_size - (address - c.uart_address)) {
        kind = DeviceIoKind::Uart;
        ticks = c.uart_latency_ticks;
    } else if ((address & 0xfffffffffff00000ull) == 0x2000000000000000ull &&
               size <= 0x1000 - (address & 0xfffull)) {
        kind = DeviceIoKind::Apic;
        ticks = c.apic_latency_ticks;
    }
    if (kind == DeviceIoKind::Unknown) return {};
    if (!c.clock_period_ticks || !ticks)
        throw std::invalid_argument("device I/O requires source tick period and service latency");
    const auto cycles = ticks / c.clock_period_ticks + (ticks % c.clock_period_ticks != 0);
    if (cycles > std::numeric_limits<std::uint32_t>::max())
        throw std::invalid_argument("device I/O service exceeds bounded cycle descriptor");
    return {kind, static_cast<std::uint32_t>(cycles)};
}
}
