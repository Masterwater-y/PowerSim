#include "fastsim/trace.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstring>
#include <filesystem>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace fastsim {
namespace {

constexpr std::array<char, 8> kTraceMagic{
    'F', 'S', 'T', 'R', 'C', '0', '1', '\0'};
constexpr std::uint32_t kTraceVersion = 6;
constexpr std::uint64_t kFeatureVirtualPageTokens = 1ull << 0;
constexpr std::uint64_t kFeatureSyscallMarkers = 1ull << 1;
constexpr std::uint64_t kFeatureDestinationClassCounts = 1ull << 2;
constexpr std::uint32_t kVirtualPageBits = 12;
constexpr std::uint64_t kVirtualPageBytes = 1ull << kVirtualPageBits;

struct LegacyTraceRecordV2 {
    std::uint64_t pc = 0;
    std::uint64_t address = 0;
    std::uint64_t target = 0;
    std::uint64_t next_pc = 0;
    std::uint16_t size = 0;
    std::uint16_t flags = kRetires;
    std::uint32_t reserved = 0;
};
static_assert(sizeof(LegacyTraceRecordV2) == 40,
              "legacy v2 trace record layout changed");

struct BinaryTraceHeader {
    std::array<char, 8> magic{};
    std::uint32_t version = kTraceVersion;
    std::uint32_t header_size = sizeof(BinaryTraceHeader);
    std::uint32_t record_size = sizeof(TraceRecord);
    std::uint32_t core_id = 0;
    std::uint64_t record_count = 0;
    std::uint64_t feature_flags = 0;
    std::array<std::uint64_t, 4> reserved{};
};
static_assert(sizeof(BinaryTraceHeader) == 72,
              "binary trace header layout changed");

std::string trim(std::string value) {
    const auto is_not_space = [](unsigned char c) {
        return !std::isspace(c);
    };
    value.erase(value.begin(),
                std::find_if(value.begin(), value.end(), is_not_space));
    value.erase(
        std::find_if(value.rbegin(), value.rend(), is_not_space).base(),
        value.end());
    return value;
}

class JsonLine {
  public:
    explicit JsonLine(const std::string& line) : line_(line) {}

    bool has(const char* key) const {
        std::size_t position = 0;
        return value_position(key, &position);
    }

    std::uint64_t u64(const char* key, std::uint64_t fallback = 0) const {
        std::size_t position = 0;
        if (!value_position(key, &position)) return fallback;
        if (position < line_.size() && line_[position] == '"') ++position;
        std::size_t consumed = 0;
        const auto* start = line_.c_str() + position;
        const auto value = std::stoull(start, &consumed, 0);
        if (consumed == 0) {
            throw std::invalid_argument(std::string("invalid number for ") +
                                        key);
        }
        return value;
    }

    bool boolean(const char* key, bool fallback = false) const {
        std::size_t position = 0;
        if (!value_position(key, &position)) return fallback;
        if (line_.compare(position, 4, "true") == 0) return true;
        if (line_.compare(position, 5, "false") == 0) return false;
        if (line_[position] == '"') {
            ++position;
            if (line_.compare(position, 4, "true") == 0) return true;
            if (line_.compare(position, 5, "false") == 0) return false;
        }
        return u64(key, 0) != 0;
    }

    std::array<std::uint32_t, 4> u32_array(
        const char* key, std::uint32_t fallback = 0) const {
        std::array<std::uint32_t, 4> result{
            fallback, fallback, fallback, fallback};
        std::size_t position = 0;
        if (!value_position(key, &position)) return result;
        if (position >= line_.size() || line_[position] != '[') {
            throw std::invalid_argument(std::string("expected array for ") +
                                        key);
        }
        ++position;
        for (std::size_t index = 0; index < result.size(); ++index) {
            while (position < line_.size() &&
                   std::isspace(static_cast<unsigned char>(line_[position]))) {
                ++position;
            }
            if (position < line_.size() && line_[position] == ']') break;
            std::size_t consumed = 0;
            const auto value = std::stoull(line_.c_str() + position,
                                           &consumed, 0);
            if (consumed == 0 ||
                value > std::numeric_limits<std::uint32_t>::max()) {
                throw std::invalid_argument(std::string("invalid array for ") +
                                            key);
            }
            result[index] = static_cast<std::uint32_t>(value);
            position += consumed;
            while (position < line_.size() &&
                   std::isspace(static_cast<unsigned char>(line_[position]))) {
                ++position;
            }
            if (position < line_.size() && line_[position] == ',') {
                ++position;
            } else if (position < line_.size() &&
                       line_[position] == ']') {
                break;
            } else {
                throw std::invalid_argument(std::string("invalid array for ") +
                                            key);
            }
        }
        return result;
    }

