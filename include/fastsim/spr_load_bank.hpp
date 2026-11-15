#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <limits>
#include <stdexcept>

namespace fastsim {
// Sparse target-cycle admission accounting for gem5-spr LSQ load banks.
// Caller checks load eligibility and port/backpressure before accept(); only
// accepted physical packets consume a bank slot. Ordering and target cycles
// are model estimates, not recorded gem5 events. The owner must prune only
// before a monotone lower bound on all future packet issue cycles.
class SprLoadBank {
  public:
    static constexpr bool packet_eligible(bool physical_address, bool ordinary,
                                          bool facts_present, bool split,
                                          bool single_packet) {
        return physical_address && ordinary && facts_present && !split && single_packet;
    }
    static constexpr std::size_t bank(std::uint64_t physical_address) {
        return static_cast<std::size_t>((physical_address >> 2) & 15u);
    }
    bool available(std::uint64_t physical_address, std::uint64_t cycle) const {
        const auto it=cycles_.find(cycle);
        return it==cycles_.end() || it->second.bank[bank(physical_address)]<2;
    }
    bool port_available(std::uint64_t cycle, std::uint32_t ports) const {
        const auto it=cycles_.find(cycle);
        return it==cycles_.end() || it->second.total<ports;
    }
    // Find a gap using only recorded occupied cycles; does not advance a
    // simulated clock or create entries for idle cycles.
    std::uint64_t first_available(std::uint64_t physical_address,
                                  std::uint64_t earliest,
                                  std::uint32_t ports=std::numeric_limits<std::uint32_t>::max(),
                                  bool known_bank=true) const {
        const auto index=bank(physical_address);
        auto cycle=earliest;
        auto it=cycles_.lower_bound(cycle);
        while (it!=cycles_.end() && it->first==cycle &&
               (it->second.total>=ports || (known_bank && it->second.bank[index]>=2))) {
            if (cycle==std::numeric_limits<std::uint64_t>::max())
                throw std::overflow_error("SPR load bank issue time overflow");
            ++cycle;
            ++it;
        }
        return cycle;
    }
    void accept(std::uint64_t physical_address, std::uint64_t cycle,
                std::uint32_t ports=std::numeric_limits<std::uint32_t>::max(),
                bool known_bank=true) {
        auto& counts=cycles_[cycle];
        auto& used=counts.bank[bank(physical_address)];
        if (counts.total>=ports || (known_bank && used>=2))
            throw std::logic_error("SPR load bank or load-port overbooked");
        ++counts.total;
        if (known_bank) ++used;
    }
    void prune(std::uint64_t lower_bound) {
        cycles_.erase(cycles_.begin(),cycles_.lower_bound(lower_bound));
    }
    void clear() { cycles_.clear(); }
    std::size_t live_cycles() const { return cycles_.size(); }
  private:
    struct Cycle {std::array<std::uint8_t,16> bank{};std::uint32_t total=0;};
    std::map<std::uint64_t,Cycle> cycles_;
};
}
