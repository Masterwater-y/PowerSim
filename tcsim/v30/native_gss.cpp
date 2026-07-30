#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>
#include <unordered_map>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace {

constexpr int kCategoricalFields = 9;
constexpr int kContinuousFields = 10;

struct Probe {
    bool hit;
    int position;
    int residency;
};

struct SetState {
    std::vector<int64_t> tags;
    std::vector<int64_t> last;
    std::vector<uint8_t> tree;
};

template <class Cache>
int cache_access(Cache &cache, int set, int64_t tag, int64_t sequence) {
    return cache.access(set, tag, sequence);
}

class FlatCache {
  public:
    FlatCache(int sets, int ways, bool tree_plru)
        : sets_(sets), ways_(ways), tree_plru_(tree_plru),
          tags_(size_t(sets) * ways, -1), last_(size_t(sets) * ways, -1),
          tree_(tree_plru ? size_t(sets) * (ways - 1) : 0, 0) {}

    Probe peek(int set, int64_t tag) const {
        check_set(set);
        const size_t base = size_t(set) * ways_;
        int hit_way = -1;
        int residency = 0;
        for (int way = 0; way < ways_; ++way) {
            const int64_t current = tags_[base + way];
            residency += current >= 0;
            if (current == tag) hit_way = way;
        }
        if (hit_way < 0) return {false, ways_, residency};
        const int64_t timestamp = last_[base + hit_way];
        int position = 0;
        for (int way = 0; way < ways_; ++way) {
            position += tags_[base + way] >= 0 && last_[base + way] > timestamp;
        }
        return {true, position, residency};
    }

    int access(int set, int64_t tag, int64_t sequence) {
        check_set(set);
        const size_t base = size_t(set) * ways_;
        int way = -1;
        for (int index = 0; index < ways_; ++index) {
            if (tags_[base + index] == tag) {
                way = index;
                break;
            }
        }
        int64_t evicted = -1;
        if (way < 0) {
            for (int index = 0; index < ways_; ++index) {
                if (tags_[base + index] < 0) {
                    way = index;
                    ++occupancy_;
                    break;
                }
            }
        }
        if (way < 0) {
            way = tree_plru_ ? tree_victim(set) : lru_victim(base);
            evicted = tags_[base + way];
        }
        tags_[base + way] = tag;
        last_[base + way] = sequence;
        if (tree_plru_) tree_touch(set, way);
        return evicted >= 0 ? int(evicted != -1) : -1;
    }

    SetState copy_set(int set) const {
        check_set(set);
        const size_t base = size_t(set) * ways_;
        SetState result;
        result.tags.assign(tags_.begin() + base, tags_.begin() + base + ways_);
        result.last.assign(last_.begin() + base, last_.begin() + base + ways_);
        if (tree_plru_) {
            const size_t tree_base = size_t(set) * (ways_ - 1);
            result.tree.assign(
                tree_.begin() + tree_base,
                tree_.begin() + tree_base + ways_ - 1
            );
        }
        return result;
    }

    int sets() const { return sets_; }
    int ways() const { return ways_; }
    bool tree_plru() const { return tree_plru_; }
    int64_t occupancy() const { return occupancy_; }

  private:
    void check_set(int set) const {
        if (set < 0 || set >= sets_) throw std::out_of_range("GSS cache set out of range");
    }

    int lru_victim(size_t base) const {
        int victim = 0;
        for (int way = 1; way < ways_; ++way) {
            if (last_[base + way] < last_[base + victim]) victim = way;
        }
        return victim;
    }

    int tree_victim(int set) const {
        const size_t base = size_t(set) * (ways_ - 1);
        int node = 0;
        int way = 0;
        int span = ways_;
        while (span > 1) {
            const int direction = tree_[base + node];
            way = way * 2 + direction;
            node = node * 2 + 1 + direction;
            span /= 2;
        }
        return way;
    }

