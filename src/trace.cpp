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
constexpr std::array<char, 8> kVirtualPageMapMagic{
    'F', 'S', 'T', 'V', 'M', 'P', '1', '\0'};
constexpr std::array<char, 8> kAddressSpaceMapMagic{
    'F', 'S', 'T', 'A', 'S', 'M', '1', '\0'};
constexpr std::array<char, 8> kInstructionMapMagicV1{
    'F', 'S', 'T', 'I', 'M', 'P', '1', '\0'};
constexpr std::array<char, 8> kInstructionMapMagicV2{
    'F', 'S', 'T', 'I', 'M', 'P', '2', '\0'};
constexpr std::uint32_t kTraceVersion = 7;
constexpr std::uint32_t kVirtualPageMapVersion = 1;
constexpr std::uint32_t kAddressSpaceMapVersion = 1;
constexpr std::uint32_t kInstructionMapVersionV1 = 1;
constexpr std::uint32_t kInstructionMapVersionV2 = 2;
constexpr std::uint64_t kFeatureVirtualPageTokens = 1ull << 0;
constexpr std::uint64_t kFeatureSyscallMarkers = 1ull << 1;
constexpr std::uint64_t kFeatureDestinationClassCounts = 1ull << 2;
constexpr std::uint64_t kFeatureSyscallMetadata = 1ull << 3;
constexpr std::uint32_t kVirtualPageBits = 12;
constexpr std::uint64_t kVirtualPageBytes = 1ull << kVirtualPageBits;
constexpr std::uint32_t kVirtualPageMapPhysicalValid = 1u << 0;
constexpr std::uint32_t kVirtualPageMapInitialPteStateValid = 1u << 1;
constexpr std::uint32_t kVirtualPageMapInitialPtePresent = 1u << 2;
constexpr std::uint32_t kVirtualPageMapMeasurementPteStateValid = 1u << 3;
constexpr std::uint32_t kVirtualPageMapMeasurementPtePresent = 1u << 4;
constexpr std::uint32_t
    kVirtualPageMapMeasurementBoundaryInflightFault = 1u << 5;
constexpr std::uint32_t kKnownVirtualPageMapFlags =
    kVirtualPageMapPhysicalValid |
    kVirtualPageMapInitialPteStateValid |
    kVirtualPageMapInitialPtePresent |
    kVirtualPageMapMeasurementPteStateValid |
    kVirtualPageMapMeasurementPtePresent |
    kVirtualPageMapMeasurementBoundaryInflightFault;
constexpr std::uint32_t kInstructionMapComplete = 1u << 0;
constexpr std::uint32_t kInstructionMapOperandsComplete = 1u << 1;
constexpr std::uint8_t kInstructionOperandsValid = 1u << 0;
constexpr std::uint16_t kKnownStaticInstructionFlags =
    kStaticBranch | kStaticConditional | kStaticIndirect | kStaticCall |
    kStaticReturn | kStaticDirectTargetValid | kStaticMemory;

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

struct BinaryVirtualPageMapHeaderV1 {
    std::array<char, 8> magic{};
    std::uint32_t version = kVirtualPageMapVersion;
    std::uint32_t header_size = sizeof(BinaryVirtualPageMapHeaderV1);
    std::uint32_t entry_size = 0;
    std::uint32_t core_id = 0;
    std::uint64_t source_record_count = 0;
    std::uint64_t entry_count = 0;
    std::uint32_t page_offset_bits = kVirtualPageBits;
    std::uint32_t reserved = 0;
};
static_assert(sizeof(BinaryVirtualPageMapHeaderV1) == 48,
              "virtual-page map header layout changed");

struct BinaryVirtualPageMapEntryV1 {
    std::uint32_t token = 0;
    std::uint32_t flags = 0;
    std::uint64_t first_record_ordinal = 0;
    std::uint64_t virtual_page = 0;
    std::uint64_t physical_page = 0;
};
static_assert(sizeof(BinaryVirtualPageMapEntryV1) == 32,
              "virtual-page map entry layout changed");

struct BinaryAddressSpaceMapHeaderV1 {
    std::array<char, 8> magic{};
    std::uint32_t version = kAddressSpaceMapVersion;
    std::uint32_t header_size = sizeof(BinaryAddressSpaceMapHeaderV1);
    std::uint32_t entry_size = 0;
    std::uint32_t core_id = 0;
    std::uint64_t source_record_count = 0;
    std::uint64_t entry_count = 0;
    std::uint64_t reserved = 0;
};
static_assert(sizeof(BinaryAddressSpaceMapHeaderV1) == 48,
              "address-space map header layout changed");

struct BinaryAddressSpaceMapEntryV1 {
    std::uint64_t record_ordinal = 0;
    std::uint64_t address_space_id = 0;
};
static_assert(sizeof(BinaryAddressSpaceMapEntryV1) == 16,
              "address-space map entry layout changed");

struct BinaryInstructionMapHeaderV1 {
    std::array<char, 8> magic{};
    std::uint32_t version = 0;
    std::uint32_t header_size = sizeof(BinaryInstructionMapHeaderV1);
    std::uint32_t entry_size = 0;
    std::uint32_t core_id = 0;
    std::uint64_t source_record_count = 0;
    std::uint64_t entry_count = 0;
    std::uint32_t flags = 0;
    std::uint32_t reserved = 0;
};
static_assert(sizeof(BinaryInstructionMapHeaderV1) == 48,
              "instruction-map header layout changed");

struct BinaryInstructionMapEntryV1 {
    std::uint64_t pc = 0;
    std::uint64_t fallthrough_pc = 0;
    std::uint64_t direct_target = 0;
    std::uint16_t flags = 0;
    std::uint8_t size = 0;
    std::array<std::uint8_t, 5> reserved{};
};
static_assert(sizeof(BinaryInstructionMapEntryV1) == 32,
              "instruction-map entry layout changed");

struct BinaryInstructionMapEntryV2 {
    std::uint64_t pc = 0;
    std::uint64_t fallthrough_pc = 0;
    std::uint64_t direct_target = 0;
    std::uint16_t flags = 0;
    std::uint8_t size = 0;
    std::uint8_t semantic_flags = 0;
    std::array<std::uint8_t, 4> reserved{};
    std::array<std::uint64_t, kStaticRegisterMaskWords>
        read_register_mask{};
    std::array<std::uint64_t, kStaticRegisterMaskWords>
        write_register_mask{};
};
static_assert(sizeof(BinaryInstructionMapEntryV2) == 64,
              "instruction-map v2 entry layout changed");

std::string virtual_page_map_path(const std::string& trace_path) {
    return trace_path + ".vmap";
}

std::string address_space_map_path(const std::string& trace_path) {
    return trace_path + ".asmap";
}

std::string instruction_map_path(const std::string& trace_path) {
    return trace_path + ".imap";
}

void validate_static_instruction(
    const StaticInstructionInfo& instruction) {
    if (instruction.size == 0 || instruction.size > 15 ||
        instruction.pc >
            std::numeric_limits<std::uint64_t>::max() - instruction.size ||
        instruction.fallthrough_pc != instruction.pc + instruction.size ||
        (instruction.flags & ~kKnownStaticInstructionFlags) != 0) {
        throw std::invalid_argument("invalid static instruction geometry");
    }
    const bool branch = instruction.is_branch();
    const bool conditional = instruction.is_conditional();
    const bool indirect = instruction.is_indirect();
    const bool call = has_static_instruction_flag(
        instruction.flags, kStaticCall);
    const bool is_return = has_static_instruction_flag(
        instruction.flags, kStaticReturn);
    const bool direct_target = instruction.has_direct_target();
    if ((conditional || indirect || call || is_return) && !branch) {
        throw std::invalid_argument(
            "static control-flow subtype requires branch flag");
    }
    if (is_return && !indirect) {
        throw std::invalid_argument(
            "static return requires indirect flag");
    }
    if (direct_target && (!branch || indirect)) {
        throw std::invalid_argument(
            "static direct target requires a direct branch");
    }
    if (!direct_target && instruction.direct_target != 0) {
        throw std::invalid_argument(
            "static direct target value lacks validity flag");
    }
    if (!instruction.operand_semantics_valid &&
        (std::any_of(instruction.read_register_mask.begin(),
                     instruction.read_register_mask.end(),
                     [](std::uint64_t value) { return value != 0; }) ||
         std::any_of(instruction.write_register_mask.begin(),
                     instruction.write_register_mask.end(),
                     [](std::uint64_t value) { return value != 0; }))) {
        throw std::invalid_argument(
            "static register masks require valid operand semantics");
    }
}

// FST v7 keeps the 64-byte TraceRecord stream hot and appends one sparse,
// fixed-width row per syscall.  Do not persist the public C++ structure
// directly: bool layout is implementation-defined and the on-disk ABI must
// remain stable.
struct BinarySyscallMetadataV1 {
    std::uint64_t record_ordinal = 0;
    std::uint64_t syscall_ordinal = 0;
    std::uint64_t thread_id = 0;
    std::uint64_t syscall_number = 0;
    std::array<std::uint64_t, kMaximumSyscallArguments> arguments{};
    std::uint64_t return_value_raw = 0;
    std::uint64_t pre_timestamp_us = 0;
    std::uint64_t post_timestamp_us = 0;
    std::uint32_t errno_value = 0;
    std::uint32_t pre_cpu = 0;
    std::uint32_t post_cpu = 0;
    std::uint16_t valid_fields = 0;
    std::uint8_t argument_count = 0;
    std::uint8_t flags = 0;
    std::uint64_t reserved = 0;
};
static_assert(sizeof(BinarySyscallMetadataV1) == 128,
              "binary syscall metadata layout changed");

constexpr std::uint8_t kBinarySyscallFailed = 1u << 0;
constexpr std::uint8_t kBinarySyscallMaybeBlocking = 1u << 1;

