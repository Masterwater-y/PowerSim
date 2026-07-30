#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace py = pybind11;

namespace {

constexpr int kFieldCount = 26;
constexpr int kResourceCount = 10;
constexpr int kCompactCount = 3;
constexpr int kSummaryCount = 38;
constexpr uint32_t kInvalidCompact = 0xffffffffu;

constexpr int kOpClass = 0;
constexpr int kProducerDistance = 3;
constexpr int kReuseDistance = 4;
constexpr int kStride = 5;
constexpr int kBranchKind = 12;
constexpr int kBranchTaken = 13;
constexpr int kL1Pressure = 23;
constexpr int kL2Pressure = 24;
constexpr int kLlcPressure = 25;

constexpr int kPhysicalLine = 0;
constexpr int kL1Set = 1;
constexpr int kL2Set = 2;
constexpr int kLlcBank = 4;
constexpr int kDramChannel = 5;

constexpr int kCompactLlcSet = 0;
constexpr int kCompactDramBank = 1;
constexpr int kCompactDramRow = 2;

template <class Key>
using CountMap = std::unordered_map<Key, int>;

template <class Key>
void increment(CountMap<Key> &counts, Key key) {
    const auto found = counts.find(key);
    if (found == counts.end()) {
        counts.emplace(key, 1);
    } else {
        ++found->second;
    }
}

template <class Key>
double hhi(const CountMap<Key> &counts) {
    int total = 0;
    for (const auto &item : counts) total += item.second;
    if (total <= 0) return 0.0;
    double squares = 0.0;
    for (const auto &item : counts) {
        const double probability = double(item.second) / double(total);
        squares += probability * probability;
    }
    return squares;
}

int pressure_bucket(int count) {
    int bucket = 0;
    while (count > 0) {
        ++bucket;
        count >>= 1;
    }
    return std::min(9, bucket);
}

int fanout_bucket(int count) {
    if (count <= 0) return 1;
    int power = 0;
    int value = count;
    while (value > 0) {
        ++power;
        value >>= 1;
    }
    // bit_width(count) == ceil(log2(count + 1)) for positive integers.
    return 1 + std::min(6, power);
}

int owner_count(uint64_t owners) {
    return __builtin_popcountll(owners);
}

struct LineOwners {
    uint64_t readers = 0;
    uint64_t writers = 0;
    uint64_t accessors = 0;
};

struct Presence {
    std::unordered_map<uint64_t, uint64_t> owners;