  private:
    bool value_position(const char* key, std::size_t* output) const {
        const std::string needle = std::string("\"") + key + "\"";
        auto position = line_.find(needle);
        if (position == std::string::npos) return false;
        position = line_.find(':', position + needle.size());
        if (position == std::string::npos) return false;
        ++position;
        while (position < line_.size() &&
               std::isspace(static_cast<unsigned char>(line_[position]))) {
            ++position;
        }
        if (position >= line_.size() ||
            line_.compare(position, 4, "null") == 0) {
            return false;
        }
        *output = position;
        return true;
    }

    const std::string& line_;
};

TraceRecord parse_gem5_json(
    const std::string& line,
    std::map<std::pair<std::uint64_t, std::uint64_t>, std::uint32_t>&
        virtual_page_tokens,
    std::uint32_t& next_virtual_page_token) {
    const JsonLine json(line);
    TraceRecord record;
    record.pc = json.u64("macro_pc", json.u64("pc", 0));
    if (json.has("paddr")) {
        record.address = json.u64("paddr", 0);
        record.flags |= kPhysicalAddress;
    } else if (json.has("physical_address")) {
        record.address = json.u64("physical_address", 0);
        record.flags |= kPhysicalAddress;
    } else {
        record.address = json.u64("vaddr", json.u64("address", 0));
    }
    record.target =
        json.u64("branch_target", json.u64("target", 0));
    const auto size = json.u64("size", 0);
    if (size > std::numeric_limits<std::uint16_t>::max()) {
        throw std::invalid_argument("memory operation size exceeds uint16");
    }
    record.size = static_cast<std::uint16_t>(size);
    record.flags = static_cast<std::uint16_t>(record.flags | kRetires);

    const auto set = [&record](TraceFlag flag, bool value) {
        if (value) {
            record.flags = static_cast<std::uint16_t>(record.flags | flag);
        }
    };
    set(kLoad, json.boolean("is_load"));
    set(kStore, json.boolean("is_store"));
    set(kAtomic, json.boolean("is_atomic"));
    set(kBranch, json.boolean("is_branch"));
    set(kConditional, json.boolean("is_branch_cond"));
    set(kIndirect, json.boolean("is_branch_indirect"));
    set(kCall, json.boolean("is_call"));
    set(kReturn, json.boolean("is_return"));
    const bool taken = json.boolean("branch_taken");
    set(kTaken, taken);
    const bool has_next_pc =
        json.has("branch_next_pc") || json.has("next_pc");
    record.next_pc = json.u64(
        "branch_next_pc",
        json.u64("next_pc", taken ? record.target : record.pc + 4));
    if (json.has("branch_taken") &&
        has_next_pc && json.boolean("is_branch")) {
        set(kBranchOutcomeValid, true);
    }
    set(kMicroOp, json.boolean("is_microop"));
    set(kLastMicroOp, json.boolean("is_last_microop"));
    // `is_syscall` is the portable contract.  `instr_type == 7` accepts the
    // existing TaoTrace records where SYS is enum value seven.
    const bool is_syscall =
        json.boolean("is_syscall") ||
        (json.has("instr_type") && json.u64("instr_type") == 7);
    set(kSerialize, json.boolean("is_serialize") || is_syscall);
    const auto op_class =
        json.u64("op_class", json.u64("opcode", 0));
    const auto n_src = json.u64("n_src", 0);
    const auto n_dst = json.u64("n_dst", 0);
    if (op_class > static_cast<std::uint64_t>(
                       std::numeric_limits<std::int16_t>::max()) ||
        n_src > std::numeric_limits<std::uint8_t>::max() ||
        n_dst > std::numeric_limits<std::uint8_t>::max()) {
        throw std::invalid_argument("core timing feature exceeds trace width");
    }
    record.op_class = is_syscall
        ? kSyscallOpClass
        : static_cast<std::int16_t>(op_class);
    record.n_src = static_cast<std::uint8_t>(n_src);
    record.n_dst = static_cast<std::uint8_t>(n_dst);
    record.producer_dists = json.u32_array("producer_dists", 0);
    const auto producer_classes =
        json.u32_array("producer_classes", 255);
    const bool has_destination_classes =
        json.has("destination_class_counts");
    const auto destination_classes =
        json.u32_array("destination_class_counts", 0);
    std::uint32_t classified_destinations = 0;
    for (std::size_t index = 0;
         index < record.producer_classes.size(); ++index) {
        if (producer_classes[index] >
            std::numeric_limits<std::uint8_t>::max()) {
            throw std::invalid_argument(
                "producer class exceeds trace width");
        }
        if (has_destination_classes) {
            if ((producer_classes[index] > 3 &&
                 producer_classes[index] != 255) ||
                destination_classes[index] > 31) {
                throw std::invalid_argument(
                    "packed register class/count exceeds trace width");
            }
            const auto producer = producer_classes[index] == 255
                                      ? 7u
                                      : producer_classes[index];
            record.producer_classes[index] =
                static_cast<std::uint8_t>(
                    (destination_classes[index] << 3) | producer);
            classified_destinations += destination_classes[index];
        } else {
            record.producer_classes[index] =
                static_cast<std::uint8_t>(producer_classes[index]);
        }
    }
    if (has_destination_classes) {
        if (classified_destinations != record.n_dst) {
            throw std::invalid_argument(
                "destination_class_counts must sum to n_dst");
        }
        record.reserved |= kDestinationClassCountsMarker;
    }
    if (record.is_memory() && json.has("vaddr")) {
        const auto virtual_address = json.u64("vaddr", 0);
        const auto physical_address = record.address;
        if (has_flag(record.flags, kPhysicalAddress) &&
            (virtual_address & (kVirtualPageBytes - 1)) !=
                (physical_address & (kVirtualPageBytes - 1))) {
            throw std::invalid_argument(
                "vaddr/paddr page offsets do not match");
        }
        const bool crosses_page =
            record.size != 0 &&
            (virtual_address & (kVirtualPageBytes - 1)) + record.size >
                kVirtualPageBytes;
        if (crosses_page) return record;
        const auto virtual_page = virtual_address >> kVirtualPageBits;
        const auto physical_page = has_flag(record.flags, kPhysicalAddress)
                                       ? physical_address >> kVirtualPageBits
                                       : std::numeric_limits<std::uint64_t>::max();
        const auto identity = std::make_pair(virtual_page, physical_page);
        const auto found = virtual_page_tokens.find(identity);
        if (found != virtual_page_tokens.end()) {
            record.reserved =
                (record.reserved & kDestinationClassCountsMarker) |
                found->second;
        } else {
            if (next_virtual_page_token == 0 ||
                next_virtual_page_token >=
                    kDestinationClassCountsMarker) {
                throw std::overflow_error(
                    "trace contains more than 31-bit virtual-page identities");
            }
            const auto token = next_virtual_page_token++;
            record.reserved =
                (record.reserved & kDestinationClassCountsMarker) | token;
            virtual_page_tokens.emplace(identity, token);
        }
        record.flags = static_cast<std::uint16_t>(
            record.flags | kVirtualPageToken);
    }
    return record;
}

std::string resolve_manifest_path(const std::string& manifest_path,
                                  const std::string& entry_path) {
    const std::filesystem::path candidate(entry_path);
    if (candidate.is_absolute()) return candidate.string();
    const auto base =
        std::filesystem::absolute(std::filesystem::path(manifest_path))
            .parent_path();
    return (base / candidate).lexically_normal().string();
}

}  // namespace