void validate_syscall_metadata(const SyscallMetadata& metadata) {
    constexpr std::uint16_t kKnownFields = (1u << 10) - 1;
    if ((metadata.valid_fields & ~kKnownFields) != 0) {
        throw std::invalid_argument(
            "syscall metadata contains unknown validity bits");
    }
    if (metadata.argument_count > kMaximumSyscallArguments) {
        throw std::invalid_argument(
            "syscall argument count exceeds portable ABI maximum");
    }
    if (!metadata.has(kSyscallArgumentsValid) &&
        metadata.argument_count != 0) {
        throw std::invalid_argument(
            "syscall argument count is present without arguments validity");
    }
    if (metadata.has(kSyscallErrnoValid) &&
        (!metadata.has(kSyscallFailureValid) || !metadata.failed)) {
        throw std::invalid_argument(
            "syscall errno requires a captured failed=true marker");
    }
    if (!metadata.has(kSyscallFailureValid) && metadata.failed) {
        throw std::invalid_argument(
            "syscall failed flag is set without failure validity");
    }
    if (!metadata.has(kSyscallMaybeBlockingValid) &&
        metadata.maybe_blocking) {
        throw std::invalid_argument(
            "syscall maybe-blocking flag is set without validity");
    }
}

BinarySyscallMetadataV1 encode_syscall_metadata(
    const SyscallMetadata& metadata) {
    validate_syscall_metadata(metadata);
    BinarySyscallMetadataV1 encoded;
    encoded.record_ordinal = metadata.record_ordinal;
    encoded.syscall_ordinal = metadata.syscall_ordinal;
    encoded.thread_id = metadata.thread_id;
    encoded.syscall_number = metadata.number;
    encoded.arguments = metadata.arguments;
    encoded.return_value_raw = metadata.return_value_raw;
    encoded.pre_timestamp_us = metadata.pre_timestamp_us;
    encoded.post_timestamp_us = metadata.post_timestamp_us;
    encoded.errno_value = metadata.errno_value;
    encoded.pre_cpu = metadata.pre_cpu;
    encoded.post_cpu = metadata.post_cpu;
    encoded.valid_fields = metadata.valid_fields;
    encoded.argument_count = metadata.argument_count;
    if (metadata.failed) encoded.flags |= kBinarySyscallFailed;
    if (metadata.maybe_blocking) {
        encoded.flags |= kBinarySyscallMaybeBlocking;
    }
    return encoded;
}

