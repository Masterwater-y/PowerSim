#include "fastsim/spr_core_resources.hpp"
#include <algorithm>
#include <limits>
#include <stdexcept>

namespace fastsim {
namespace {
constexpr std::size_t index(SprIqDomain d) { return static_cast<std::size_t>(d); }
bool banked(const SprLoadBankRequest& r) { return r.cacheable_ordinary_load && !r.strictly_ordered && !r.prefetch; }
int scan(std::uint16_t candidates, std::uint16_t eligible, unsigned start) {
    for (unsigned i = 0; i < 12; ++i) {
        auto p = (start + i) % 12;
        if ((candidates & eligible & (1u << p)) != 0) return static_cast<int>(p);
    }
    return -1;
}
}
SprPartitionedIq::SprPartitionedIq(SprIqProfile profile) : profile_(profile) {
    std::size_t total = 0;
    for (auto n : profile.capacity) {
        if (n > kGroups - total) throw std::invalid_argument("SPR IQ capacity exceeds bounded calendar");
        total += n;
    }
}
SprIqDomain SprPartitionedIq::domain_for(std::string_view op) {
    static constexpr std::string_view memory[] = {
        "MemRead", "MemWrite", "FloatMemRead", "FloatMemWrite", "X86StoreAddress",
        "SimdUnitStrideLoad", "SimdUnitStrideStore", "SimdUnitStrideMaskLoad", "SimdUnitStrideMaskStore",
        "SimdUnitStrideSegmentedLoad", "SimdUnitStrideSegmentedStore", "SimdStridedLoad", "SimdStridedStore",
        "SimdIndexedLoad", "SimdIndexedStore", "SimdUnitStrideFaultOnlyFirstLoad",
        "SimdUnitStrideSegmentedFaultOnlyFirstLoad", "SimdWholeRegisterLoad", "SimdWholeRegisterStore",
        "SimdStrideSegmentedLoad", "SimdStrideSegmentedStore"};
    if (op == "X86StoreData") return SprIqDomain::StoreData;
    if (std::find(std::begin(memory), std::end(memory), op) != std::end(memory)) return SprIqDomain::Memory;
    return SprIqDomain::Execution;
}
std::optional<SprIqHandle> SprPartitionedIq::insert_member(std::size_t group, bool memory) {
    for (std::size_t i = 0; i < members_.size(); ++i) {
        auto& m = members_[i];
        if (!m.active && m.generation != std::numeric_limits<std::uint64_t>::max()) {
            ++m.generation; m.active = true; m.scheduled = false; m.memory = memory; m.group = static_cast<std::uint16_t>(group);
            ++groups_[group].members;
            return SprIqHandle{static_cast<std::uint16_t>(i), m.generation};
        }
    }
    return std::nullopt;
}
std::optional<SprIqHandle> SprPartitionedIq::acquire(std::string_view op, bool capable, bool memory) {
    const auto domain = domain_for(op); const auto d = index(domain);
    if ((!capable && op != "No_OpClass") || used_[d] >= profile_.capacity[d]) return std::nullopt;
    for (std::size_t i = 0; i < groups_.size(); ++i) if (!groups_[i].active) {
        auto h = insert_member(i, memory);
        if (!h) return std::nullopt;
        groups_[i].active = true; groups_[i].domain = domain; ++used_[d]; return h;
    }
    return std::nullopt;
}
std::optional<SprIqHandle> SprPartitionedIq::join(SprIqHandle h, std::string_view op, bool memory) {
    if (h.slot >= members_.size()) return std::nullopt;
    const auto& m = members_[h.slot];
    if (!m.active || m.generation != h.generation || groups_[m.group].domain != domain_for(op)) return std::nullopt;
    return insert_member(m.group, memory);
}
bool SprPartitionedIq::release(SprIqHandle h, SprIqRelease boundary) {
    if (h.slot >= members_.size()) return false;
    auto& m = members_[h.slot];
    if (!m.active || m.generation != h.generation || (m.memory ? boundary != SprIqRelease::Complete : boundary != SprIqRelease::Issue)) return false;
    auto& g = groups_[m.group]; m.active = false; m.scheduled = false;
    if (--g.members == 0) { --used_[index(g.domain)]; g.active = false; }
    return true;
}
bool SprPartitionedIq::schedule_release(SprIqHandle h, SprIqRelease boundary, std::uint64_t cycle) {
    if (h.slot >= members_.size() || (advanced_ && cycle < cycle_)) return false;
    auto& m = members_[h.slot];
    if (!m.active || m.generation != h.generation || m.scheduled ||
        (m.memory ? boundary != SprIqRelease::Complete : boundary != SprIqRelease::Issue)) return false;
    m.scheduled = true; m.release_cycle = cycle; return true;
}
void SprPartitionedIq::advance(std::uint64_t cycle) {
    if (advanced_ && cycle < cycle_) throw std::invalid_argument("SPR IQ calendar cannot move backwards");
    advanced_ = true; cycle_ = cycle;
    for (std::size_t i = 0; i < members_.size(); ++i) {
        auto& m = members_[i];
        if (m.active && m.scheduled && m.release_cycle <= cycle)
            release(SprIqHandle{static_cast<std::uint16_t>(i), m.generation}, m.memory ? SprIqRelease::Complete : SprIqRelease::Issue);
    }
}
std::size_t SprPartitionedIq::used(SprIqDomain d) const { return used_[index(d)]; }
int SprPortRoundRobin::probe(unsigned family, std::uint16_t candidates, std::uint16_t eligible) const {
    if (family >= cursors_.size() || candidates >= 4096) return -1;
    return scan(candidates, eligible, cursors_[family][candidates]);
}
bool SprPortRoundRobin::commit(unsigned family, std::uint16_t candidates, unsigned port) {
    if (family >= cursors_.size() || candidates >= 4096 || port >= 12 || !(candidates & (1u << port))) return false;
    cursors_[family][candidates] = static_cast<std::uint8_t>((port + 1) % 12); return true;
}
int SprGenericFuRing::attempt(std::uint16_t candidates, std::uint16_t eligible) {
    const int first = scan(candidates, candidates, cursor_);
    if (first < 0) return -1;
    const int selected = scan(candidates, eligible, cursor_);
    // Native getFU() fetches the first entry again to detect a complete
    // all-busy loop, so both cases advance past the last fetched entry.
    const unsigned last = selected < 0 ? static_cast<unsigned>(first)
                                       : static_cast<unsigned>(selected);
    cursor_ = static_cast<std::uint8_t>((last + 1) % 12);
    return selected;
}
int SprGenericFuRing::probe(std::uint16_t candidates, std::uint16_t eligible) const { return scan(candidates, eligible, cursor_); }
bool SprGenericFuRing::commit(std::uint16_t candidates, unsigned unit) {
    if (unit >= 12 || !(candidates & (1u << unit))) return false;
    cursor_ = static_cast<std::uint8_t>((unit + 1) % 12); return true;
}
bool SprLoadBanks::available(std::uint64_t cycle, const SprLoadBankRequest& r) const {
    if (!banked(r)) return true;
    const auto& b = banks_[(r.paddr >> 2) & 15]; return !b.valid || b.cycle != cycle || b.uses < 2;
}
bool SprLoadBanks::note_send(std::uint64_t cycle, const SprLoadBankRequest& r, bool sent) {
    if (!sent || !banked(r)) return true;
    if (!available(cycle, r)) return false;
    auto& b = banks_[(r.paddr >> 2) & 15];
    if (!b.valid || b.cycle != cycle) { b.valid = true; b.cycle = cycle; b.uses = 0; }
    ++b.uses; return true;
}
}
