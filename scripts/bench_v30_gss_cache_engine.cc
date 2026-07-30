// Microbenchmark for the v30 cache-only GSS production data layout.
//
// It measures one pre-access probe + transition through private L1D/private L2
// and shared banked LLC. L1 uses exact last-touch LRU; L2/LLC use binary-tree
// PLRU. The benchmark excludes JSON parsing, Python objects, TLB/MSHR/coherence,
// and transactional overlay bookkeeping.
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <vector>

struct Result {
    bool hit;
    uint32_t position;
    uint32_t residency;
    bool evicted;
};

class FlatCache {
  public:
    FlatCache(uint32_t sets, uint32_t ways, bool tree_plru)
        : sets_(sets), ways_(ways), tree_plru_(tree_plru),
          tags_(uint64_t(sets) * ways, kInvalid),
          last_(uint64_t(sets) * ways, 0),
          tree_(tree_plru ? uint64_t(sets) * (ways - 1) : 0, 0) {}

    Result access(uint32_t set, uint64_t tag, uint64_t sequence) {
        const uint64_t base = uint64_t(set) * ways_;
        uint32_t residency = 0;
        int32_t hit_way = -1;
        int32_t free_way = -1;
        for (uint32_t way = 0; way < ways_; ++way) {
            const uint64_t current = tags_[base + way];
            if (current != kInvalid) ++residency;
            else if (free_way < 0) free_way = int32_t(way);
            if (current == tag) hit_way = int32_t(way);
        }
        uint32_t position = ways_;
        if (hit_way >= 0) {
            const uint32_t touched = last_[base + uint32_t(hit_way)];
            position = 0;
            for (uint32_t way = 0; way < ways_; ++way)
                position += last_[base + way] > touched;
        }
        uint32_t way = 0;
        bool evicted = false;
        if (hit_way >= 0) {
            way = uint32_t(hit_way);
        } else if (free_way >= 0) {
            way = uint32_t(free_way);
        } else {
            evicted = true;
            way = tree_plru_ ? treeVictim(set) : lruVictim(base);
        }
        tags_[base + way] = tag;
        last_[base + way] = uint32_t(sequence);
        if (tree_plru_) treeTouch(set, way);
        return {hit_way >= 0, position, residency, evicted};
    }

  private:
    static constexpr uint64_t kInvalid = std::numeric_limits<uint64_t>::max();
    uint32_t sets_;
    uint32_t ways_;
    bool tree_plru_;
    std::vector<uint64_t> tags_;
    std::vector<uint32_t> last_;
    std::vector<uint8_t> tree_;

    uint32_t lruVictim(uint64_t base) const {
        uint32_t victim = 0;
        for (uint32_t way = 1; way < ways_; ++way)
            if (last_[base + way] < last_[base + victim]) victim = way;
        return victim;
    }

    uint32_t treeVictim(uint32_t set) const {
        const uint64_t base = uint64_t(set) * (ways_ - 1);
        uint32_t node = 0, way = 0, span = ways_;
        while (span > 1) {
            const uint32_t direction = tree_[base + node];
            way = way * 2 + direction;
            node = node * 2 + 1 + direction;
            span /= 2;
        }
        return way;
    }

    void treeTouch(uint32_t set, uint32_t way) {
        const uint64_t base = uint64_t(set) * (ways_ - 1);
        uint32_t node = 0, low = 0, span = ways_;
        while (span > 1) {
            const uint32_t half = span / 2;
            const uint32_t direction = way < low + half ? 0 : 1;
            tree_[base + node] = uint8_t(1 - direction);
            node = node * 2 + 1 + direction;
            if (direction) low += half;
            span = half;
        }
    }
};

static uint64_t xorshift64(uint64_t &state) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return state;
}

int main(int argc, char **argv) {
    const uint64_t events = argc > 1 ? std::strtoull(argv[1], nullptr, 10) : 5000000;
    const uint32_t cores = argc > 2 ? uint32_t(std::strtoul(argv[2], nullptr, 10)) : 32;
    const uint64_t working_set_lines = argc > 3
        ? std::strtoull(argv[3], nullptr, 10) : 2 * 1024 * 1024;
    std::vector<FlatCache> l1;
    std::vector<FlatCache> l2;
    l1.reserve(cores);
    l2.reserve(cores);
    for (uint32_t core = 0; core < cores; ++core) {
        l1.emplace_back(64, 8, false);
        l2.emplace_back(2048, 8, true);
    }
    FlatCache llc(8 * 8192, 16, true);
    uint64_t rng = 0x9e3779b97f4a7c15ULL;
    uint64_t checksum = 0;
    const auto begin = std::chrono::steady_clock::now();
    for (uint64_t event = 1; event <= events; ++event) {
        const uint64_t random = xorshift64(rng);
        const uint32_t core = uint32_t((random >> 32) % cores);
        // Mix a hot component and a capacity-pressure component.
        const uint64_t line = ((random & 7) == 0)
            ? ((random >> 8) & 0x3fff)
            : ((random >> 8) % working_set_lines);
        const uint32_t l1_set = uint32_t(line & 63);
        const uint32_t l2_set = uint32_t(line & 2047);
        const uint32_t bank = uint32_t(line & 7);
        const uint32_t llc_set = uint32_t((line >> 3) & 8191);
        const Result a = l1[core].access(l1_set, line, event);
        const Result b = l2[core].access(l2_set, line, event);
        const Result c = llc.access(bank * 8192 + llc_set, line, event);
        checksum += uint64_t(a.hit) + 3 * uint64_t(b.hit) + 7 * uint64_t(c.hit)
                  + a.position + b.position + c.position
                  + uint64_t(a.evicted) + uint64_t(b.evicted) + uint64_t(c.evicted);
    }
    const auto end = std::chrono::steady_clock::now();
    const double seconds = std::chrono::duration<double>(end - begin).count();
    const double per_event_us = seconds * 1e6 / double(events);
    std::cout << "{\"events\":" << events
              << ",\"cores\":" << cores
              << ",\"working_set_lines\":" << working_set_lines
              << ",\"seconds\":" << seconds
              << ",\"events_per_second\":" << double(events) / seconds
              << ",\"microseconds_per_event\":" << per_event_us
              << ",\"checksum\":" << checksum << "}\n";
    return 0;
}