Gem5JsonlTraceSource::Gem5JsonlTraceSource(std::string path)
    : path_(std::move(path)), input_(path_) {
    if (!input_) {
        throw std::runtime_error("cannot open gem5 JSONL trace: " + path_);
    }
}

bool Gem5JsonlTraceSource::next(TraceRecord& record) {
    std::string line;
    while (std::getline(input_, line)) {
        ++line_number_;
        const auto first = line.find('{');
        if (first == std::string::npos) continue;
        try {
            record = parse_gem5_json(
                line.substr(first), virtual_page_tokens_,
                next_virtual_page_token_);
            return true;
        } catch (const std::exception& error) {
            throw std::runtime_error(path_ + ":" +
                                     std::to_string(line_number_) + ": " +
                                     error.what());
        }
    }
    return false;
}

std::string Gem5JsonlTraceSource::description() const {
    return "gem5-jsonl:" + path_;
}

BinaryTraceSource::BinaryTraceSource(std::string path)
    : path_(std::move(path)), input_(path_, std::ios::binary) {
    if (!input_) {
        throw std::runtime_error("cannot open binary trace: " + path_);
    }
    BinaryTraceHeader header;
    input_.read(reinterpret_cast<char*>(&header), sizeof(header));
    const bool current =
        (header.version == kTraceVersion || header.version == 5 ||
         header.version == 4 || header.version == 3) &&
        header.record_size == sizeof(TraceRecord);
    const bool legacy =
        header.version == 2 &&
        header.record_size == sizeof(LegacyTraceRecordV2);
    if (!input_ || header.magic != kTraceMagic ||
        header.header_size != sizeof(BinaryTraceHeader) ||
        (!current && !legacy)) {
        throw std::runtime_error("invalid or unsupported binary trace: " +
                                 path_);
    }
    core_id_ = header.core_id;
    record_count_ = header.record_count;
    legacy_v2_ = legacy;
    if (!legacy_v2_) buffer_.resize(4096);
}