    void tree_touch(int set, int way) {
        const size_t base = size_t(set) * (ways_ - 1);
        int node = 0;
        int low = 0;
        int span = ways_;
        while (span > 1) {
            const int half = span / 2;
            const int direction = way < low + half ? 0 : 1;
            tree_[base + node] = uint8_t(1 - direction);
            node = node * 2 + 1 + direction;
            if (direction) low += half;
            span = half;
        }
    }

    int sets_;
    int ways_;
    bool tree_plru_;
    std::vector<int64_t> tags_;
    std::vector<int64_t> last_;
    std::vector<uint8_t> tree_;
    int64_t occupancy_ = 0;
};

class ShadowCache {
  public:
    ShadowCache(int sets, int ways, bool tree_plru, const FlatCache *parent)
        : sets_(sets), ways_(ways), tree_plru_(tree_plru), parent_(parent),
          occupancy_(parent ? parent->occupancy() : 0) {}

    Probe peek(int set, int64_t tag) const {
        check_set(set);
        const auto found = sets_state_.find(set);
        if (found == sets_state_.end()) {
            if (parent_) return parent_->peek(set, tag);
            return {false, ways_, 0};
        }
        return peek_state(found->second, tag);
    }

    int access(int set, int64_t tag, int64_t sequence) {
        SetState &state = mutable_set(set);
        int way = -1;
        for (int index = 0; index < ways_; ++index) {
            if (state.tags[index] == tag) {
                way = index;
                break;
            }
        }
        int64_t evicted = -1;
        if (way < 0) {
            for (int index = 0; index < ways_; ++index) {
                if (state.tags[index] < 0) {
                    way = index;
                    ++occupancy_;
                    break;
                }
            }
        }
        if (way < 0) {
            way = tree_plru_ ? tree_victim(state) : lru_victim(state);
            evicted = state.tags[way];
        }
        state.tags[way] = tag;
        state.last[way] = sequence;
        if (tree_plru_) tree_touch(state, way);
        return evicted >= 0 ? int(evicted != -1) : -1;
    }

    int64_t occupancy() const { return occupancy_; }

  private:
    void check_set(int set) const {
        if (set < 0 || set >= sets_) throw std::out_of_range("GSS cache set out of range");
    }

    SetState &mutable_set(int set) {
        check_set(set);
        auto found = sets_state_.find(set);
        if (found != sets_state_.end()) return found->second;
        SetState state;
        if (parent_) {
            state = parent_->copy_set(set);
        } else {
            state.tags.assign(ways_, -1);
            state.last.assign(ways_, -1);
            if (tree_plru_) state.tree.assign(ways_ - 1, 0);
        }
        return sets_state_.emplace(set, std::move(state)).first->second;
    }

    Probe peek_state(const SetState &state, int64_t tag) const {
        int hit_way = -1;
        int residency = 0;
        for (int way = 0; way < ways_; ++way) {
            residency += state.tags[way] >= 0;
            if (state.tags[way] == tag) hit_way = way;
        }
        if (hit_way < 0) return {false, ways_, residency};
        const int64_t timestamp = state.last[hit_way];
        int position = 0;
        for (int way = 0; way < ways_; ++way) {
            position += state.tags[way] >= 0 && state.last[way] > timestamp;
        }
        return {true, position, residency};
    }

    int lru_victim(const SetState &state) const {
        int victim = 0;
        for (int way = 1; way < ways_; ++way) {
            if (state.last[way] < state.last[victim]) victim = way;
        }
        return victim;
    }

    int tree_victim(const SetState &state) const {
        int node = 0;
        int way = 0;
        int span = ways_;
        while (span > 1) {
            const int direction = state.tree[node];
            way = way * 2 + direction;
            node = node * 2 + 1 + direction;
            span /= 2;
        }
        return way;
    }