    uint64_t mask(uint64_t key) const {
        const auto found = owners.find(key);
        return found == owners.end() ? 0 : found->second;
    }
};

void require_shape(
    const py::buffer_info &info,
    int dimensions,
    py::ssize_t rows,
    py::ssize_t length,
    py::ssize_t width,
    const char *name
);

py::tuple cross_features(
    py::array_t<int64_t, py::array::c_style> resources,
    py::array_t<int64_t, py::array::c_style> lines,
    py::array_t<uint8_t, py::array::c_style> kinds,
    py::array_t<bool, py::array::c_style> valid,
    py::array_t<uint32_t, py::array::c_style> compact,
    int64_t dram_row_radix
) {
    auto resource_info = resources.request();
    if (resource_info.ndim != 3 || resource_info.shape[2] != kResourceCount) {
        throw std::invalid_argument("native cross resources must be [C,K,10]");
    }
    const py::ssize_t rows = resource_info.shape[0];
    const py::ssize_t length = resource_info.shape[1];
    if (rows <= 0 || rows > 64 || dram_row_radix <= 0) {
        throw std::invalid_argument("native cross requires 1..64 cores and a positive row radix");
    }
    require_shape(lines.request(), 2, rows, length, 0, "cross lines");
    require_shape(kinds.request(), 2, rows, length, 0, "cross kinds");
    require_shape(valid.request(), 2, rows, length, 0, "cross valid");
    require_shape(compact.request(), 3, rows, length, kCompactCount, "cross compact");

    py::array_t<int64_t> dynamic({rows, length, py::ssize_t(8)});
    py::array_t<double> relations({rows, py::ssize_t(22)});
    auto dynamic_out = dynamic.mutable_unchecked<3>();
    auto relation_out = relations.mutable_unchecked<2>();
    auto resource = resources.unchecked<3>();
    auto line = lines.unchecked<2>();
    auto kind = kinds.unchecked<2>();
    auto is_valid = valid.unchecked<2>();
    auto compact_value = compact.unchecked<3>();

    std::unordered_map<int64_t, LineOwners> line_owners;
    Presence llc_sets;
    Presence llc_banks;
    Presence channels;
    Presence dram_banks;
    Presence dram_rows;
    line_owners.reserve(size_t(rows * length));
    llc_sets.owners.reserve(size_t(rows * length));
    llc_banks.owners.reserve(size_t(rows * length));
    channels.owners.reserve(size_t(rows * length));
    dram_banks.owners.reserve(size_t(rows * length));
    dram_rows.owners.reserve(size_t(rows * length));
    int total_uops = 0;
    int total_mem = 0;

    for (py::ssize_t core = 0; core < rows; ++core) {
        const uint64_t bit = uint64_t(1) << core;
        for (py::ssize_t token = 0; token < length; ++token) {
            for (int feature = 0; feature < 8; ++feature) {
                dynamic_out(core, token, feature) = is_valid(core, token) ? 0 : 8;
            }
            if (!is_valid(core, token)) continue;
            ++total_uops;
            const uint8_t access_kind = kind(core, token);
            const int64_t physical_line = line(core, token);
            if (physical_line >= 0 && access_kind >= 1 && access_kind <= 3) {
                LineOwners &owners = line_owners[physical_line];
                owners.accessors |= bit;
                if (access_kind == 1 || access_kind == 3) owners.readers |= bit;
                if (access_kind == 2 || access_kind == 3) owners.writers |= bit;
            }
            if (access_kind == 0) continue;
            ++total_mem;
            const uint32_t llc_set = compact_value(core, token, kCompactLlcSet);
            const int64_t llc_bank = resource(core, token, kLlcBank);
            const int64_t channel = resource(core, token, kDramChannel);
            const uint32_t dram_bank = compact_value(core, token, kCompactDramBank);
            const uint32_t dram_row = compact_value(core, token, kCompactDramRow);
            if (llc_set != kInvalidCompact) llc_sets.owners[llc_set] |= bit;
            if (llc_bank >= 0) llc_banks.owners[uint64_t(llc_bank)] |= bit;
            if (channel >= 0) channels.owners[uint64_t(channel)] |= bit;
            if (dram_bank != kInvalidCompact && resource(core, token, 8) >= 0) {
                dram_banks.owners[dram_bank] |= bit;
            }
            if (dram_row != kInvalidCompact) dram_rows.owners[dram_row] |= bit;
        }
    }

    std::vector<std::unordered_map<uint64_t, int>> bank_row_counts(
        static_cast<size_t>(rows)
    );
    for (const auto &item : dram_rows.owners) {
        const uint64_t bank = item.first / uint64_t(dram_row_radix);
        if (dram_banks.owners.find(bank) == dram_banks.owners.end()) {
            throw std::runtime_error("native DRAM row key has no matching bank key");
        }
        uint64_t owners = item.second;
        while (owners) {
            const int core = __builtin_ctzll(owners);
            ++bank_row_counts[size_t(core)][bank];
            owners &= owners - 1;
        }
    }
    auto row_conflicts = [&](int core, uint64_t row_key) {
        const uint64_t bit = uint64_t(1) << core;
        const uint64_t bank = row_key / uint64_t(dram_row_radix);
        const uint64_t bank_mask = dram_banks.mask(bank) & ~bit;
        const uint64_t row_mask = dram_rows.mask(row_key);
        int conflicts = 0;
        uint64_t others = bank_mask;
        while (others) {
            const int other = __builtin_ctzll(others);
            const auto found = bank_row_counts[size_t(other)].find(bank);
            const int count = found == bank_row_counts[size_t(other)].end() ? 0 : found->second;
            if (count > 1 || (row_mask & (uint64_t(1) << other)) == 0) ++conflicts;
            others &= others - 1;
        }
        return conflicts;
    };

    const int fanout_den = std::max(1, int(rows) - 1);
    for (py::ssize_t core = 0; core < rows; ++core) {
        const uint64_t bit = uint64_t(1) << core;
        int own_access = 0;
        int own_read = 0;
        int own_write = 0;
        int shared_access = 0;
        int read_after_write = 0;
        int write_other_access = 0;
        int multiwriter = 0;
        int shared_read = 0;
        int shared_write = 0;
        int other_reader_sum = 0;
        int other_writer_sum = 0;
        int max_other_access = 0;
        int max_writer_coverage = 0;
        for (const auto &item : line_owners) {
            const LineOwners &owners = item.second;
            const bool access = (owners.accessors & bit) != 0;
            const bool read = (owners.readers & bit) != 0;
            const bool write = (owners.writers & bit) != 0;
            if (access) {
                ++own_access;
                const int access_count = owner_count(owners.accessors);
                const int readers = owner_count(owners.readers & ~bit);
                const int writers = owner_count(owners.writers & ~bit);
                shared_access += access_count > 1;
                other_reader_sum += readers;
                other_writer_sum += writers;
                max_other_access = std::max(max_other_access, access_count - 1);
            }
            if (read) {
                ++own_read;
                read_after_write += (owners.writers & ~bit) != 0;
                shared_read += owner_count(owners.accessors) > 1;
            }
            if (write) {
                ++own_write;
                write_other_access += owner_count(owners.accessors) > 1;
                multiwriter += owner_count(owners.writers) > 1;
                shared_write += owner_count(owners.accessors) > 1;
                max_writer_coverage = std::max(max_writer_coverage, owner_count(owners.writers));
            }
        }
        auto resource_stats = [&](const Presence &presence) {
            std::array<double, 3> result{};
            for (const auto &item : presence.owners) {
                if ((item.second & bit) == 0) continue;
                result[0] += 1.0;
                result[1] += owner_count(item.second) > 1;
                result[2] += owner_count(item.second) - 1;
            }
            return result;
        };
        const auto llc_set_stats = resource_stats(llc_sets);
        const auto llc_bank_stats = resource_stats(llc_banks);
        const auto channel_stats = resource_stats(channels);
        const auto dram_bank_stats = resource_stats(dram_banks);
        const auto dram_row_stats = resource_stats(dram_rows);
        int conflict_rows = 0;
        for (const auto &item : dram_rows.owners) {
            if ((item.second & bit) != 0) conflict_rows += row_conflicts(int(core), item.first) > 0;
        }
        const double access_den = double(std::max(1, own_access));
        const double read_den = double(std::max(1, own_read));
        const double write_den = double(std::max(1, own_write));
        int index = 0;
        relation_out(core, index++) = std::log1p(double(rows)) / 4.0;
        relation_out(core, index++) = double(shared_access) / access_den;
        relation_out(core, index++) = double(read_after_write) / read_den;
        relation_out(core, index++) = double(write_other_access) / write_den;
        relation_out(core, index++) = double(multiwriter) / write_den;
        relation_out(core, index++) = double(shared_read) / read_den;
        relation_out(core, index++) = double(shared_write) / write_den;
        relation_out(core, index++) = double(other_reader_sum) / access_den / fanout_den;
        relation_out(core, index++) = double(other_writer_sum) / access_den / fanout_den;
        relation_out(core, index++) = double(max_other_access) / fanout_den;
        relation_out(core, index++) = double(max_writer_coverage) / std::max<py::ssize_t>(1, rows);
        relation_out(core, index++) = std::log1p(1000.0 * line_owners.size() / std::max(1, total_uops)) / 8.0;
        relation_out(core, index++) = double(own_access) / std::max<size_t>(1, line_owners.size());
        relation_out(core, index++) = double(total_mem) / std::max(1, total_uops);
        relation_out(core, index++) = llc_set_stats[1] / std::max(1.0, llc_set_stats[0]);
        relation_out(core, index++) = llc_bank_stats[1] / std::max(1.0, llc_bank_stats[0]);
        relation_out(core, index++) = channel_stats[1] / std::max(1.0, channel_stats[0]);
        relation_out(core, index++) = dram_bank_stats[1] / std::max(1.0, dram_bank_stats[0]);
        relation_out(core, index++) = dram_row_stats[1] / std::max(1.0, dram_row_stats[0]);
        relation_out(core, index++) = double(conflict_rows) / std::max(1.0, dram_row_stats[0]);
        relation_out(core, index++) = llc_set_stats[2] / std::max(1.0, llc_set_stats[0]) / fanout_den;
        relation_out(core, index++) = dram_bank_stats[2] / std::max(1.0, dram_bank_stats[0]) / fanout_den;
        if (index != 22) throw std::runtime_error("native relation dimension drift");
    }

    for (py::ssize_t core = 0; core < rows; ++core) {
        const uint64_t bit = uint64_t(1) << core;
        for (py::ssize_t token = 0; token < length; ++token) {
            if (!is_valid(core, token)) continue;
            const uint8_t access_kind = kind(core, token);
            const int64_t physical_line = line(core, token);
            if (physical_line < 0 || access_kind < 1 || access_kind > 3) continue;
            const LineOwners &owners = line_owners.at(physical_line);
            const int other_read = owner_count(owners.readers & ~bit);
            const int other_write = owner_count(owners.writers & ~bit);
            int role = 0;
            if (access_kind == 1) {
                role = other_write > 0 ? 4 : (other_read > 0 ? 2 : 1);
            } else {
                role = other_write > 0 ? 6 : (other_read > 0 ? 5 : 3);
            }
            const uint32_t llc_set = compact_value(core, token, kCompactLlcSet);
            const int64_t llc_bank = resource(core, token, kLlcBank);
            const int64_t channel = resource(core, token, kDramChannel);
            const uint32_t dram_bank = compact_value(core, token, kCompactDramBank);
            const uint32_t dram_row = compact_value(core, token, kCompactDramRow);
            const int line_other = owner_count(owners.accessors & ~bit);
            const int llc_set_other = llc_set == kInvalidCompact ? 0 : owner_count(llc_sets.mask(llc_set) & ~bit);
            const int llc_bank_other = llc_bank < 0 ? 0 : owner_count(llc_banks.mask(uint64_t(llc_bank)) & ~bit);
            const int channel_other = channel < 0 ? 0 : owner_count(channels.mask(uint64_t(channel)) & ~bit);
            const int dram_bank_other = dram_bank == kInvalidCompact ? 0 : owner_count(dram_banks.mask(dram_bank) & ~bit);
            const int same_row = dram_row == kInvalidCompact ? 0 : owner_count(dram_rows.mask(dram_row) & ~bit);
            const int conflicts = dram_row == kInvalidCompact ? dram_bank_other : row_conflicts(int(core), dram_row);
            dynamic_out(core, token, 0) = role;
            dynamic_out(core, token, 1) = fanout_bucket(line_other);
            dynamic_out(core, token, 2) = fanout_bucket(llc_set_other);
            dynamic_out(core, token, 3) = fanout_bucket(llc_bank_other);
            dynamic_out(core, token, 4) = fanout_bucket(channel_other);
            dynamic_out(core, token, 5) = fanout_bucket(dram_bank_other);
            dynamic_out(core, token, 6) = fanout_bucket(same_row);
            dynamic_out(core, token, 7) = fanout_bucket(conflicts);
        }
    }
    return py::make_tuple(dynamic, relations);
}

void require_shape(
    const py::buffer_info &info,
    int dimensions,
    py::ssize_t rows,
    py::ssize_t length,
    py::ssize_t width,
    const char *name
) {
    if (info.ndim != dimensions || info.shape[0] != rows ||
        info.shape[1] != length ||
        (dimensions == 3 && info.shape[2] != width)) {
        throw std::invalid_argument(std::string("native context shape mismatch: ") + name);
    }
}

py::array_t<double> pressure_summary(
    py::array_t<int64_t, py::array::c_style> fields,
    py::array_t<int64_t, py::array::c_style> resources,
    py::array_t<uint32_t, py::array::c_style> compact,
    py::array_t<bool, py::array::c_style> valid,
    py::array_t<uint8_t, py::array::c_style> semantic,
    py::array_t<int64_t, py::array::c_style> functional_line,
    py::array_t<int64_t, py::array::c_style> functional_page,
    py::array_t<float, py::array::c_style> producer_log,
    py::array_t<uint32_t, py::array::c_style> macro_id,
    py::array_t<bool, py::array::c_style> macro_end
) {
    auto field_info = fields.request();
    if (field_info.ndim != 3 || field_info.shape[2] != kFieldCount) {
        throw std::invalid_argument("native context fields must be [C,K,26]");
    }
    const py::ssize_t rows = field_info.shape[0];
    const py::ssize_t length = field_info.shape[1];
    require_shape(resources.request(), 3, rows, length, kResourceCount, "resources");
    require_shape(compact.request(), 3, rows, length, kCompactCount, "compact");
    require_shape(valid.request(), 2, rows, length, 0, "valid");
    require_shape(semantic.request(), 2, rows, length, 0, "semantic");
    require_shape(functional_line.request(), 2, rows, length, 0, "functional_line");
    require_shape(functional_page.request(), 2, rows, length, 0, "functional_page");
    require_shape(producer_log.request(), 2, rows, length, 0, "producer_log");
    require_shape(macro_id.request(), 2, rows, length, 0, "macro_id");
    require_shape(macro_end.request(), 2, rows, length, 0, "macro_end");
    if (!fields.writeable()) {
        throw std::invalid_argument("native context fields must be writeable");
    }

    py::array_t<double> output({rows, py::ssize_t(kSummaryCount)});
    auto out = output.mutable_unchecked<2>();
    auto field = fields.mutable_unchecked<3>();
    auto resource = resources.unchecked<3>();
    auto compact_value = compact.unchecked<3>();
    auto is_valid = valid.unchecked<2>();
    auto semantic_value = semantic.unchecked<2>();
    auto line_value = functional_line.unchecked<2>();
    auto page_value = functional_page.unchecked<2>();
    auto producer_value = producer_log.unchecked<2>();
    auto macro_value = macro_id.unchecked<2>();
    auto macro_end_value = macro_end.unchecked<2>();

    for (py::ssize_t row = 0; row < rows; ++row) {
        CountMap<int64_t> l1_pressure;
        CountMap<int64_t> l2_pressure;
        CountMap<uint32_t> llc_pressure;
        l1_pressure.reserve(size_t(length));
        l2_pressure.reserve(size_t(length));
        llc_pressure.reserve(size_t(length));
        for (py::ssize_t token = 0; token < length; ++token) {
            field(row, token, kL1Pressure) = 0;
            field(row, token, kL2Pressure) = 0;
            field(row, token, kLlcPressure) = 0;
            if (!is_valid(row, token)) continue;
            const int64_t l1 = resource(row, token, kL1Set);
            const int64_t l2 = resource(row, token, kL2Set);
            const uint32_t llc = compact_value(row, token, kCompactLlcSet);
            if (l1 >= 0) increment(l1_pressure, l1);
            if (l2 >= 0) increment(l2_pressure, l2);
            if (llc != kInvalidCompact) increment(llc_pressure, llc);
        }
        for (py::ssize_t token = 0; token < length; ++token) {
            if (!is_valid(row, token)) continue;
            const int64_t l1 = resource(row, token, kL1Set);
            const int64_t l2 = resource(row, token, kL2Set);
            const uint32_t llc = compact_value(row, token, kCompactLlcSet);
            if (l1 >= 0) field(row, token, kL1Pressure) = pressure_bucket(l1_pressure.at(l1));
            if (l2 >= 0) field(row, token, kL2Pressure) = pressure_bucket(l2_pressure.at(l2));
            if (llc != kInvalidCompact) {
                field(row, token, kLlcPressure) = pressure_bucket(llc_pressure.at(llc));
            }
        }

        std::array<int, 8> semantic_counts{};
        std::array<int, 30> opclass_counts{};
        CountMap<uint32_t> pc_counts;
        std::unordered_set<int64_t> distinct_lines;
        std::unordered_set<int64_t> distinct_pages;
        std::unordered_set<int64_t> distinct_l1;
        std::unordered_set<int64_t> distinct_l2;
        CountMap<uint32_t> llc_set_counts;
        CountMap<int64_t> llc_bank_counts;
        CountMap<int64_t> channel_counts;
        CountMap<uint32_t> dram_bank_counts;
        CountMap<uint32_t> dram_row_counts;
        pc_counts.reserve(size_t(length));
        distinct_lines.reserve(size_t(length));
        distinct_pages.reserve(size_t(length));
        distinct_l1.reserve(size_t(length));
        distinct_l2.reserve(size_t(length));
        llc_set_counts.reserve(size_t(length));
        llc_bank_counts.reserve(size_t(length));
        channel_counts.reserve(size_t(length));
        dram_bank_counts.reserve(size_t(length));
        dram_row_counts.reserve(size_t(length));

        int n_valid = 0;
        int n_mem = 0;
        int valid_llc_sets = 0;
        int valid_dram_rows = 0;
        int valid_paddr = 0;
        int mem_hot = 0;
        int mem_cold = 0;
        int stream_stride = 0;
        int large_stride = 0;
        int short_dependency = 0;
        int branch_count = 0;
        int branch_taken_count = 0;
        int branch_switches = 0;
        int previous_branch_taken = -1;
        int macro_blocks = 0;
        bool last_macro_end = false;
        double producer_sum = 0.0;
        float producer_max = 0.0f;

        for (py::ssize_t token = 0; token < length; ++token) {
            if (!is_valid(row, token)) continue;
            ++n_valid;
            const uint8_t flags = semantic_value(row, token);
            for (int bit = 0; bit < 8; ++bit) {
                semantic_counts[bit] += (flags & uint8_t(1u << bit)) != 0;
            }
            const int opclass = int(field(row, token, kOpClass));
            if (opclass >= 0 && opclass < int(opclass_counts.size())) ++opclass_counts[opclass];
            const float producer = producer_value(row, token);
            producer_sum += double(producer);
            producer_max = std::max(producer_max, producer);
            const int producer_distance = int(field(row, token, kProducerDistance));
            short_dependency += producer_distance > 0 && producer_distance <= 4;
            increment(pc_counts, macro_value(row, token));
            last_macro_end = macro_end_value(row, token);
            macro_blocks += last_macro_end;

            if ((flags & uint8_t(1u << 3)) != 0) {
                const int taken = field(row, token, kBranchTaken) == 2;
                branch_taken_count += taken;
                if (previous_branch_taken >= 0) branch_switches += taken != previous_branch_taken;
                previous_branch_taken = taken;
                ++branch_count;
            }

            const bool memory = (flags & 0x7u) != 0;
            if (!memory) continue;
            ++n_mem;
            const int reuse = int(field(row, token, kReuseDistance));
            const int stride = int(field(row, token, kStride));
            mem_hot += reuse == 2 || reuse == 3;
            mem_cold += reuse == 1 || reuse == 8;
            stream_stride += stride >= 3 && stride <= 6;
            large_stride += stride >= 7 && stride <= 9;
            const int64_t line = line_value(row, token);
            const int64_t page = page_value(row, token);
            if (line >= 0) distinct_lines.insert(line);
            if (page >= 0) distinct_pages.insert(page);
            const int64_t physical = resource(row, token, kPhysicalLine);
            const int64_t l1 = resource(row, token, kL1Set);
            const int64_t l2 = resource(row, token, kL2Set);
            const int64_t bank = resource(row, token, kLlcBank);
            const int64_t channel = resource(row, token, kDramChannel);
            const uint32_t llc = compact_value(row, token, kCompactLlcSet);
            const uint32_t dram_bank = compact_value(row, token, kCompactDramBank);
            const uint32_t dram_row = compact_value(row, token, kCompactDramRow);
            valid_paddr += physical >= 0;
            if (l1 >= 0) distinct_l1.insert(l1);
            if (l2 >= 0) distinct_l2.insert(l2);
            if (llc != kInvalidCompact) {
                ++valid_llc_sets;
                increment(llc_set_counts, llc);
            }
            if (bank >= 0) increment(llc_bank_counts, bank);
            if (channel >= 0) increment(channel_counts, channel);
            if (dram_bank != kInvalidCompact) increment(dram_bank_counts, dram_bank);
            if (dram_row != kInvalidCompact) {
                ++valid_dram_rows;
                increment(dram_row_counts, dram_row);
            }
        }
        if (n_valid > 0 && !last_macro_end) ++macro_blocks;
        const double n = double(std::max(1, n_valid));
        const double mem_den = double(std::max(1, n_mem));
        double pc_entropy = 0.0;
        if (pc_counts.size() > 1) {
            for (const auto &item : pc_counts) {
                const double probability = double(item.second) / n;
                pc_entropy -= probability * std::log(std::max(probability, 1.0e-12));
            }
            pc_entropy /= std::max(std::log(double(pc_counts.size())), 1.0e-12);
        }
        int index = 0;
        for (int value : semantic_counts) out(row, index++) = double(value) / n;
        out(row, index++) = double(opclass_counts[2]) / n;
        out(row, index++) = double(opclass_counts[3]) / n;
        out(row, index++) = double(opclass_counts[4] + opclass_counts[5] + opclass_counts[6] + opclass_counts[10]) / n;
        out(row, index++) = double(opclass_counts[7] + opclass_counts[8]) / n;
        out(row, index++) = double(opclass_counts[9] + opclass_counts[11] + opclass_counts[23] + opclass_counts[24] + opclass_counts[29]) / n;
        int conditional = 0;
        int indirect = 0;
        for (py::ssize_t token = 0; token < length; ++token) {
            if (!is_valid(row, token)) continue;
            const int kind = int(field(row, token, kBranchKind));
            conditional += (kind & 0x2) != 0;
            indirect += (kind & 0x4) != 0;
        }
        out(row, index++) = double(conditional) / n;
        out(row, index++) = double(indirect) / n;
        out(row, index++) = double(distinct_lines.size()) / n;
        out(row, index++) = double(distinct_pages.size()) / n;
        out(row, index++) = producer_sum / n;
        out(row, index++) = double(producer_max) / 16.0;
        out(row, index++) = double(mem_hot) / mem_den;
        out(row, index++) = double(mem_cold) / mem_den;
        out(row, index++) = double(stream_stride) / mem_den;
        out(row, index++) = double(large_stride) / mem_den;
        out(row, index++) = double(short_dependency) / n;
        out(row, index++) = pc_entropy;
        out(row, index++) = std::log1p(double(n_valid) / double(std::max(1, macro_blocks))) / 8.0;
        out(row, index++) = double(n_valid) / double(std::max<py::ssize_t>(1, length));
        out(row, index++) = double(branch_taken_count) / double(std::max(1, branch_count));
        out(row, index++) = double(branch_switches) / double(std::max(1, branch_count - 1));
        out(row, index++) = double(valid_paddr) / mem_den;
        out(row, index++) = double(distinct_l1.size()) / mem_den;
        out(row, index++) = double(distinct_l2.size()) / mem_den;
        out(row, index++) = double(llc_set_counts.size()) / mem_den;
        out(row, index++) = double(valid_llc_sets - int(llc_set_counts.size())) / mem_den;
        out(row, index++) = hhi(llc_bank_counts);
        out(row, index++) = hhi(channel_counts);
        out(row, index++) = hhi(dram_bank_counts);
        out(row, index++) = double(valid_dram_rows - int(dram_row_counts.size())) / mem_den;
        if (index != kSummaryCount) throw std::runtime_error("native summary dimension drift");
    }
    return output;
}

}  // namespace

PYBIND11_MODULE(_context_native, module) {
    module.doc() = "Fused v29 pressure and summary hot path";
    module.def("pressure_summary", &pressure_summary, py::arg("fields"),
               py::arg("resources"), py::arg("resource_compact"),
               py::arg("valid"), py::arg("semantic"),
               py::arg("functional_line"), py::arg("functional_page"),
               py::arg("producer_log"), py::arg("macro_id"),
               py::arg("macro_end"));
    module.def("cross_features", &cross_features, py::arg("resources"),
               py::arg("lines"), py::arg("kinds"), py::arg("valid"),
               py::arg("resource_compact"), py::arg("dram_row_radix"));
}