bool BinaryTraceSource::next(TraceRecord& record) {
    if (records_read_ >= record_count_) return false;
    if (legacy_v2_) {
        LegacyTraceRecordV2 legacy;
        input_.read(reinterpret_cast<char*>(&legacy), sizeof(legacy));
        record = TraceRecord{};
        record.pc = legacy.pc;
        record.address = legacy.address;
        record.target = legacy.target;
        record.next_pc = legacy.next_pc;
        record.size = legacy.size;
        record.flags = legacy.flags;
        record.reserved = legacy.reserved;
    } else {
        if (buffer_cursor_ == buffer_size_) {
            const auto remaining = record_count_ - records_read_;
            buffer_size_ = static_cast<std::size_t>(
                std::min<std::uint64_t>(remaining, buffer_.size()));
            buffer_cursor_ = 0;
            const auto bytes = buffer_size_ * sizeof(TraceRecord);
            input_.read(reinterpret_cast<char*>(buffer_.data()),
                        static_cast<std::streamsize>(bytes));
            if (input_.gcount() != static_cast<std::streamsize>(bytes)) {
                throw std::runtime_error(
                    "truncated binary trace: " + path_);
            }
        }
        record = buffer_[buffer_cursor_++];
    }
    if (!input_) {
        throw std::runtime_error("truncated binary trace: " + path_);
    }
    ++records_read_;
    return true;
}

std::string BinaryTraceSource::description() const {
    return "fastsim-binary:" + path_;
}

BinaryTraceWriter::BinaryTraceWriter(std::string path, std::uint32_t core_id)
    : path_(std::move(path)),
      output_(path_, std::ios::in | std::ios::out | std::ios::binary |
                        std::ios::trunc),
      core_id_(core_id) {
    if (!output_) {
        throw std::runtime_error("cannot create binary trace: " + path_);
    }
    write_header();
}

BinaryTraceWriter::~BinaryTraceWriter() {
    if (!closed_) {
        try {
            close();
        } catch (...) {
        }
    }
}

void BinaryTraceWriter::write_header() {
    BinaryTraceHeader header;
    header.magic = kTraceMagic;
    header.core_id = core_id_;
    header.record_count = record_count_;
    header.feature_flags = feature_flags_;
    output_.seekp(0);
    output_.write(reinterpret_cast<const char*>(&header), sizeof(header));
    if (!output_) {
        throw std::runtime_error("failed writing binary trace header: " +
                                 path_);
    }
}