    void tree_touch(SetState &state, int way) {
        int node = 0;
        int low = 0;
        int span = ways_;
        while (span > 1) {
            const int half = span / 2;
            const int direction = way < low + half ? 0 : 1;
            state.tree[node] = uint8_t(1 - direction);
            node = node * 2 + 1 + direction;
            if (direction) low += half;
            span = half;
        }
    }

    int sets_;
    int ways_;
    bool tree_plru_;
    const FlatCache *parent_;
    std::unordered_map<int, SetState> sets_state_;
    int64_t occupancy_;
};

struct CoreSummary {
    double l1_miss_ema = 0.0;
    double l2_miss_ema = 0.0;
    double llc_miss_ema = 0.0;
    double eviction_ema = 0.0;
    int64_t llc_miss_run = 0;
};

struct LineInfo {
    int core;
    int64_t event;
};

struct Geometry {
    int l1_sets;
    int l1_ways;
    int l2_sets;
    int l2_ways;
    int llc_sets_per_bank;
    int llc_ways;
    int llc_banks;
};

struct MissCounters {
    int64_t l1d_load = 0;
    int64_t l1d_store = 0;
    int64_t l2_load = 0;
    int64_t l2_store = 0;
    int64_t llc = 0;
};

struct CanonicalCore {
    explicit CanonicalCore(const Geometry &g)
        : l1(g.l1_sets, g.l1_ways, false),
          l2(g.l2_sets, g.l2_ways, true) {}
    FlatCache l1;
    FlatCache l2;
};

class CanonicalState {
  public:
    explicit CanonicalState(const Geometry &geometry)
        : geometry(geometry),
          llc_cache(geometry.llc_sets_per_bank * geometry.llc_banks,
                    geometry.llc_ways, true),
          bank_occupancy_values(geometry.llc_banks, 0) {}

    FlatCache &l1(int core) { return private_core(core).l1; }
    FlatCache &l2(int core) { return private_core(core).l2; }
    FlatCache &llc() { return llc_cache; }
    CoreSummary &summary(int core) { return summaries[core]; }
    bool line_info(int64_t line, LineInfo &out) const {
        const auto found = lines.find(line);
        if (found == lines.end()) return false;
        out = found->second;
        return true;
    }
    void write_line(int64_t line, LineInfo value) { lines[line] = value; }
    int64_t &event_count() { return events; }
    int64_t &unique_lines() { return unique; }
    int64_t &bank_occupancy(int bank) { return bank_occupancy_values.at(bank); }
    MissCounters &miss_counters() { return misses; }

    CanonicalCore &private_core(int core) {
        auto found = cores.find(core);
        if (found == cores.end()) {
            found = cores.emplace(core, std::make_unique<CanonicalCore>(geometry)).first;
        }
        return *found->second;
    }

    const Geometry geometry;
    std::unordered_map<int, std::unique_ptr<CanonicalCore>> cores;
    FlatCache llc_cache;
    std::unordered_map<int, CoreSummary> summaries;
    std::unordered_map<int64_t, LineInfo> lines;
    std::vector<int64_t> bank_occupancy_values;
    int64_t events = 0;
    int64_t unique = 0;
    MissCounters misses;
};

struct ShadowCore {
    ShadowCore(const Geometry &g, const CanonicalCore *parent)
        : l1(g.l1_sets, g.l1_ways, false, parent ? &parent->l1 : nullptr),
          l2(g.l2_sets, g.l2_ways, true, parent ? &parent->l2 : nullptr) {}
    ShadowCache l1;
    ShadowCache l2;
};

class ShadowState {
  public:
    explicit ShadowState(CanonicalState &canonical)
        : parent(canonical), geometry(canonical.geometry),
          llc_cache(geometry.llc_sets_per_bank * geometry.llc_banks,
                    geometry.llc_ways, true, &canonical.llc_cache),
          bank_occupancy_values(canonical.bank_occupancy_values),
          events(canonical.events), unique(canonical.unique),
          misses(canonical.misses) {}