SyscallMetadata decode_syscall_metadata(
    const BinarySyscallMetadataV1& encoded) {
    if ((encoded.flags & ~(kBinarySyscallFailed |
                           kBinarySyscallMaybeBlocking)) != 0 ||
        encoded.reserved != 0) {
        throw std::invalid_argument(
            "unsupported syscall metadata flags or reserved fields");
    }
    SyscallMetadata metadata;
    metadata.record_ordinal = encoded.record_ordinal;
    metadata.syscall_ordinal = encoded.syscall_ordinal;
    metadata.thread_id = encoded.thread_id;
    metadata.number = encoded.syscall_number;
    metadata.arguments = encoded.arguments;
    metadata.return_value_raw = encoded.return_value_raw;
    metadata.pre_timestamp_us = encoded.pre_timestamp_us;
    metadata.post_timestamp_us = encoded.post_timestamp_us;
    metadata.errno_value = encoded.errno_value;
    metadata.pre_cpu = encoded.pre_cpu;
    metadata.post_cpu = encoded.post_cpu;
    metadata.valid_fields = encoded.valid_fields;
    metadata.argument_count = encoded.argument_count;
    metadata.failed = (encoded.flags & kBinarySyscallFailed) != 0;
    metadata.maybe_blocking =
        (encoded.flags & kBinarySyscallMaybeBlocking) != 0;
    validate_syscall_metadata(metadata);
    return metadata;
}

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

    // Preserve the exact register bits of a syscall return value.  JSON
    // producers may spell Linux errno returns either as -11 or as the
    // corresponding unsigned 64-bit value.
    std::uint64_t u64_bits(const char* key,
                           std::uint64_t fallback = 0) const {
        std::size_t position = 0;
        if (!value_position(key, &position)) return fallback;
        if (position < line_.size() && line_[position] == '"') ++position;
        std::size_t consumed = 0;
        const auto* start = line_.c_str() + position;
        if (*start == '-') {
            const auto value = std::stoll(start, &consumed, 0);
            if (consumed == 0) {
                throw std::invalid_argument(
                    std::string("invalid raw number for ") + key);
            }
            return static_cast<std::uint64_t>(value);
        }
        const auto value = std::stoull(start, &consumed, 0);
        if (consumed == 0) {
            throw std::invalid_argument(
                std::string("invalid raw number for ") + key);
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
            const auto* start = line_.c_str() + position;
            const auto value = *start == '-'
                                   ? static_cast<std::uint64_t>(
                                         std::stoll(start, &consumed, 0))
                                   : std::stoull(start, &consumed, 0);
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

    std::array<std::uint64_t, kMaximumSyscallArguments> u64_array(
        const char* key, std::size_t* count) const {
        std::array<std::uint64_t, kMaximumSyscallArguments> result{};
        *count = 0;
        std::size_t position = 0;
        if (!value_position(key, &position)) return result;
        if (position >= line_.size() || line_[position] != '[') {
            throw std::invalid_argument(std::string("expected array for ") +
                                        key);
        }
        ++position;
        while (true) {
            while (position < line_.size() &&
                   std::isspace(static_cast<unsigned char>(line_[position]))) {
                ++position;
            }
            if (position < line_.size() && line_[position] == ']') break;
            if (*count >= result.size()) {
                throw std::invalid_argument(
                    std::string("too many syscall arguments for ") + key);
            }
            std::size_t consumed = 0;
            const auto* start = line_.c_str() + position;
            const auto value = *start == '-'
                                   ? static_cast<std::uint64_t>(
                                         std::stoll(start, &consumed, 0))
                                   : std::stoull(start, &consumed, 0);
            if (consumed == 0) {
                throw std::invalid_argument(std::string("invalid array for ") +
                                            key);
            }
            result[(*count)++] = value;
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
    std::map<std::tuple<std::uint64_t, std::uint64_t, std::uint64_t>,
             std::uint32_t>&
        virtual_page_tokens,
    std::uint32_t& next_virtual_page_token,
    std::map<std::uint32_t, VirtualPageMapping>& virtual_page_mappings,
    std::uint64_t record_ordinal,
    SyscallMetadata* syscall_metadata,
    std::uint64_t* address_space_id) {
    const JsonLine json(line);
    if (address_space_id != nullptr) {
        *address_space_id = json.u64(
            "address_space_id",
            json.u64("asid", json.u64("cr3", 0)));
    }
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
    if (is_syscall) {
        if (record.is_memory()) {
            throw std::invalid_argument(
                "syscall marker cannot also be a memory operation");
        }
        // The syscall marker carries no memory address; reuse `address` to
        // hold the syscall number (contract: syscall-modeling-dual-cpi.md).
        // A missing number stays 0 and selects the scalar fallback cost.
        record.address = json.u64(
            "syscall_number",
            json.u64("syscall_nr", json.u64("sysnum", 0)));
        record.flags = static_cast<std::uint16_t>(
            record.flags & ~static_cast<std::uint16_t>(kPhysicalAddress));
        if (syscall_metadata != nullptr) {
            *syscall_metadata = SyscallMetadata{};
            syscall_metadata->number = record.address;

            const char* arguments_key =
                json.has("syscall_args") ? "syscall_args"
                                           : (json.has("args") ? "args"
                                                               : nullptr);
            if (arguments_key != nullptr) {
                std::size_t argument_count = 0;
                syscall_metadata->arguments =
                    json.u64_array(arguments_key, &argument_count);
                syscall_metadata->argument_count =
                    static_cast<std::uint8_t>(argument_count);
                syscall_metadata->valid_fields |=
                    kSyscallArgumentsValid;
                const char* count_key =
                    json.has("syscall_arg_count")
                        ? "syscall_arg_count"
                        : (json.has("arg_count") ? "arg_count" : nullptr);
                if (count_key != nullptr &&
                    json.u64(count_key) != argument_count) {
                    throw std::invalid_argument(
                        "syscall argument count does not match argument array");
                }
            }

            const char* return_key =
                json.has("syscall_retval_raw")
                    ? "syscall_retval_raw"
                    : (json.has("syscall_retval")
                           ? "syscall_retval"
                           : (json.has("retval_raw") ? "retval_raw"
                                                     : nullptr));
            if (return_key != nullptr) {
                syscall_metadata->return_value_raw =
                    json.u64_bits(return_key);
                syscall_metadata->valid_fields |=
                    kSyscallReturnValueValid;
            }

            const char* failure_key =
                json.has("syscall_failed")
                    ? "syscall_failed"
                    : (json.has("failed") ? "failed" : nullptr);
            if (failure_key != nullptr) {
                syscall_metadata->failed = json.boolean(failure_key);
                syscall_metadata->valid_fields |= kSyscallFailureValid;
            }

            const char* errno_key =
                json.has("syscall_errno")
                    ? "syscall_errno"
                    : (json.has("errno") ? "errno" : nullptr);
            if (errno_key != nullptr) {
                const auto value = json.u64(errno_key);
                if (value > std::numeric_limits<std::uint32_t>::max()) {
                    throw std::invalid_argument(
                        "syscall errno exceeds uint32");
                }
                syscall_metadata->errno_value =
                    static_cast<std::uint32_t>(value);
                syscall_metadata->failed = true;
                syscall_metadata->valid_fields |= kSyscallErrnoValid;
                syscall_metadata->valid_fields |= kSyscallFailureValid;
            }

            const auto set_u64 = [&](const char* primary,
                                     const char* fallback,
                                     std::uint64_t* value,
                                     SyscallMetadataField field) {
                const char* key = json.has(primary)
                                      ? primary
                                      : (json.has(fallback) ? fallback
                                                            : nullptr);
                if (key != nullptr) {
                    *value = json.u64(key);
                    syscall_metadata->valid_fields |= field;
                }
            };
            set_u64("syscall_pre_timestamp_us", "pre_timestamp_us",
                    &syscall_metadata->pre_timestamp_us,
                    kSyscallPreTimestampValid);
            set_u64("syscall_post_timestamp_us", "post_timestamp_us",
                    &syscall_metadata->post_timestamp_us,
                    kSyscallPostTimestampValid);

            const auto set_u32 = [&](const char* primary,
                                     const char* fallback,
                                     std::uint32_t* value,
                                     SyscallMetadataField field) {
                const char* key = json.has(primary)
                                      ? primary
                                      : (json.has(fallback) ? fallback
                                                            : nullptr);
                if (key == nullptr) return;
                const auto parsed = json.u64(key);
                if (parsed > std::numeric_limits<std::uint32_t>::max()) {
                    throw std::invalid_argument(
                        std::string("syscall field exceeds uint32: ") + key);
                }
                *value = static_cast<std::uint32_t>(parsed);
                syscall_metadata->valid_fields |= field;
            };
            set_u32("syscall_pre_cpu", "pre_cpu",
                    &syscall_metadata->pre_cpu, kSyscallPreCpuValid);
            set_u32("syscall_post_cpu", "post_cpu",
                    &syscall_metadata->post_cpu, kSyscallPostCpuValid);

            const char* blocking_key =
                json.has("syscall_maybe_blocking")
                    ? "syscall_maybe_blocking"
                    : (json.has("maybe_blocking") ? "maybe_blocking"
                                                  : nullptr);
            if (blocking_key != nullptr) {
                syscall_metadata->maybe_blocking =
                    json.boolean(blocking_key);
                syscall_metadata->valid_fields |=
                    kSyscallMaybeBlockingValid;
            }

            const char* thread_key =
                json.has("thread_id")
                    ? "thread_id"
                    : (json.has("threadid") ? "threadid"
                                            : (json.has("tid") ? "tid"
                                                               : nullptr));
            if (thread_key != nullptr) {
                syscall_metadata->thread_id = json.u64(thread_key);
                syscall_metadata->valid_fields |= kSyscallThreadIdValid;
            }
            validate_syscall_metadata(*syscall_metadata);
        }
    }
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
        const auto identity = std::make_tuple(
            address_space_id == nullptr ? 0 : *address_space_id,
            virtual_page, physical_page);
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
        const auto token = record.virtual_page_token();
        auto mapping_it = virtual_page_mappings.find(token);
        if (mapping_it == virtual_page_mappings.end()) {
            VirtualPageMapping mapping;
            mapping.token = token;
            mapping.first_record_ordinal = record_ordinal;
            mapping.virtual_page = virtual_page;
            mapping.physical_page = physical_page;
            mapping.physical_page_valid =
                has_flag(record.flags, kPhysicalAddress);
            mapping.initial_pte_state_valid =
                json.boolean("initial_pte_state_valid", false);
            mapping.initial_pte_present =
                json.boolean("initial_pte_present", false);
            if (mapping.initial_pte_present &&
                !mapping.initial_pte_state_valid) {
                throw std::invalid_argument(
                    "initial_pte_present requires valid initial PTE state");
            }
            mapping.measurement_pte_state_valid =
                json.boolean("measurement_pte_state_valid", false);
            mapping.measurement_pte_present =
                json.boolean("measurement_pte_present", false);
            mapping.measurement_boundary_inflight_fault = json.boolean(
                "measurement_boundary_inflight_fault", false);
            if (mapping.measurement_pte_present &&
                !mapping.measurement_pte_state_valid) {
                throw std::invalid_argument(
                    "measurement_pte_present requires valid measurement "
                    "PTE state");
            }
            mapping_it =
                virtual_page_mappings.emplace(token, mapping).first;
        } else if (json.has("measurement_pte_state_valid") ||
                   json.has("measurement_pte_present") ||
                   json.has("measurement_boundary_inflight_fault")) {
            const bool state_valid =
                json.boolean("measurement_pte_state_valid", false);
            const bool present =
                json.boolean("measurement_pte_present", false);
            if (present && !state_valid) {
                throw std::invalid_argument(
                    "measurement_pte_present requires valid measurement "
                    "PTE state");
            }
            auto& mapping = mapping_it->second;
            if (state_valid && mapping.measurement_pte_state_valid &&
                mapping.measurement_pte_present != present) {
                throw std::invalid_argument(
                    "conflicting measurement PTE state for virtual page");
            }
            if (state_valid) {
                mapping.measurement_pte_state_valid = true;
                mapping.measurement_pte_present = present;
            }
            if (json.boolean(
                    "measurement_boundary_inflight_fault", false)) {
                mapping.measurement_boundary_inflight_fault = true;
            }
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
    has_current_syscall_metadata_ = false;
    std::string line;
    while (std::getline(input_, line)) {
        ++line_number_;
        const auto first = line.find('{');
        if (first == std::string::npos) continue;
        try {
            SyscallMetadata metadata;
            std::uint64_t address_space_id = 0;
            record = parse_gem5_json(
                line.substr(first), virtual_page_tokens_,
                next_virtual_page_token_, virtual_page_mappings_,
                records_emitted_, &metadata, &address_space_id);
            if (address_space_id == 0) {
                if (!address_space_transitions_.empty()) {
                    throw std::invalid_argument(
                        "address_space_id is missing after explicit "
                        "address-space records");
                }
            } else {
                if (records_emitted_ != 0 &&
                    address_space_transitions_.empty()) {
                    throw std::invalid_argument(
                        "address_space_id first appears after record zero");
                }
                if (address_space_transitions_.empty() ||
                    address_space_transitions_.back().address_space_id !=
                        address_space_id) {
                    address_space_transitions_.push_back(
                        AddressSpaceTransition{
                            records_emitted_, address_space_id});
                }
            }
            current_address_space_id_ = address_space_id;
            if (record.is_syscall()) {
                metadata.record_ordinal = records_emitted_;
                metadata.syscall_ordinal = syscalls_emitted_++;
                current_syscall_metadata_ = metadata;
                has_current_syscall_metadata_ = true;
            }
            ++records_emitted_;
            return true;
        } catch (const std::exception& error) {
            throw std::runtime_error(path_ + ":" +
                                     std::to_string(line_number_) + ": " +
                                     error.what());
        }
    }
    return false;
}

std::uint64_t Gem5JsonlTraceSource::address_space_id_for_record(
    std::uint64_t ordinal) const {
    if (ordinal >= records_emitted_ || address_space_transitions_.empty()) {
        return 0;
    }
    const auto found = std::upper_bound(
        address_space_transitions_.begin(),
        address_space_transitions_.end(), ordinal,
        [](std::uint64_t value, const AddressSpaceTransition& transition) {
            return value < transition.record_ordinal;
        });
    return std::prev(found)->address_space_id;
}

std::string Gem5JsonlTraceSource::description() const {
    return "gem5-jsonl:" + path_;
}

const VirtualPageMapping* Gem5JsonlTraceSource::virtual_page_mapping(
    std::uint32_t token) const {
    const auto found = virtual_page_mappings_.find(token);
    return found == virtual_page_mappings_.end() ? nullptr : &found->second;
}

BinaryTraceSource::BinaryTraceSource(std::string path)
    : path_(std::move(path)), input_(path_, std::ios::binary) {
    if (!input_) {
        throw std::runtime_error("cannot open binary trace: " + path_);
    }
    BinaryTraceHeader header;
    input_.read(reinterpret_cast<char*>(&header), sizeof(header));
    const bool current =
        (header.version == kTraceVersion || header.version == 6 ||
         header.version == 5 ||
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
    trace_version_ = header.version;
    legacy_v2_ = legacy;
    if (!legacy_v2_) buffer_.resize(4096);

    if (header.version == kTraceVersion) {
        switch (static_cast<SyscallAbi>(header.reserved[3])) {
            case SyscallAbi::kUnknown:
            case SyscallAbi::kLinuxX86_64:
            case SyscallAbi::kLinuxX86_32:
            case SyscallAbi::kLinuxAArch64:
            case SyscallAbi::kLinuxArm32:
                syscall_abi_ = static_cast<SyscallAbi>(header.reserved[3]);
                break;
            default:
                throw std::runtime_error(
                    "unsupported syscall ABI in binary trace: " + path_);
        }

        if (record_count_ >
            (std::numeric_limits<std::uint64_t>::max() -
             sizeof(BinaryTraceHeader)) /
                sizeof(TraceRecord)) {
            throw std::runtime_error("binary trace size overflows: " + path_);
        }
        const auto records_end =
            static_cast<std::uint64_t>(sizeof(BinaryTraceHeader)) +
            record_count_ * sizeof(TraceRecord);
        const auto file_bytes = std::filesystem::file_size(path_);
        has_syscall_metadata_ =
            (header.feature_flags & kFeatureSyscallMetadata) != 0;
        if (has_syscall_metadata_) {
            if ((header.feature_flags & kFeatureSyscallMarkers) == 0) {
                throw std::runtime_error(
                    "syscall metadata table lacks marker feature bit: " +
                    path_);
            }
            const auto metadata_offset = header.reserved[0];
            const auto metadata_count = header.reserved[1];
            const auto metadata_size = header.reserved[2];
            if (metadata_count == 0 || metadata_offset != records_end ||
                metadata_size != sizeof(BinarySyscallMetadataV1) ||
                metadata_count >
                    (std::numeric_limits<std::uint64_t>::max() -
                     metadata_offset) /
                        metadata_size ||
                file_bytes !=
                    metadata_offset + metadata_count * metadata_size ||
                metadata_count >
                    static_cast<std::uint64_t>(
                        std::numeric_limits<std::size_t>::max())) {
                throw std::runtime_error(
                    "invalid syscall metadata table in binary trace: " +
                    path_);
            }
            input_.seekg(static_cast<std::streamoff>(metadata_offset));
            syscall_metadata_.reserve(
                static_cast<std::size_t>(metadata_count));
            for (std::uint64_t index = 0; index < metadata_count; ++index) {
                BinarySyscallMetadataV1 encoded;
                input_.read(reinterpret_cast<char*>(&encoded),
                            sizeof(encoded));
                if (!input_) {
                    throw std::runtime_error(
                        "truncated syscall metadata table: " + path_);
                }
                SyscallMetadata metadata;
                try {
                    metadata = decode_syscall_metadata(encoded);
                } catch (const std::exception& error) {
                    throw std::runtime_error(
                        "invalid syscall metadata table in " + path_ +
                        ": " + error.what());
                }
                if (metadata.syscall_ordinal != index ||
                    metadata.record_ordinal >= record_count_ ||
                    (!syscall_metadata_.empty() &&
                     metadata.record_ordinal <=
                         syscall_metadata_.back().record_ordinal)) {
                    throw std::runtime_error(
                        "unordered syscall metadata table in binary trace: " +
                        path_);
                }
                syscall_metadata_.push_back(metadata);
            }
        } else if (file_bytes != records_end) {
            throw std::runtime_error(
                "unexpected trailing data in binary trace: " + path_);
        }

        const auto as_map_path = address_space_map_path(path_);
        if (std::filesystem::exists(as_map_path)) {
            std::ifstream as_map(as_map_path, std::ios::binary);
            BinaryAddressSpaceMapHeaderV1 map_header;
            as_map.read(reinterpret_cast<char*>(&map_header),
                        sizeof(map_header));
            const auto map_bytes = std::filesystem::file_size(as_map_path);
            const bool count_fits =
                map_header.entry_count <=
                static_cast<std::uint64_t>(
                    std::numeric_limits<std::size_t>::max());
            const bool size_fits =
                map_header.entry_count <=
                (std::numeric_limits<std::uint64_t>::max() -
                 sizeof(map_header)) /
                    sizeof(BinaryAddressSpaceMapEntryV1);
            if (!as_map || map_header.magic != kAddressSpaceMapMagic ||
                map_header.version != kAddressSpaceMapVersion ||
                map_header.header_size != sizeof(map_header) ||
                map_header.entry_size !=
                    sizeof(BinaryAddressSpaceMapEntryV1) ||
                map_header.core_id != core_id_ ||
                map_header.source_record_count != record_count_ ||
                map_header.entry_count == 0 || map_header.reserved != 0 ||
                record_count_ == 0 || !count_fits || !size_fits ||
                map_bytes != sizeof(map_header) +
                    map_header.entry_count *
                        sizeof(BinaryAddressSpaceMapEntryV1)) {
                throw std::runtime_error(
                    "invalid address-space map for binary trace: " + path_);
            }
            address_space_transitions_.reserve(
                static_cast<std::size_t>(map_header.entry_count));
            for (std::uint64_t index = 0;
                 index < map_header.entry_count; ++index) {
                BinaryAddressSpaceMapEntryV1 encoded;
                as_map.read(reinterpret_cast<char*>(&encoded),
                            sizeof(encoded));
                const bool first = index == 0;
                const bool ordered = first
                    ? encoded.record_ordinal == 0
                    : encoded.record_ordinal >
                          address_space_transitions_.back().record_ordinal;
                const bool changed = first ||
                    encoded.address_space_id !=
                        address_space_transitions_.back().address_space_id;
                if (!as_map || encoded.address_space_id == 0 || !ordered ||
                    !changed || encoded.record_ordinal >= record_count_) {
                    throw std::runtime_error(
                        "invalid address-space map entry for binary trace: " +
                        path_);
                }
                address_space_transitions_.push_back(
                    AddressSpaceTransition{
                        encoded.record_ordinal,
                        encoded.address_space_id});
            }
        }

        const auto page_map_path = virtual_page_map_path(path_);
        if (std::filesystem::exists(page_map_path)) {
            if ((header.feature_flags & kFeatureVirtualPageTokens) == 0) {
                throw std::runtime_error(
                    "virtual-page map accompanies a trace without tokens: " +
                    path_);
            }
            std::ifstream page_map(page_map_path, std::ios::binary);
            BinaryVirtualPageMapHeaderV1 map_header;
            page_map.read(reinterpret_cast<char*>(&map_header),
                          sizeof(map_header));
            const auto map_bytes = std::filesystem::file_size(page_map_path);
            const bool count_fits =
                map_header.entry_count <=
                static_cast<std::uint64_t>(
                    std::numeric_limits<std::size_t>::max());
            const bool size_fits =
                map_header.entry_count <=
                (std::numeric_limits<std::uint64_t>::max() -
                 sizeof(map_header)) /
                    sizeof(BinaryVirtualPageMapEntryV1);
            if (!page_map || map_header.magic != kVirtualPageMapMagic ||
                map_header.version != kVirtualPageMapVersion ||
                map_header.header_size != sizeof(map_header) ||
                map_header.entry_size !=
                    sizeof(BinaryVirtualPageMapEntryV1) ||
                map_header.core_id != core_id_ ||
                map_header.source_record_count != record_count_ ||
                map_header.page_offset_bits != kVirtualPageBits ||
                map_header.reserved != 0 || map_header.entry_count == 0 ||
                !count_fits || !size_fits ||
                map_bytes != sizeof(map_header) +
                    map_header.entry_count *
                        sizeof(BinaryVirtualPageMapEntryV1)) {
                throw std::runtime_error(
                    "invalid virtual-page map for binary trace: " + path_);
            }
            for (std::uint64_t index = 0;
                 index < map_header.entry_count; ++index) {
                BinaryVirtualPageMapEntryV1 encoded;
                page_map.read(reinterpret_cast<char*>(&encoded),
                              sizeof(encoded));
                if (!page_map || encoded.token == 0 ||
                    encoded.token >= kDestinationClassCountsMarker ||
                    (encoded.flags & ~kKnownVirtualPageMapFlags) != 0 ||
                    ((encoded.flags & kVirtualPageMapInitialPtePresent) != 0 &&
                     (encoded.flags &
                      kVirtualPageMapInitialPteStateValid) == 0) ||
                    ((encoded.flags &
                      kVirtualPageMapMeasurementPtePresent) != 0 &&
                     (encoded.flags &
                      kVirtualPageMapMeasurementPteStateValid) == 0) ||
                    encoded.first_record_ordinal >= record_count_) {
                    throw std::runtime_error(
                        "invalid virtual-page map entry for binary trace: " +
                        path_);
                }
                VirtualPageMapping mapping;
                mapping.token = encoded.token;
                mapping.first_record_ordinal =
                    encoded.first_record_ordinal;
                mapping.virtual_page = encoded.virtual_page;
                mapping.physical_page = encoded.physical_page;
                mapping.physical_page_valid =
                    (encoded.flags & kVirtualPageMapPhysicalValid) != 0;
                mapping.initial_pte_state_valid =
                    (encoded.flags &
                     kVirtualPageMapInitialPteStateValid) != 0;
                mapping.initial_pte_present =
                    (encoded.flags &
                     kVirtualPageMapInitialPtePresent) != 0;
                mapping.measurement_pte_state_valid =
                    (encoded.flags &
                     kVirtualPageMapMeasurementPteStateValid) != 0;
                mapping.measurement_pte_present =
                    (encoded.flags &
                     kVirtualPageMapMeasurementPtePresent) != 0;
                mapping.measurement_boundary_inflight_fault =
                    (encoded.flags &
                     kVirtualPageMapMeasurementBoundaryInflightFault) != 0;
                if (!virtual_page_mappings_
                         .emplace(mapping.token, mapping).second) {
                    throw std::runtime_error(
                        "duplicate virtual-page token in map: " + path_);
                }
            }
        }
        const auto static_map_path = instruction_map_path(path_);
        if (std::filesystem::exists(static_map_path)) {
            std::ifstream static_map(static_map_path, std::ios::binary);
            BinaryInstructionMapHeaderV1 map_header;
            static_map.read(reinterpret_cast<char*>(&map_header),
                            sizeof(map_header));
            const auto map_bytes =
                std::filesystem::file_size(static_map_path);
            const bool map_v1 =
                map_header.magic == kInstructionMapMagicV1 &&
                map_header.version == kInstructionMapVersionV1;
            const bool map_v2 =
                map_header.magic == kInstructionMapMagicV2 &&
                map_header.version == kInstructionMapVersionV2;
            const auto expected_entry_size = map_v2
                ? sizeof(BinaryInstructionMapEntryV2)
                : sizeof(BinaryInstructionMapEntryV1);
            const auto known_header_flags = map_v2
                ? kInstructionMapComplete |
                      kInstructionMapOperandsComplete
                : kInstructionMapComplete;
            const bool valid_isa = map_v1
                ? map_header.reserved == 0
                : map_header.reserved == static_cast<std::uint32_t>(
                      StaticInstructionIsa::kX86_64);
            const bool count_fits =
                map_header.entry_count <=
                static_cast<std::uint64_t>(
                    std::numeric_limits<std::size_t>::max());
            const bool size_fits =
                map_header.entry_count <=
                (std::numeric_limits<std::uint64_t>::max() -
                 sizeof(map_header)) /
                    expected_entry_size;
            if (!static_map ||
                (!map_v1 && !map_v2) ||
                map_header.header_size != sizeof(map_header) ||
                map_header.entry_size != expected_entry_size ||
                map_header.core_id != core_id_ ||
                map_header.source_record_count != record_count_ ||
                (map_header.flags & ~known_header_flags) != 0 ||
                !valid_isa || map_header.entry_count == 0 ||
                !count_fits || !size_fits ||
                map_bytes != sizeof(map_header) +
                    map_header.entry_count *
                        expected_entry_size) {
                throw std::runtime_error(
                    "invalid static instruction map for binary trace: " +
                    path_);
            }
            std::uint64_t previous_pc = 0;
            bool previous_pc_valid = false;
            bool any_operand_semantics = false;
            bool all_operand_semantics = true;
            for (std::uint64_t index = 0;
                 index < map_header.entry_count; ++index) {
                StaticInstructionInfo instruction;
                bool reserved_valid = false;
                if (map_v1) {
                    BinaryInstructionMapEntryV1 encoded;
                    static_map.read(reinterpret_cast<char*>(&encoded),
                                    sizeof(encoded));
                    reserved_valid = std::none_of(
                        encoded.reserved.begin(), encoded.reserved.end(),
                        [](std::uint8_t value) { return value != 0; });
                    instruction.pc = encoded.pc;
                    instruction.fallthrough_pc = encoded.fallthrough_pc;
                    instruction.direct_target = encoded.direct_target;
                    instruction.flags = encoded.flags;
                    instruction.size = encoded.size;
                } else {
                    BinaryInstructionMapEntryV2 encoded;
                    static_map.read(reinterpret_cast<char*>(&encoded),
                                    sizeof(encoded));
                    reserved_valid =
                        (encoded.semantic_flags &
                         ~kInstructionOperandsValid) == 0 &&
                        std::none_of(
                            encoded.reserved.begin(),
                            encoded.reserved.end(),
                            [](std::uint8_t value) {
                                return value != 0;
                            });
                    instruction.pc = encoded.pc;
                    instruction.fallthrough_pc = encoded.fallthrough_pc;
                    instruction.direct_target = encoded.direct_target;
                    instruction.flags = encoded.flags;
                    instruction.size = encoded.size;
                    instruction.operand_semantics_valid =
                        (encoded.semantic_flags &
                         kInstructionOperandsValid) != 0;
                    instruction.read_register_mask =
                        encoded.read_register_mask;
                    instruction.write_register_mask =
                        encoded.write_register_mask;
                }
                if (!static_map || !reserved_valid ||
                    (previous_pc_valid &&
                     instruction.pc <= previous_pc)) {
                    throw std::runtime_error(
                        "invalid static instruction map entry for binary "
                        "trace: " + path_);
                }
                try {
                    validate_static_instruction(instruction);
                } catch (const std::exception& error) {
                    throw std::runtime_error(
                        "invalid static instruction map entry in " + path_ +
                        ": " + error.what());
                }
                static_instruction_map_.emplace(
                    instruction.pc, instruction);
                any_operand_semantics |=
                    instruction.operand_semantics_valid;
                all_operand_semantics &=
                    instruction.operand_semantics_valid;
                previous_pc = instruction.pc;
                previous_pc_valid = true;
            }
            const bool operands_complete =
                (map_header.flags &
                 kInstructionMapOperandsComplete) != 0;
            if ((map_v2 && !any_operand_semantics) ||
                (operands_complete && !all_operand_semantics)) {
                throw std::runtime_error(
                    "invalid static operand coverage in map for binary "
                    "trace: " + path_);
            }
            static_instruction_map_complete_ =
                (map_header.flags & kInstructionMapComplete) != 0;
            static_instruction_isa_ = map_v2
                ? static_cast<StaticInstructionIsa>(map_header.reserved)
                : StaticInstructionIsa::kUnknown;
            static_instruction_operands_complete_ = operands_complete;
            if (address_space_transitions_.size() > 1) {
                // .imap v1/v2 is keyed by virtual PC only. It cannot prove
                // which decoding belongs to which CR3 root, so a multi-AS
                // stream must not feed those facts into speculative I-side
                // reconstruction until an AS-scoped schema exists.
                static_instruction_map_.clear();
                static_instruction_map_complete_ = false;
                static_instruction_operands_complete_ = false;
                static_instruction_isa_ = StaticInstructionIsa::kUnknown;
            }
        }
        input_.clear();
        input_.seekg(sizeof(BinaryTraceHeader));
    }
}

bool BinaryTraceSource::next(TraceRecord& record) {
    current_syscall_metadata_ = nullptr;
    if (records_read_ >= record_count_) {
        if (syscalls_read_ != syscall_metadata_.size()) {
            throw std::runtime_error(
                "unconsumed syscall metadata in binary trace: " + path_);
        }
        return false;
    }
    const auto record_ordinal = records_read_;
    if (!address_space_transitions_.empty()) {
        while (address_space_transition_cursor_ + 1 <
                   address_space_transitions_.size() &&
               address_space_transitions_[
                   address_space_transition_cursor_ + 1]
                       .record_ordinal <= record_ordinal) {
            ++address_space_transition_cursor_;
        }
        current_address_space_id_ =
            address_space_transitions_[address_space_transition_cursor_]
                .address_space_id;
    }
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
    if (has_flag(record.flags, kVirtualPageToken) &&
        record.virtual_page_token() != 0 &&
        !virtual_page_mappings_.empty()) {
        const auto found = virtual_page_mappings_.find(
            record.virtual_page_token());
        if (found == virtual_page_mappings_.end() ||
            found->second.first_record_ordinal > record_ordinal ||
            (!address_space_transitions_.empty() &&
             address_space_id_for_record(
                 found->second.first_record_ordinal) !=
                 current_address_space_id_) ||
            (found->second.physical_page_valid &&
             has_flag(record.flags, kPhysicalAddress) &&
             found->second.physical_page !=
                 (record.address >> kVirtualPageBits))) {
            throw std::runtime_error(
                "hot record disagrees with virtual-page map: " + path_);
        }
    }
    if (!input_) {
        throw std::runtime_error("truncated binary trace: " + path_);
    }
    ++records_read_;
    if (has_syscall_metadata_) {
        const bool row_at_record =
            syscalls_read_ < syscall_metadata_.size() &&
            syscall_metadata_[syscalls_read_].record_ordinal ==
                record_ordinal;
        if (record.is_syscall()) {
            if (!row_at_record) {
                throw std::runtime_error(
                    "syscall record has no aligned metadata row: " + path_);
            }
            const auto& metadata = syscall_metadata_[syscalls_read_++];
            if (metadata.number != record.syscall_number()) {
                throw std::runtime_error(
                    "syscall number disagrees with metadata table: " + path_);
            }
            current_syscall_metadata_ = &metadata;
        } else if (row_at_record ||
                   (syscalls_read_ < syscall_metadata_.size() &&
                    syscall_metadata_[syscalls_read_].record_ordinal <
                        record_ordinal)) {
            throw std::runtime_error(
                "syscall metadata row points at a non-syscall record: " +
                path_);
        }
    } else if (trace_version_ == kTraceVersion && record.is_syscall()) {
        throw std::runtime_error(
            "FST v7 syscall record is missing its metadata table: " + path_);
    }
    return true;
}

std::string BinaryTraceSource::description() const {
    return "fastsim-binary:" + path_;
}

const VirtualPageMapping* BinaryTraceSource::virtual_page_mapping(
    std::uint32_t token) const {
    const auto found = virtual_page_mappings_.find(token);
    return found == virtual_page_mappings_.end() ? nullptr : &found->second;
}

std::uint64_t BinaryTraceSource::address_space_id_for_record(
    std::uint64_t ordinal) const {
    if (ordinal >= record_count_ || address_space_transitions_.empty()) {
        return 0;
    }
    const auto found = std::upper_bound(
        address_space_transitions_.begin(),
        address_space_transitions_.end(), ordinal,
        [](std::uint64_t value, const AddressSpaceTransition& transition) {
            return value < transition.record_ordinal;
        });
    return std::prev(found)->address_space_id;
}

const StaticInstructionInfo* BinaryTraceSource::static_instruction(
    std::uint64_t pc) const {
    const auto found = static_instruction_map_.find(pc);
    return found == static_instruction_map_.end() ? nullptr : &found->second;
}

InstructionSliceTraceSource::InstructionSliceTraceSource(
    std::unique_ptr<TraceSource> source,
    std::uint64_t skip_instructions,
    std::uint64_t take_instructions)
    : source_(std::move(source)),
      skip_instructions_(skip_instructions),
      take_instructions_(take_instructions) {
    if (!source_) {
        throw std::invalid_argument(
            "instruction slice requires a trace source");
    }
    if (take_instructions_ == 0) {
        throw std::invalid_argument(
            "instruction slice take count must be greater than zero");
    }
}

bool InstructionSliceTraceSource::completes_instruction(
    const TraceRecord& record) {
    return record.retires() &&
           (!has_flag(record.flags, kMicroOp) ||
            has_flag(record.flags, kLastMicroOp));
}

void InstructionSliceTraceSource::skip_prefix() {
    if (prefix_skipped_) return;
    TraceRecord record;
    while (skipped_instructions_ < skip_instructions_) {
        if (!source_->next(record)) {
            throw std::runtime_error(
                "trace ended before instruction-slice skip boundary: " +
                source_->description());
        }
        if (completes_instruction(record)) {
            ++skipped_instructions_;
        }
    }
    prefix_skipped_ = true;
}

bool InstructionSliceTraceSource::next(TraceRecord& record) {
    skip_prefix();
    if (emitted_instructions_ >= take_instructions_) return false;
    if (!source_->next(record)) {
        throw std::runtime_error(
            "trace ended before instruction-slice take boundary: " +
            source_->description());
    }
    if (completes_instruction(record)) {
        ++emitted_instructions_;
    }
    return true;
}

std::string InstructionSliceTraceSource::description() const {
    return "instruction-slice:" + source_->description() +
           ":skip=" + std::to_string(skip_instructions_) +
           ":take=" + std::to_string(take_instructions_);
}

WarmupInstructionTraceSource::WarmupInstructionTraceSource(
    std::unique_ptr<TraceSource> source,
    std::uint64_t warmup_instructions,
    std::uint64_t take_instructions,
    std::uint64_t warmup_records,
    std::uint64_t take_records,
    bool has_record_counts)
    : source_(std::move(source)),
      warmup_instructions_(warmup_instructions),
      take_instructions_(take_instructions),
      warmup_records_(warmup_records),
      take_records_(take_records),
      has_record_counts_(has_record_counts),
      boundary_pending_(has_record_counts ? warmup_records == 0
                                          : warmup_instructions == 0) {
    if (!source_) {
        throw std::invalid_argument(
            "functional warmup requires a trace source");
    }
    if (take_instructions_ == 0) {
        throw std::invalid_argument(
            "functional warmup take count must be greater than zero");
    }
    if (has_record_counts_ && take_records_ == 0) {
        throw std::invalid_argument(
            "functional measurement record count must be greater than zero");
    }
    if (has_record_counts_ && warmup_records_ == 0 &&
        warmup_instructions_ != 0) {
        throw std::invalid_argument(
            "zero-record functional warmup cannot contain instructions");
    }
}

bool WarmupInstructionTraceSource::completes_instruction(
    const TraceRecord& record) {
    return record.retires() &&
           (!has_flag(record.flags, kMicroOp) ||
            has_flag(record.flags, kLastMicroOp));
}

bool WarmupInstructionTraceSource::next(TraceRecord& record) {
    if (boundary_pending_) return false;
    const bool measurement_complete =
        has_record_counts_
            ? measurement_records_emitted_ >= take_records_
            : measurement_emitted_ >= take_instructions_;
    if (measuring_ && measurement_complete) {
        return false;
    }
    if (!source_->next(record)) {
        const auto phase = measuring_ ? "measurement" : "warmup";
        throw std::runtime_error(
            "trace ended before functional " + std::string(phase) +
            " instruction boundary: " + source_->description());
    }
    const bool completes = completes_instruction(record);
    if (measuring_) {
        ++measurement_records_emitted_;
        if (completes) {
            ++measurement_emitted_;
        }
        if (has_record_counts_ &&
            measurement_records_emitted_ == take_records_ &&
            measurement_emitted_ != take_instructions_) {
            throw std::runtime_error(
                "functional measurement record/instruction count mismatch: " +
                source_->description());
        }
    } else {
        ++warmup_records_emitted_;
        if (completes) {
            ++warmup_emitted_;
        }
        const bool warmup_complete =
            has_record_counts_
                ? warmup_records_emitted_ == warmup_records_
                : warmup_emitted_ == warmup_instructions_;
        if (warmup_complete) {
            if (has_record_counts_ &&
                warmup_emitted_ != warmup_instructions_) {
                throw std::runtime_error(
                    "functional warmup record/instruction count mismatch: " +
                    source_->description());
            }
            boundary_pending_ = true;
        }
    }
    return true;
}

void WarmupInstructionTraceSource::start_measurement() {
    if (measuring_ || !boundary_pending_ ||
        warmup_emitted_ != warmup_instructions_) {
        throw std::logic_error(
            "functional measurement released outside its boundary");
    }
    boundary_pending_ = false;
    measuring_ = true;
}

std::string WarmupInstructionTraceSource::description() const {
    return "functional-warmup:" + source_->description() +
           ":warmup=" + std::to_string(warmup_instructions_) +
           ":take=" + std::to_string(take_instructions_) +
           (has_record_counts_
                ? ":warmup-records=" + std::to_string(warmup_records_) +
                      ":take-records=" + std::to_string(take_records_)
                : "");
}

BinaryTraceWriter::BinaryTraceWriter(std::string path, std::uint32_t core_id,
                                     SyscallAbi syscall_abi)
    : path_(std::move(path)),
      output_(path_, std::ios::in | std::ios::out | std::ios::binary |
                        std::ios::trunc),
      core_id_(core_id),
      syscall_abi_(syscall_abi) {
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
    if (!syscall_metadata_.empty()) {
        header.reserved[0] =
            sizeof(BinaryTraceHeader) + record_count_ * sizeof(TraceRecord);
        header.reserved[1] = syscall_metadata_.size();
        header.reserved[2] = sizeof(BinarySyscallMetadataV1);
    }
    header.reserved[3] = static_cast<std::uint64_t>(syscall_abi_);
    output_.seekp(0);
    output_.write(reinterpret_cast<const char*>(&header), sizeof(header));
    if (!output_) {
        throw std::runtime_error("failed writing binary trace header: " +
                                 path_);
    }
}

void BinaryTraceWriter::append(const TraceRecord& record) {
    append(record, nullptr);
}

void BinaryTraceWriter::append(const TraceRecord& record,
                               const SyscallMetadata* syscall_metadata) {
    if (closed_) throw std::logic_error("binary trace writer is closed");
    if (syscall_metadata_written_) {
        throw std::logic_error(
            "cannot append records after syscall metadata was written");
    }
    if (!record.is_syscall() && syscall_metadata != nullptr) {
        throw std::invalid_argument(
            "syscall metadata supplied for a non-syscall record");
    }
    SyscallMetadata prepared_metadata;
    if (record.is_syscall()) {
        prepared_metadata = syscall_metadata == nullptr
                                ? SyscallMetadata{}
                                : *syscall_metadata;
        if (syscall_metadata != nullptr &&
            prepared_metadata.number != record.syscall_number()) {
            throw std::invalid_argument(
                "syscall number disagrees with supplied metadata");
        }
        prepared_metadata.record_ordinal = record_count_;
        prepared_metadata.syscall_ordinal = syscall_metadata_.size();
        prepared_metadata.number = record.syscall_number();
        validate_syscall_metadata(prepared_metadata);
    }
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
        feature_flags_ |= kFeatureSyscallMetadata;
        syscall_metadata_.push_back(prepared_metadata);
    }
    if (record.has_destination_class_counts()) {
        feature_flags_ |= kFeatureDestinationClassCounts;
    }
    ++record_count_;
}

void BinaryTraceWriter::register_virtual_page_mapping(
    const VirtualPageMapping& mapping) {
    if (closed_) throw std::logic_error("binary trace writer is closed");
    if (mapping.token == 0 ||
        mapping.token >= kDestinationClassCountsMarker ||
        mapping.first_record_ordinal > record_count_ ||
        (mapping.initial_pte_present &&
         !mapping.initial_pte_state_valid) ||
        (mapping.measurement_pte_present &&
         !mapping.measurement_pte_state_valid)) {
        throw std::invalid_argument("invalid virtual-page mapping");
    }
    const auto found = virtual_page_mappings_.find(mapping.token);
    if (found != virtual_page_mappings_.end()) {
        const auto& prior = found->second;
        if (prior.first_record_ordinal != mapping.first_record_ordinal ||
            prior.virtual_page != mapping.virtual_page ||
            prior.physical_page != mapping.physical_page ||
            prior.physical_page_valid != mapping.physical_page_valid ||
            prior.initial_pte_state_valid !=
                mapping.initial_pte_state_valid ||
            prior.initial_pte_present != mapping.initial_pte_present ||
            prior.measurement_pte_state_valid !=
                mapping.measurement_pte_state_valid ||
            prior.measurement_pte_present !=
                mapping.measurement_pte_present ||
            prior.measurement_boundary_inflight_fault !=
                mapping.measurement_boundary_inflight_fault) {
            throw std::invalid_argument(
                "virtual-page token maps to multiple identities");
        }
        return;
    }
    virtual_page_mappings_.emplace(mapping.token, mapping);
}

void BinaryTraceWriter::set_address_space_id(
    std::uint64_t address_space_id) {
    if (closed_) throw std::logic_error("binary trace writer is closed");
    if (address_space_id == 0) {
        if (!address_space_transitions_.empty()) {
            throw std::invalid_argument(
                "explicit address-space trace cannot return to unspecified "
                "address space zero");
        }
        current_address_space_id_ = 0;
        return;
    }
    if (record_count_ != 0 && address_space_transitions_.empty()) {
        throw std::invalid_argument(
            "explicit address space must be set before record zero");
    }
    if (!address_space_transitions_.empty() &&
        current_address_space_id_ == address_space_id) {
        return;
    }
    address_space_transitions_.push_back(
        AddressSpaceTransition{record_count_, address_space_id});
    current_address_space_id_ = address_space_id;
}

void BinaryTraceWriter::register_static_instruction(
    const StaticInstructionInfo& instruction) {
    if (closed_) throw std::logic_error("binary trace writer is closed");
    validate_static_instruction(instruction);
    const auto found = static_instruction_map_.find(instruction.pc);
    if (found != static_instruction_map_.end()) {
        const auto& prior = found->second;
        if (prior.fallthrough_pc != instruction.fallthrough_pc ||
            prior.direct_target != instruction.direct_target ||
            prior.flags != instruction.flags ||
            prior.size != instruction.size ||
            prior.operand_semantics_valid !=
                instruction.operand_semantics_valid ||
            prior.read_register_mask != instruction.read_register_mask ||
            prior.write_register_mask != instruction.write_register_mask) {
            throw std::invalid_argument(
                "static instruction PC maps to multiple decodings");
        }
        return;
    }
    static_instruction_map_.emplace(instruction.pc, instruction);
}

void BinaryTraceWriter::close() {
    if (closed_) return;
    if (static_instruction_map_complete_ &&
        static_instruction_map_.empty()) {
        throw std::runtime_error(
            "complete static instruction map cannot be empty");
    }
    const bool any_operand_semantics = std::any_of(
        static_instruction_map_.begin(), static_instruction_map_.end(),
        [](const auto& item) {
            return item.second.operand_semantics_valid;
        });
    const bool all_operand_semantics =
        !static_instruction_map_.empty() && std::all_of(
            static_instruction_map_.begin(),
            static_instruction_map_.end(),
            [](const auto& item) {
                return item.second.operand_semantics_valid;
            });
    if (any_operand_semantics &&
        static_instruction_isa_ != StaticInstructionIsa::kX86_64) {
        throw std::runtime_error(
            "static operand semantics require the x86-64 ISA namespace");
    }
    if (!any_operand_semantics &&
        static_instruction_isa_ != StaticInstructionIsa::kUnknown) {
        throw std::runtime_error(
            "static instruction ISA requires decoded operand semantics");
    }
    if (!syscall_metadata_written_) {
        output_.seekp(0, std::ios::end);
        for (const auto& metadata : syscall_metadata_) {
            const auto encoded = encode_syscall_metadata(metadata);
            output_.write(reinterpret_cast<const char*>(&encoded),
                          sizeof(encoded));
        }
        syscall_metadata_written_ = true;
        if (!output_) {
            throw std::runtime_error(
                "failed writing syscall metadata table: " + path_);
        }
    }
    write_header();
    output_.flush();
    if (!output_) {
        throw std::runtime_error("failed finalizing binary trace: " + path_);
    }
    output_.close();

    const auto page_map_path = virtual_page_map_path(path_);
    if (!virtual_page_mappings_.empty()) {
        std::ofstream page_map(
            page_map_path, std::ios::binary | std::ios::trunc);
        BinaryVirtualPageMapHeaderV1 header;
        header.magic = kVirtualPageMapMagic;
        header.entry_size = sizeof(BinaryVirtualPageMapEntryV1);
        header.core_id = core_id_;
        header.source_record_count = record_count_;
        header.entry_count = virtual_page_mappings_.size();
        page_map.write(reinterpret_cast<const char*>(&header),
                       sizeof(header));
        for (const auto& [token, mapping] : virtual_page_mappings_) {
            BinaryVirtualPageMapEntryV1 encoded;
            encoded.token = token;
            encoded.first_record_ordinal = mapping.first_record_ordinal;
            encoded.virtual_page = mapping.virtual_page;
            encoded.physical_page = mapping.physical_page;
            if (mapping.physical_page_valid) {
                encoded.flags |= kVirtualPageMapPhysicalValid;
            }
            if (mapping.initial_pte_state_valid) {
                encoded.flags |= kVirtualPageMapInitialPteStateValid;
                if (mapping.initial_pte_present) {
                    encoded.flags |= kVirtualPageMapInitialPtePresent;
                }
            } else if (mapping.initial_pte_present) {
                throw std::runtime_error(
                    "initial PTE present bit lacks a valid state");
            }
            if (mapping.measurement_pte_state_valid) {
                encoded.flags |=
                    kVirtualPageMapMeasurementPteStateValid;
                if (mapping.measurement_pte_present) {
                    encoded.flags |=
                        kVirtualPageMapMeasurementPtePresent;
                }
            } else if (mapping.measurement_pte_present) {
                throw std::runtime_error(
                    "measurement PTE present bit lacks a valid state");
            }
            if (mapping.measurement_boundary_inflight_fault) {
                encoded.flags |=
                    kVirtualPageMapMeasurementBoundaryInflightFault;
            }
            page_map.write(reinterpret_cast<const char*>(&encoded),
                           sizeof(encoded));
        }
        page_map.flush();
        if (!page_map) {
            throw std::runtime_error(
                "failed writing virtual-page map: " + page_map_path);
        }
    } else {
        std::error_code error;
        std::filesystem::remove(page_map_path, error);
        if (error) {
            throw std::runtime_error(
                "failed removing stale virtual-page map: " +
                page_map_path + ": " + error.message());
        }
    }
    const auto as_map_path = address_space_map_path(path_);
    if (!address_space_transitions_.empty()) {
        if (record_count_ == 0 ||
            address_space_transitions_.front().record_ordinal != 0) {
            throw std::runtime_error(
                "address-space map must start at record zero");
        }
        std::ofstream as_map(
            as_map_path, std::ios::binary | std::ios::trunc);
        BinaryAddressSpaceMapHeaderV1 header;
        header.magic = kAddressSpaceMapMagic;
        header.entry_size = sizeof(BinaryAddressSpaceMapEntryV1);
        header.core_id = core_id_;
        header.source_record_count = record_count_;
        header.entry_count = address_space_transitions_.size();
        as_map.write(reinterpret_cast<const char*>(&header),
                     sizeof(header));
        for (const auto& transition : address_space_transitions_) {
            BinaryAddressSpaceMapEntryV1 encoded;
            encoded.record_ordinal = transition.record_ordinal;
            encoded.address_space_id = transition.address_space_id;
            as_map.write(reinterpret_cast<const char*>(&encoded),
                         sizeof(encoded));
        }
        as_map.flush();
        if (!as_map) {
            throw std::runtime_error(
                "failed writing address-space map: " + as_map_path);
        }
    } else {
        std::error_code error;
        std::filesystem::remove(as_map_path, error);
        if (error) {
            throw std::runtime_error(
                "failed removing stale address-space map: " +
                as_map_path + ": " + error.message());
        }
    }
    const auto static_map_path = instruction_map_path(path_);
    if (!static_instruction_map_.empty()) {
        std::ofstream static_map(
            static_map_path, std::ios::binary | std::ios::trunc);
        BinaryInstructionMapHeaderV1 header;
        header.magic = any_operand_semantics
            ? kInstructionMapMagicV2
            : kInstructionMapMagicV1;
        header.version = any_operand_semantics
            ? kInstructionMapVersionV2
            : kInstructionMapVersionV1;
        header.entry_size = any_operand_semantics
            ? sizeof(BinaryInstructionMapEntryV2)
            : sizeof(BinaryInstructionMapEntryV1);
        header.core_id = core_id_;
        header.source_record_count = record_count_;
        header.entry_count = static_instruction_map_.size();
        if (static_instruction_map_complete_) {
            header.flags |= kInstructionMapComplete;
        }
        if (all_operand_semantics) {
            header.flags |= kInstructionMapOperandsComplete;
        }
        if (any_operand_semantics) {
            header.reserved = static_cast<std::uint32_t>(
                static_instruction_isa_);
        }
        static_map.write(reinterpret_cast<const char*>(&header),
                         sizeof(header));
        for (const auto& [pc, instruction] : static_instruction_map_) {
            (void)pc;
            if (any_operand_semantics) {
                BinaryInstructionMapEntryV2 encoded;
                encoded.pc = instruction.pc;
                encoded.fallthrough_pc = instruction.fallthrough_pc;
                encoded.direct_target = instruction.direct_target;
                encoded.flags = instruction.flags;
                encoded.size = instruction.size;
                if (instruction.operand_semantics_valid) {
                    encoded.semantic_flags |=
                        kInstructionOperandsValid;
                }
                encoded.read_register_mask =
                    instruction.read_register_mask;
                encoded.write_register_mask =
                    instruction.write_register_mask;
                static_map.write(
                    reinterpret_cast<const char*>(&encoded),
                    sizeof(encoded));
            } else {
                BinaryInstructionMapEntryV1 encoded;
                encoded.pc = instruction.pc;
                encoded.fallthrough_pc = instruction.fallthrough_pc;
                encoded.direct_target = instruction.direct_target;
                encoded.flags = instruction.flags;
                encoded.size = instruction.size;
                static_map.write(
                    reinterpret_cast<const char*>(&encoded),
                    sizeof(encoded));
            }
        }
        static_map.flush();
        if (!static_map) {
            throw std::runtime_error(
                "failed writing static instruction map: " +
                static_map_path);
        }
    } else {
        std::error_code error;
        std::filesystem::remove(static_map_path, error);
        if (error) {
            throw std::runtime_error(
                "failed removing stale static instruction map: " +
                static_map_path + ": " + error.message());
        }
    }
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
                ": expected '<core-id> <format> <path> ...'");
        }
        std::string source_core;
        if (entry.format == "fastsim-binary-slice" ||
            entry.format == "binary-slice" ||
            entry.format == "fastsim-binary-warmup-slice" ||
            entry.format == "binary-warmup-slice") {
            const bool measurement_warmup =
                entry.format == "fastsim-binary-warmup-slice" ||
                entry.format == "binary-warmup-slice";
            std::string prefix_instructions;
            std::string take_instructions;
            if (!(parser >> source_core >> prefix_instructions >>
                  take_instructions)) {
                throw std::runtime_error(
                    manifest_path + ":" + std::to_string(line_number) +
                    (measurement_warmup
                         ? ": binary warmup slice expects '<source-core-id> "
                           "<warmup-instructions> <take-instructions>'"
                         : ": binary slice expects '<source-core-id> "
                           "<skip-instructions> <take-instructions>'"));
            }
            const auto parse_u64 = [&](const std::string& text,
                                       const char* field) {
                std::size_t consumed = 0;
                const auto value = std::stoull(text, &consumed, 10);
                if (consumed != text.size()) {
                    throw std::runtime_error(
                        manifest_path + ":" +
                        std::to_string(line_number) + ": invalid " + field);
                }
                return static_cast<std::uint64_t>(value);
            };
            const auto source_value =
                parse_u64(source_core, "source core ID");
            if (source_value >
                std::numeric_limits<std::uint32_t>::max()) {
                throw std::runtime_error(
                    manifest_path + ":" + std::to_string(line_number) +
                    ": invalid source core ID");
            }
            entry.source_core_id =
                static_cast<std::uint32_t>(source_value);
            entry.has_source_core_id = true;
            const auto prefix_count = parse_u64(
                prefix_instructions,
                measurement_warmup ? "warmup instruction count"
                                   : "skip instruction count");
            if (measurement_warmup) {
                entry.warmup_instructions = prefix_count;
                entry.has_measurement_warmup = true;
            } else {
                entry.skip_instructions = prefix_count;
                entry.has_instruction_slice = true;
            }
            entry.take_instructions =
                parse_u64(take_instructions, "take instruction count");
            if (entry.take_instructions == 0 ||
                (!measurement_warmup && prefix_count == 0)) {
                throw std::runtime_error(
                    manifest_path + ":" + std::to_string(line_number) +
                    (measurement_warmup
                         ? ": take instruction count must be greater than zero"
                         : ": skip and take instruction counts must be greater "
                           "than zero"));
            }
            std::string warmup_records;
            if (parser >> warmup_records) {
                if (!measurement_warmup) {
                    throw std::runtime_error(
                        manifest_path + ":" + std::to_string(line_number) +
                        ": unexpected trailing manifest field");
                }
                std::string take_records;
                if (!(parser >> take_records)) {
                    throw std::runtime_error(
                        manifest_path + ":" + std::to_string(line_number) +
                        ": binary warmup record counts expect both "
                        "'<warmup-records> <take-records>'");
                }
                entry.warmup_records =
                    parse_u64(warmup_records, "warmup record count");
                entry.take_records =
                    parse_u64(take_records, "take record count");
                entry.has_record_counts = true;
                if (entry.take_records == 0 ||
                    (entry.warmup_records == 0 && prefix_count != 0)) {
                    throw std::runtime_error(
                        manifest_path + ":" + std::to_string(line_number) +
                        ": invalid binary warmup record counts");
                }
                std::string extra;
                if (parser >> extra) {
                    throw std::runtime_error(
                        manifest_path + ":" + std::to_string(line_number) +
                        ": unexpected trailing manifest field");
                }
            }
        } else if (parser >> source_core) {
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
                   entry.format == "binary" ||
                   entry.format == "fastsim-binary-slice" ||
                   entry.format == "binary-slice" ||
                   entry.format == "fastsim-binary-warmup-slice" ||
                   entry.format == "binary-warmup-slice") {
            auto source = std::make_unique<BinaryTraceSource>(entry.path);
            const auto expected_source_core =
                entry.has_source_core_id ? entry.source_core_id : core;
            if (source->core_id() != expected_source_core) {
                throw std::runtime_error(
                    "binary trace source core ID does not match manifest: " +
                    entry.path);
            }
            if (entry.has_measurement_warmup) {
                sources.push_back(
                    std::make_unique<WarmupInstructionTraceSource>(
                        std::move(source), entry.warmup_instructions,
                        entry.take_instructions, entry.warmup_records,
                        entry.take_records, entry.has_record_counts));
            } else if (entry.has_instruction_slice) {
                sources.push_back(
                    std::make_unique<InstructionSliceTraceSource>(
                        std::move(source), entry.skip_instructions,
                        entry.take_instructions));
            } else {
                sources.push_back(std::move(source));
            }
        } else {
            throw std::runtime_error("unknown trace format: " +
                                     entry.format);
        }
    }
    return sources;
}

SyscallAbi parse_syscall_abi(const std::string& name) {
    std::string normalized = name;
    std::transform(normalized.begin(), normalized.end(), normalized.begin(),
                   [](unsigned char character) {
                       return static_cast<char>(std::tolower(character));
                   });
    std::replace(normalized.begin(), normalized.end(), '_', '-');
    if (normalized == "unknown" || normalized == "none") {
        return SyscallAbi::kUnknown;
    }
    if (normalized == "linux-x86-64" || normalized == "linux-x86_64" ||
        normalized == "x86-64" || normalized == "x86_64" ||
        normalized == "amd64") {
        return SyscallAbi::kLinuxX86_64;
    }
    if (normalized == "linux-x86-32" || normalized == "linux-x86" ||
        normalized == "x86-32" || normalized == "x86" ||
        normalized == "i386") {
        return SyscallAbi::kLinuxX86_32;
    }
    if (normalized == "linux-aarch64" || normalized == "aarch64" ||
        normalized == "arm64") {
        return SyscallAbi::kLinuxAArch64;
    }
    if (normalized == "linux-arm32" || normalized == "arm32" ||
        normalized == "arm") {
        return SyscallAbi::kLinuxArm32;
    }
    throw std::invalid_argument("unknown syscall ABI: " + name);
}

std::string syscall_abi_name(SyscallAbi abi) {
    switch (abi) {
        case SyscallAbi::kUnknown:
            return "unknown";
        case SyscallAbi::kLinuxX86_64:
            return "linux-x86_64";
        case SyscallAbi::kLinuxX86_32:
            return "linux-x86_32";
        case SyscallAbi::kLinuxAArch64:
            return "linux-aarch64";
        case SyscallAbi::kLinuxArm32:
            return "linux-arm32";
    }
    throw std::invalid_argument("invalid syscall ABI value");
}

namespace {

void write_syscall_json(std::ostream& output, std::uint32_t core_id,
                        const TraceRecord& record,
                        const SyscallMetadata& metadata, SyscallAbi abi) {
    output << "{\"schema\":\"fastsim-functional-syscall-v2\""
           << ",\"event\":\"syscall\""
           << ",\"abi\":\"" << syscall_abi_name(abi) << "\""
           << ",\"core_id\":" << core_id
           << ",\"record_ordinal\":" << metadata.record_ordinal
           << ",\"syscall_ordinal\":" << metadata.syscall_ordinal
           << ",\"pc\":" << record.pc
           << ",\"syscall_nr\":" << metadata.number
           << ",\"capture_flags\":" << metadata.valid_fields;
    if (metadata.has(kSyscallThreadIdValid)) {
        output << ",\"thread_id\":" << metadata.thread_id;
    }
    if (metadata.has(kSyscallArgumentsValid)) {
        output << ",\"arg_count\":"
               << static_cast<unsigned int>(metadata.argument_count)
               << ",\"args\":[";
        for (std::size_t index = 0; index < metadata.argument_count;
             ++index) {
            if (index != 0) output << ',';
            output << metadata.arguments[index];
        }
        output << ']';
    }
    if (metadata.has(kSyscallReturnValueValid)) {
        output << ",\"retval_raw\":" << metadata.return_value_raw;
    }
    if (metadata.has(kSyscallFailureValid)) {
        output << ",\"failed\":"
               << (metadata.failed ? "true" : "false");
    }
    if (metadata.has(kSyscallErrnoValid)) {
        output << ",\"errno\":" << metadata.errno_value;
    }
    if (metadata.has(kSyscallPreTimestampValid)) {
        output << ",\"pre_timestamp_us\":"
               << metadata.pre_timestamp_us;
    }
    if (metadata.has(kSyscallPostTimestampValid)) {
        output << ",\"post_timestamp_us\":"
               << metadata.post_timestamp_us;
    }
    if (metadata.has(kSyscallPreCpuValid)) {
        output << ",\"pre_cpu\":" << metadata.pre_cpu;
    }
    if (metadata.has(kSyscallPostCpuValid)) {
        output << ",\"post_cpu\":" << metadata.post_cpu;
    }
    if (metadata.has(kSyscallMaybeBlockingValid)) {
        output << ",\"maybe_blocking\":"
               << (metadata.maybe_blocking ? "true" : "false");
    }
    if (metadata.has(kSyscallPreTimestampValid) ||
        metadata.has(kSyscallPostTimestampValid)) {
        output << ",\"timestamp_unit\":\"microseconds\"";
    }
    output << "}\n";
}

}  // namespace

void convert_gem5_jsonl_to_binary(
    const std::string& input_path, const std::string& output_path,
    std::uint32_t core_id, const std::string& syscall_output_path,
    SyscallAbi syscall_abi) {
    Gem5JsonlTraceSource input(input_path);
    BinaryTraceWriter output(output_path, core_id, syscall_abi);
    std::ofstream syscall_output;
    if (!syscall_output_path.empty()) {
        syscall_output.open(syscall_output_path, std::ios::trunc);
        if (!syscall_output) {
            throw std::runtime_error(
                "cannot create syscall JSONL sidecar: " +
                syscall_output_path);
        }
    }
    TraceRecord record;
    while (input.next(record)) {
        output.set_address_space_id(input.current_address_space_id());
        const auto* metadata = input.current_syscall_metadata();
        if (has_flag(record.flags, kVirtualPageToken)) {
            const auto* mapping = input.virtual_page_mapping(
                record.virtual_page_token());
            if (mapping != nullptr) {
                output.register_virtual_page_mapping(*mapping);
            }
        }
        output.append(record, metadata);
        if (metadata != nullptr && syscall_output) {
            write_syscall_json(syscall_output, core_id, record, *metadata,
                               syscall_abi);
        }
    }
    output.close();
    if (syscall_output) {
        syscall_output.flush();
        if (!syscall_output) {
            throw std::runtime_error(
                "failed writing syscall JSONL sidecar: " +
                syscall_output_path);
        }
    }
}

void upgrade_binary_trace_to_v7(const std::string& input_path,
                                const std::string& output_path,
                                SyscallAbi syscall_abi) {
    if (std::filesystem::weakly_canonical(input_path) ==
        std::filesystem::weakly_canonical(output_path)) {
        throw std::invalid_argument(
            "FST upgrade input and output must be different files");
    }
    BinaryTraceSource input(input_path);
    BinaryTraceWriter output(output_path, input.core_id(), syscall_abi);
    TraceRecord record;
    while (input.next(record)) {
        output.set_address_space_id(input.current_address_space_id());
        if (has_flag(record.flags, kVirtualPageToken)) {
            const auto* mapping = input.virtual_page_mapping(
                record.virtual_page_token());
            if (mapping != nullptr) {
                output.register_virtual_page_mapping(*mapping);
            }
        }
        output.append(record, input.current_syscall_metadata());
    }
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