void BinaryTraceWriter::append(const TraceRecord& record) {
    if (closed_) throw std::logic_error("binary trace writer is closed");
    output_.seekp(0, std::ios::end);
    output_.write(reinterpret_cast<const char*>(&record), sizeof(record));
    if (!output_) {
        throw std::runtime_error("failed writing binary trace: " + path_);
    }
    if (has_flag(record.flags, kVirtualPageToken)) {
        feature_flags_ |= kFeatureVirtualPageTokens;
    }
    if (record.is_syscall()) {
        feature_flags_ |= kFeatureSyscallMarkers;
    }
    if (record.has_destination_class_counts()) {
        feature_flags_ |= kFeatureDestinationClassCounts;
    }
    ++record_count_;
}

void BinaryTraceWriter::close() {
    if (closed_) return;
    write_header();
    output_.flush();
    if (!output_) {
        throw std::runtime_error("failed finalizing binary trace: " + path_);
    }
    output_.close();
    closed_ = true;
}

SyntheticTraceSource::SyntheticTraceSource(SyntheticTraceConfig config)
    : config_(config),
      state_(config.seed ^
             (0x9e3779b97f4a7c15ull *
              (static_cast<std::uint64_t>(config.core_id) + 1))) {
    if (config.memory_percent > 100 || config.write_percent > 100 ||
        config.branch_percent > 100 || config.taken_percent > 100 ||
        config.shared_percent > 100 || config.working_set_lines == 0) {
        throw std::invalid_argument("invalid synthetic trace percentages");
    }
}

std::uint64_t SyntheticTraceSource::random() {
    // xorshift64*: deterministic and cheap enough to keep the generator from
    // becoming the benchmark bottleneck.
    state_ ^= state_ >> 12;
    state_ ^= state_ << 25;
    state_ ^= state_ >> 27;
    return state_ * 0x2545f4914f6cdd1dull;
}

bool SyntheticTraceSource::next(TraceRecord& record) {
    if (cursor_ >= config_.instructions) return false;
    const auto bits = random();
    record = TraceRecord{};
    record.pc = 0x400000ull + ((cursor_ & 0xfffull) << 2);
    record.flags = kRetires;

    if ((bits % 100) < config_.memory_percent) {
        record.flags = static_cast<std::uint16_t>(
            record.flags | kLoad | kPhysicalAddress | kVirtualPageToken);
        if (((bits >> 8) % 100) < config_.write_percent) {
            record.flags = static_cast<std::uint16_t>(
                static_cast<std::uint16_t>(
                    record.flags & ~static_cast<std::uint16_t>(kLoad)) |
                kStore);
        }
        const bool shared =
            ((bits >> 16) % 100) < config_.shared_percent;
        const auto line = (bits >> 24) % config_.working_set_lines;
        const auto base = shared
                              ? 0x10000000ull
                              : 0x20000000ull +
                                    static_cast<std::uint64_t>(
                                        config_.core_id) *
                                        config_.working_set_lines * 64;
        record.address = base + line * 64;
        record.reserved = static_cast<std::uint32_t>(
            ((record.address >> kVirtualPageBits) & 0xffffffffull) + 1);
        record.size = 8;
    }

    if (((bits >> 32) % 100) < config_.branch_percent) {
        record.flags =
            static_cast<std::uint16_t>(
                record.flags | kBranch | kConditional |
                kBranchOutcomeValid);
        if (((bits >> 40) % 100) < config_.taken_percent) {
            record.flags =
                static_cast<std::uint16_t>(record.flags | kTaken);
        }
        const auto delta =
            static_cast<std::int64_t>(((bits >> 48) & 0xffu) * 4) - 512;
        record.target = static_cast<std::uint64_t>(
            static_cast<std::int64_t>(record.pc) + delta);
        record.next_pc = has_flag(record.flags, kTaken)
                             ? record.target
                             : record.pc + 4;
    }
    ++cursor_;
    return true;
}

std::string SyntheticTraceSource::description() const {
    return "synthetic:core" + std::to_string(config_.core_id);
}