    ShadowCache &l1(int core) { return private_core(core).l1; }
    ShadowCache &l2(int core) { return private_core(core).l2; }
    ShadowCache &llc() { return llc_cache; }
    CoreSummary &summary(int core) {
        auto found = summaries.find(core);
        if (found == summaries.end()) {
            const auto source = parent.summaries.find(core);
            found = summaries.emplace(
                core, source == parent.summaries.end() ? CoreSummary{} : source->second
            ).first;
        }
        return found->second;
    }
    bool line_info(int64_t line, LineInfo &out) const {
        const auto local = lines.find(line);
        if (local != lines.end()) {
            out = local->second;
            return true;
        }
        return parent.line_info(line, out);
    }
    void write_line(int64_t line, LineInfo value) { lines[line] = value; }
    int64_t &event_count() { return events; }
    int64_t &unique_lines() { return unique; }
    int64_t &bank_occupancy(int bank) { return bank_occupancy_values.at(bank); }
    MissCounters &miss_counters() { return misses; }

    ShadowCore &private_core(int core) {
        auto found = cores.find(core);
        if (found == cores.end()) {
            const auto source = parent.cores.find(core);
            const CanonicalCore *source_core =
                source == parent.cores.end() ? nullptr : source->second.get();
            found = cores.emplace(
                core, std::make_unique<ShadowCore>(geometry, source_core)
            ).first;
        }
        return *found->second;
    }

    CanonicalState &parent;
    const Geometry geometry;
    std::unordered_map<int, std::unique_ptr<ShadowCore>> cores;
    ShadowCache llc_cache;
    std::unordered_map<int, CoreSummary> summaries;
    std::unordered_map<int64_t, LineInfo> lines;
    std::vector<int64_t> bank_occupancy_values;
    int64_t events;
    int64_t unique;
    MissCounters misses;
};

struct Event {
    int core;
    int64_t line;
    int l1_set;
    int l2_set;
    int llc_set;
    int llc_bank;
    int access_kind;
};

template <class State>
void apply_access(
    State &state,
    const Geometry &geometry,
    const Event &event,
    double ema_alpha,
    int64_t recent_horizon,
    int64_t *categorical,
    float *continuous
) {
    if (event.line < 0) {
        if (categorical) {
            const int64_t invalid[kCategoricalFields] = {
                0, 0, 0, 0, 3, 0, 0, event.access_kind, 0
            };
            std::copy(invalid, invalid + kCategoricalFields, categorical);
        }
        if (continuous) std::fill(continuous, continuous + kContinuousFields, 0.0f);
        return;
    }
    if (event.llc_bank < 0 || event.llc_bank >= geometry.llc_banks) {
        throw std::out_of_range("GSS LLC bank out of range");
    }
    auto &l1 = state.l1(event.core);
    auto &l2 = state.l2(event.core);
    auto &llc = state.llc();
    const int combined = event.llc_bank * geometry.llc_sets_per_bank + event.llc_set;
    const Probe l1_probe = l1.peek(event.l1_set, event.line);
    const Probe l2_probe = l2.peek(event.l2_set, event.line);
    const Probe llc_probe = llc.peek(combined, event.line);
    const int hit_level = l1_probe.hit ? 1 : l2_probe.hit ? 2 : llc_probe.hit ? 3 : 4;
    MissCounters &misses = state.miss_counters();
    const bool store_like = event.access_kind == 2 || event.access_kind == 3;
    if (!l1_probe.hit) {
        if (store_like) ++misses.l1d_store;
        else ++misses.l1d_load;
    }
    if (!l1_probe.hit && !l2_probe.hit) {
        if (store_like) ++misses.l2_store;
        else ++misses.l2_load;
    }
    if (hit_level == 4) ++misses.llc;
    LineInfo previous{};
    const bool seen = state.line_info(event.line, previous);
    const int miss_kind = hit_level < 4 ? 0 : (!seen ? 1 : 2);
    const int other_recent = seen && previous.core != event.core &&
        state.event_count() - previous.event <= recent_horizon;
    CoreSummary &summary = state.summary(event.core);
    const int64_t bank_capacity = int64_t(geometry.llc_sets_per_bank) * geometry.llc_ways;
    const int64_t union_capacity = bank_capacity * geometry.llc_banks;
    if (continuous) {
        continuous[0] = float(double(l1_probe.residency) / geometry.l1_ways);
        continuous[1] = float(double(l2_probe.residency) / geometry.l2_ways);
        continuous[2] = float(double(llc_probe.residency) / geometry.llc_ways);
        continuous[3] = float(summary.l1_miss_ema);
        continuous[4] = float(summary.l2_miss_ema);
        continuous[5] = float(summary.llc_miss_ema);
        continuous[6] = float(std::min(1.0, std::log2(1.0 + summary.llc_miss_run) / 16.0));
        continuous[7] = float(summary.eviction_ema);
        continuous[8] = float(double(state.bank_occupancy(event.llc_bank)) /
                              std::max<int64_t>(1, bank_capacity));
        continuous[9] = float(std::min(
            1.0, double(state.unique_lines()) / std::max<int64_t>(1, union_capacity)
        ));
    }
    const int64_t sequence = ++state.event_count();
    const int evicted_l1 = l1.access(event.l1_set, event.line, sequence);
    const int evicted_l2 = l2.access(event.l2_set, event.line, sequence);
    const int evicted_llc = llc.access(combined, event.line, sequence);
    if (!llc_probe.hit && llc_probe.residency < geometry.llc_ways) {
        ++state.bank_occupancy(event.llc_bank);
    }
    const int eviction_level = evicted_llc >= 0 ? 3 : evicted_l2 >= 0 ? 2 : evicted_l1 >= 0 ? 1 : 0;
    const auto ema = [ema_alpha](double old, bool value) {
        return (1.0 - ema_alpha) * old + ema_alpha * double(value);
    };
    summary.l1_miss_ema = ema(summary.l1_miss_ema, !l1_probe.hit);
    summary.l2_miss_ema = ema(summary.l2_miss_ema, !l2_probe.hit);
    summary.llc_miss_ema = ema(summary.llc_miss_ema, !llc_probe.hit);
    summary.eviction_ema = ema(summary.eviction_ema, eviction_level > 0);
    summary.llc_miss_run = !llc_probe.hit ? summary.llc_miss_run + 1 : 0;
    if (!seen) ++state.unique_lines();
    state.write_line(event.line, {event.core, sequence});
    if (categorical) {
        const int64_t values[kCategoricalFields] = {
            hit_level, l1_probe.position, l2_probe.position, llc_probe.position,
            miss_kind, eviction_level, other_recent, event.access_kind, 1
        };
        std::copy(values, values + kCategoricalFields, categorical);
    }
}

Event read_event(const py::detail::unchecked_reference<int64_t, 2> &events, ssize_t row) {
    return {
        int(events(row, 0)), events(row, 1), int(events(row, 2)),
        int(events(row, 3)), int(events(row, 4)), int(events(row, 5)),
        int(events(row, 6))
    };
}

class NativeGSS {
  public:
    NativeGSS(
        int l1_sets, int l1_ways, int l2_sets, int l2_ways,
        int llc_sets_per_bank, int llc_ways, int llc_banks,
        double ema_alpha = 0.02, int64_t recent_horizon = 4096
    ) : geometry_{l1_sets, l1_ways, l2_sets, l2_ways,
                  llc_sets_per_bank, llc_ways, llc_banks},
        state_(geometry_), ema_alpha_(ema_alpha), recent_horizon_(recent_horizon) {
        if (l1_sets <= 0 || l1_ways <= 0 || l2_sets <= 0 || l2_ways <= 0 ||
            llc_sets_per_bank <= 0 || llc_ways <= 0 || llc_banks <= 0) {
            throw std::invalid_argument("GSS geometry must be positive");
        }
        if ((l2_ways & (l2_ways - 1)) || (llc_ways & (llc_ways - 1))) {
            throw std::invalid_argument("GSS TreePLRU associativity must be power-of-two");
        }
    }