std::vector<TraceManifestEntry> read_trace_manifest(
    const std::string& manifest_path) {
    std::ifstream input(manifest_path);
    if (!input) {
        throw std::runtime_error("cannot open trace manifest: " +
                                 manifest_path);
    }
    std::vector<TraceManifestEntry> entries;
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        const auto comment = line.find('#');
        if (comment != std::string::npos) line.resize(comment);
        line = trim(line);
        if (line.empty()) continue;
        std::istringstream parser(line);
        TraceManifestEntry entry;
        if (!(parser >> entry.core_id >> entry.format >> entry.path)) {
            throw std::runtime_error(
                manifest_path + ":" + std::to_string(line_number) +
                ": expected '<core-id> <format> <path> "
                "[source-core-id]'");
        }
        std::string source_core;
        if (parser >> source_core) {
            std::size_t consumed = 0;
            const auto value = std::stoull(source_core, &consumed, 10);
            if (consumed != source_core.size() ||
                value > std::numeric_limits<std::uint32_t>::max()) {
                throw std::runtime_error(
                    manifest_path + ":" + std::to_string(line_number) +
                    ": invalid source core ID");
            }
            entry.source_core_id = static_cast<std::uint32_t>(value);
            entry.has_source_core_id = true;
            std::string extra;
            if (parser >> extra) {
                throw std::runtime_error(
                    manifest_path + ":" + std::to_string(line_number) +
                    ": unexpected trailing manifest field");
            }
        }
        entry.path = resolve_manifest_path(manifest_path, entry.path);
        entries.push_back(std::move(entry));
    }
    return entries;
}

std::vector<std::unique_ptr<TraceSource>> open_trace_manifest(
    const std::string& manifest_path, std::uint32_t maximum_cores) {
    auto entries = read_trace_manifest(manifest_path);
    if (entries.empty() || entries.size() > maximum_cores) {
        throw std::runtime_error(
            "trace manifest has " + std::to_string(entries.size()) +
            " entries; expected between 1 and configured core count " +
            std::to_string(maximum_cores));
    }
    std::sort(entries.begin(), entries.end(),
              [](const auto& left, const auto& right) {
                  return left.core_id < right.core_id;
              });
    std::vector<std::unique_ptr<TraceSource>> sources;
    sources.reserve(entries.size());
    for (std::uint32_t core = 0; core < entries.size(); ++core) {
        const auto& entry = entries[core];
        if (entry.core_id != core) {
            throw std::runtime_error(
                "trace manifest core IDs must be dense starting at zero");
        }
        if (entry.format == "gem5-jsonl" || entry.format == "jsonl") {
            if (entry.has_source_core_id) {
                throw std::runtime_error(
                    "source core remapping is only valid for binary traces");
            }
            sources.push_back(
                std::make_unique<Gem5JsonlTraceSource>(entry.path));
        } else if (entry.format == "fastsim-binary" ||
                   entry.format == "binary") {
            auto source = std::make_unique<BinaryTraceSource>(entry.path);
            const auto expected_source_core =
                entry.has_source_core_id ? entry.source_core_id : core;
            if (source->core_id() != expected_source_core) {
                throw std::runtime_error(
                    "binary trace source core ID does not match manifest: " +
                    entry.path);
            }
            sources.push_back(std::move(source));
        } else {
            throw std::runtime_error("unknown trace format: " +
                                     entry.format);
        }
    }
    return sources;
}

void convert_gem5_jsonl_to_binary(const std::string& input_path,
                                  const std::string& output_path,
                                  std::uint32_t core_id) {
    Gem5JsonlTraceSource input(input_path);
    BinaryTraceWriter output(output_path, core_id);
    TraceRecord record;
    while (input.next(record)) output.append(record);
    output.close();
}

std::string hit_level_name(HitLevel level) {
    switch (level) {
        case HitLevel::kL1:
            return "L1";
        case HitLevel::kL2:
            return "L2";
        case HitLevel::kLlc:
            return "LLC";
        case HitLevel::kRemote:
            return "REMOTE";
        case HitLevel::kMemory:
            return "MEMORY";
        case HitLevel::kUnknown:
            return "UNKNOWN";
    }
    return "UNKNOWN";
}

}  // namespace fastsim