    py::tuple preview(py::array_t<int64_t, py::array::c_style | py::array::forcecast> input) {
        if (input.ndim() != 2 || input.shape(1) != 7) {
            throw std::invalid_argument("GSS events must have shape [N,7]");
        }
        const ssize_t count = input.shape(0);
        py::array_t<int64_t> categorical({count, ssize_t(kCategoricalFields)});
        py::array_t<float> continuous({count, ssize_t(kContinuousFields)});
        const auto events = input.unchecked<2>();
        auto categories = categorical.mutable_unchecked<2>();
        auto values = continuous.mutable_unchecked<2>();
        ShadowState shadow(state_);
        {
            py::gil_scoped_release release;
            for (ssize_t row = 0; row < count; ++row) {
                apply_access(
                    shadow, geometry_, read_event(events, row), ema_alpha_, recent_horizon_,
                    &categories(row, 0), &values(row, 0)
                );
            }
        }
        return py::make_tuple(std::move(categorical), std::move(continuous));
    }

    void commit(py::array_t<int64_t, py::array::c_style | py::array::forcecast> input) {
        if (input.ndim() != 2 || input.shape(1) != 7) {
            throw std::invalid_argument("GSS events must have shape [N,7]");
        }
        const auto events = input.unchecked<2>();
        py::gil_scoped_release release;
        for (ssize_t row = 0; row < input.shape(0); ++row) {
            apply_access(
                state_, geometry_, read_event(events, row), ema_alpha_, recent_horizon_,
                nullptr, nullptr
            );
        }
    }

    py::dict state_summary() const {
        int64_t l1_occupancy = 0;
        int64_t l2_occupancy = 0;
        for (const auto &item : state_.cores) {
            l1_occupancy += item.second->l1.occupancy();
            l2_occupancy += item.second->l2.occupancy();
        }
        py::dict result;
        result["events"] = state_.events;
        result["cores"] = state_.cores.size();
        result["unique_lines"] = state_.unique;
        result["l1_occupancy"] = l1_occupancy;
        result["l2_occupancy"] = l2_occupancy;
        result["llc_occupancy"] = state_.llc_cache.occupancy();
        result["l1d_load_misses"] = state_.misses.l1d_load;
        result["l1d_store_misses"] = state_.misses.l1d_store;
        result["l1d_misses"] = state_.misses.l1d_load + state_.misses.l1d_store;
        result["l2_load_misses"] = state_.misses.l2_load;
        result["l2_store_misses"] = state_.misses.l2_store;
        result["l2_misses"] = state_.misses.l2_load + state_.misses.l2_store;
        result["llc_misses"] = state_.misses.llc;
        return result;
    }

  private:
    Geometry geometry_;
    CanonicalState state_;
    double ema_alpha_;
    int64_t recent_horizon_;
};

}  // namespace

PYBIND11_MODULE(_gss_native, module) {
    module.doc() = "Batched transactional v30 GSS cache engine";
    py::class_<NativeGSS>(module, "NativeGSS")
        .def(py::init<int, int, int, int, int, int, int, double, int64_t>(),
             py::arg("l1_sets"), py::arg("l1_ways"),
             py::arg("l2_sets"), py::arg("l2_ways"),
             py::arg("llc_sets_per_bank"), py::arg("llc_ways"),
             py::arg("llc_banks"), py::arg("ema_alpha") = 0.02,
             py::arg("recent_horizon") = 4096)
        .def("preview", &NativeGSS::preview)
        .def("commit", &NativeGSS::commit)
        .def("state_summary", &NativeGSS::state_summary);
}
