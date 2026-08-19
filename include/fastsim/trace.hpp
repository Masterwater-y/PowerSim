#pragma once

#include <cstdint>
#include <fstream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "fastsim/types.hpp"

namespace fastsim {

class TraceSource {
  public:
    virtual ~TraceSource() = default;
    virtual bool next(TraceRecord& record) = 0;
    virtual std::string description() const = 0;
    // Valid until the next call to next().  A null pointer means the current
    // record is not a syscall or the source is a legacy trace without the
    // portable syscall metadata table.
    virtual const SyscallMetadata* current_syscall_metadata() const {
        return nullptr;
    }
    virtual const VirtualPageMapping* virtual_page_mapping(
        std::uint32_t) const {
        return nullptr;
    }
    // Cold companion metadata, exposed for deterministic process-wide page
    // ownership before parallel trace producers start. Streaming JSONL
    // sources may return an empty map until decoded; canonical binary FST
    // sources have the complete map at construction time.
    virtual const std::map<std::uint32_t, VirtualPageMapping>*
    all_virtual_page_mappings() const {
        return nullptr;
    }
    // Address-space identity for the record most recently returned by
    // next().  Legacy/single-process sources return zero (unspecified).
    virtual std::uint64_t current_address_space_id() const { return 0; }
    // Cold ordinal lookup used while process-wide PTE/page state is built
    // before replay workers start.  It refers to source ordinals, so slicing
    // wrappers deliberately delegate without rebasing.
    virtual std::uint64_t address_space_id_for_record(
        std::uint64_t) const {
        return 0;
    }
    virtual const std::vector<AddressSpaceTransition>*
    address_space_transitions() const {
        return nullptr;
    }
    virtual const StaticInstructionInfo* static_instruction(
        std::uint64_t) const {
        return nullptr;
    }
    virtual bool static_instruction_map_complete() const { return false; }
    virtual StaticInstructionIsa static_instruction_isa() const {
        return StaticInstructionIsa::kUnknown;
    }
    virtual bool static_instruction_operands_complete() const {
        return false;
    }
    virtual bool has_measurement_boundary() const { return false; }
    virtual bool measurement_boundary_pending() const { return false; }
    virtual void start_measurement() {
        throw std::logic_error(
            "trace source has no measurement boundary");
    }
};

class Gem5JsonlTraceSource final : public TraceSource {
  public:
    explicit Gem5JsonlTraceSource(std::string path);
    bool next(TraceRecord& record) override;
    std::string description() const override;
    const SyscallMetadata* current_syscall_metadata() const override {
        return has_current_syscall_metadata_ ? &current_syscall_metadata_
                                             : nullptr;
    }
    const VirtualPageMapping* virtual_page_mapping(
        std::uint32_t token) const override;
    const std::map<std::uint32_t, VirtualPageMapping>*
    all_virtual_page_mappings() const override {
        return &virtual_page_mappings_;
    }
    std::uint64_t current_address_space_id() const override {
        return current_address_space_id_;
    }
    std::uint64_t address_space_id_for_record(
        std::uint64_t ordinal) const override;
    const std::vector<AddressSpaceTransition>*
    address_space_transitions() const override {
        return address_space_transitions_.empty()
                   ? nullptr
                   : &address_space_transitions_;
    }

  private:
    std::string path_;
    std::ifstream input_;
    std::uint64_t line_number_ = 0;
    std::map<std::tuple<std::uint64_t, std::uint64_t, std::uint64_t>,
             std::uint32_t>
        virtual_page_tokens_;
    std::uint32_t next_virtual_page_token_ = 1;
    std::map<std::uint32_t, VirtualPageMapping> virtual_page_mappings_;
    std::uint64_t records_emitted_ = 0;
    std::uint64_t syscalls_emitted_ = 0;
    SyscallMetadata current_syscall_metadata_{};
    bool has_current_syscall_metadata_ = false;
    std::uint64_t current_address_space_id_ = 0;
    std::vector<AddressSpaceTransition> address_space_transitions_;
};

class BinaryTraceSource final : public TraceSource {
  public:
    explicit BinaryTraceSource(std::string path);
    bool next(TraceRecord& record) override;
    std::string description() const override;
    const SyscallMetadata* current_syscall_metadata() const override {
        return current_syscall_metadata_;
    }
    std::uint32_t core_id() const { return core_id_; }
    std::uint64_t record_count() const { return record_count_; }
    SyscallAbi syscall_abi() const { return syscall_abi_; }
    const VirtualPageMapping* virtual_page_mapping(
        std::uint32_t token) const override;
    const std::map<std::uint32_t, VirtualPageMapping>*
    all_virtual_page_mappings() const override {
        return &virtual_page_mappings_;
    }
    bool has_virtual_page_map() const {
        return !virtual_page_mappings_.empty();
    }
    std::uint64_t current_address_space_id() const override {
        return current_address_space_id_;
    }
    std::uint64_t address_space_id_for_record(
        std::uint64_t ordinal) const override;
    const std::vector<AddressSpaceTransition>*
    address_space_transitions() const override {
        return address_space_transitions_.empty()
                   ? nullptr
                   : &address_space_transitions_;
    }
    const StaticInstructionInfo* static_instruction(
        std::uint64_t pc) const override;
    bool static_instruction_map_complete() const override {
        return static_instruction_map_complete_;
    }
    StaticInstructionIsa static_instruction_isa() const override {
        return static_instruction_isa_;
    }
    bool static_instruction_operands_complete() const override {
        return static_instruction_operands_complete_;
    }

  private:
    std::string path_;
    std::ifstream input_;
    std::uint32_t core_id_ = 0;
    std::uint64_t record_count_ = 0;
    std::uint64_t records_read_ = 0;
    std::uint32_t trace_version_ = 0;
    bool legacy_v2_ = false;
    bool has_syscall_metadata_ = false;
    std::vector<TraceRecord> buffer_;
    std::size_t buffer_cursor_ = 0;
    std::size_t buffer_size_ = 0;
    std::vector<SyscallMetadata> syscall_metadata_;
    std::size_t syscalls_read_ = 0;
    const SyscallMetadata* current_syscall_metadata_ = nullptr;
    SyscallAbi syscall_abi_ = SyscallAbi::kUnknown;
    std::map<std::uint32_t, VirtualPageMapping> virtual_page_mappings_;
    std::map<std::uint64_t, StaticInstructionInfo>
        static_instruction_map_;
    bool static_instruction_map_complete_ = false;
    StaticInstructionIsa static_instruction_isa_ =
        StaticInstructionIsa::kUnknown;
    bool static_instruction_operands_complete_ = false;
    std::vector<AddressSpaceTransition> address_space_transitions_;
    std::size_t address_space_transition_cursor_ = 0;
    std::uint64_t current_address_space_id_ = 0;
};

// Exposes a macro-instruction-aligned subrange of another functional trace.
// This is used to remove functional warmup prefixes without copying large FST
// files. The wrapper fails closed if the source ends before either boundary.
class InstructionSliceTraceSource final : public TraceSource {
  public:
    InstructionSliceTraceSource(
        std::unique_ptr<TraceSource> source,
        std::uint64_t skip_instructions,
        std::uint64_t take_instructions);
    bool next(TraceRecord& record) override;
    std::string description() const override;
    const SyscallMetadata* current_syscall_metadata() const override {
        return source_->current_syscall_metadata();
    }
    const VirtualPageMapping* virtual_page_mapping(
        std::uint32_t token) const override {
        return source_->virtual_page_mapping(token);
    }
    const std::map<std::uint32_t, VirtualPageMapping>*
    all_virtual_page_mappings() const override {
        return source_->all_virtual_page_mappings();
    }
    std::uint64_t current_address_space_id() const override {
        return source_->current_address_space_id();
    }
    std::uint64_t address_space_id_for_record(
        std::uint64_t ordinal) const override {
        return source_->address_space_id_for_record(ordinal);
    }
    const std::vector<AddressSpaceTransition>*
    address_space_transitions() const override {
        return source_->address_space_transitions();
    }
    const StaticInstructionInfo* static_instruction(
        std::uint64_t pc) const override {
        return source_->static_instruction(pc);
    }
    bool static_instruction_map_complete() const override {
        return source_->static_instruction_map_complete();
    }
    StaticInstructionIsa static_instruction_isa() const override {
        return source_->static_instruction_isa();
    }
    bool static_instruction_operands_complete() const override {
        return source_->static_instruction_operands_complete();
    }

  private:
    static bool completes_instruction(const TraceRecord& record);
    void skip_prefix();

    std::unique_ptr<TraceSource> source_;
    std::uint64_t skip_instructions_ = 0;
    std::uint64_t take_instructions_ = 0;
    std::uint64_t skipped_instructions_ = 0;
    std::uint64_t emitted_instructions_ = 0;
    bool prefix_skipped_ = false;
};

// Replays a functional warmup prefix, pauses at its producer-defined record
// boundary, then resumes for a bounded measurement interval. Optional exact
// record counts preserve an asynchronous marker that can fall between UOPs of
// one macro instruction; instruction counts remain independently checked.
// The simulator releases the pause only after every active stream reaches the
// same global barrier, so producer lookahead cannot decode ROI UOPs before
// statistics are reset.
class WarmupInstructionTraceSource final : public TraceSource {
  public:
    WarmupInstructionTraceSource(
        std::unique_ptr<TraceSource> source,
        std::uint64_t warmup_instructions,
        std::uint64_t take_instructions,
        std::uint64_t warmup_records = 0,
        std::uint64_t take_records = 0,
        bool has_record_counts = false);
    bool next(TraceRecord& record) override;
    std::string description() const override;
    const SyscallMetadata* current_syscall_metadata() const override {
        return source_->current_syscall_metadata();
    }
    const VirtualPageMapping* virtual_page_mapping(
        std::uint32_t token) const override {
        return source_->virtual_page_mapping(token);
    }
    const std::map<std::uint32_t, VirtualPageMapping>*
    all_virtual_page_mappings() const override {
        return source_->all_virtual_page_mappings();
    }
    std::uint64_t current_address_space_id() const override {
        return source_->current_address_space_id();
    }
    std::uint64_t address_space_id_for_record(
        std::uint64_t ordinal) const override {
        return source_->address_space_id_for_record(ordinal);
    }
    const std::vector<AddressSpaceTransition>*
    address_space_transitions() const override {
        return source_->address_space_transitions();
    }
    const StaticInstructionInfo* static_instruction(
        std::uint64_t pc) const override {
        return source_->static_instruction(pc);
    }
    bool static_instruction_map_complete() const override {
        return source_->static_instruction_map_complete();
    }
    StaticInstructionIsa static_instruction_isa() const override {
        return source_->static_instruction_isa();
    }
    bool static_instruction_operands_complete() const override {
        return source_->static_instruction_operands_complete();
    }
    bool has_measurement_boundary() const override { return true; }
    bool measurement_boundary_pending() const override {
        return boundary_pending_;
    }
    void start_measurement() override;

  private:
    static bool completes_instruction(const TraceRecord& record);

    std::unique_ptr<TraceSource> source_;
    std::uint64_t warmup_instructions_ = 0;
    std::uint64_t take_instructions_ = 0;
    std::uint64_t warmup_records_ = 0;
    std::uint64_t take_records_ = 0;
    std::uint64_t warmup_emitted_ = 0;
    std::uint64_t measurement_emitted_ = 0;
    std::uint64_t warmup_records_emitted_ = 0;
    std::uint64_t measurement_records_emitted_ = 0;
    bool has_record_counts_ = false;
    bool boundary_pending_ = false;
    bool measuring_ = false;
};

class BinaryTraceWriter {
  public:
    BinaryTraceWriter(
        std::string path, std::uint32_t core_id,
        SyscallAbi syscall_abi = SyscallAbi::kUnknown);
    ~BinaryTraceWriter();
    BinaryTraceWriter(const BinaryTraceWriter&) = delete;
    BinaryTraceWriter& operator=(const BinaryTraceWriter&) = delete;

    void append(const TraceRecord& record);
    void append(const TraceRecord& record,
                const SyscallMetadata* syscall_metadata);
    void register_virtual_page_mapping(
        const VirtualPageMapping& mapping);
    // Set the address space for the next appended record. Repeated values are
    // run-length encoded in `<trace>.asmap`; zero preserves the legacy
    // single/unspecified-address-space contract and cannot be mixed with
    // explicit non-zero IDs.
    void set_address_space_id(std::uint64_t address_space_id);
    void register_static_instruction(
        const StaticInstructionInfo& instruction);
    void set_static_instruction_map_complete(bool complete = true) {
        static_instruction_map_complete_ = complete;
    }
    void set_static_instruction_isa(StaticInstructionIsa isa) {
        if (closed_) {
            throw std::logic_error("binary trace writer is closed");
        }
        static_instruction_isa_ = isa;
    }
    void close();
    std::uint64_t record_count() const { return record_count_; }

  private:
    void write_header();
    std::string path_;
    std::fstream output_;
    std::uint32_t core_id_ = 0;
    std::uint64_t record_count_ = 0;
    std::uint64_t feature_flags_ = 0;
    SyscallAbi syscall_abi_ = SyscallAbi::kUnknown;
    std::vector<SyscallMetadata> syscall_metadata_;
    std::map<std::uint32_t, VirtualPageMapping> virtual_page_mappings_;
    std::map<std::uint64_t, StaticInstructionInfo>
        static_instruction_map_;
    std::vector<AddressSpaceTransition> address_space_transitions_;
    std::uint64_t current_address_space_id_ = 0;
    bool static_instruction_map_complete_ = false;
    StaticInstructionIsa static_instruction_isa_ =
        StaticInstructionIsa::kUnknown;
    bool syscall_metadata_written_ = false;
    bool closed_ = false;
};

struct SyntheticTraceConfig {
    std::uint32_t core_id = 0;
    std::uint64_t instructions = 1'000'000;
    std::uint64_t working_set_lines = 1ull << 18;
    std::uint32_t memory_percent = 30;
    std::uint32_t write_percent = 20;
    std::uint32_t branch_percent = 15;
    std::uint32_t taken_percent = 60;
    std::uint32_t shared_percent = 5;
    std::uint64_t seed = 1;
};

class SyntheticTraceSource final : public TraceSource {
  public:
    explicit SyntheticTraceSource(SyntheticTraceConfig config);
    bool next(TraceRecord& record) override;
    std::string description() const override;

  private:
    std::uint64_t random();
    SyntheticTraceConfig config_;
    std::uint64_t cursor_ = 0;
    std::uint64_t state_ = 0;
};

struct TraceManifestEntry {
    std::uint32_t core_id = 0;
    std::string format;
    std::string path;
    std::uint32_t source_core_id = 0;
    bool has_source_core_id = false;
    std::uint64_t skip_instructions = 0;
    std::uint64_t warmup_instructions = 0;
    std::uint64_t take_instructions = 0;
    std::uint64_t warmup_records = 0;
    std::uint64_t take_records = 0;
    bool has_instruction_slice = false;
    bool has_measurement_warmup = false;
    bool has_record_counts = false;
};

std::vector<TraceManifestEntry> read_trace_manifest(
    const std::string& manifest_path);
std::vector<std::unique_ptr<TraceSource>> open_trace_manifest(
    const std::string& manifest_path, std::uint32_t maximum_cores);

void convert_gem5_jsonl_to_binary(const std::string& input_path,
                                  const std::string& output_path,
                                  std::uint32_t core_id,
                                  const std::string& syscall_output_path = "",
                                  SyscallAbi syscall_abi =
                                      SyscallAbi::kLinuxX86_64);

// Rewrite any supported legacy/current binary stream as canonical FST v7.
// Legacy syscall markers retain their inline syscall number and receive one
// sparse metadata row whose optional-field validity mask is empty.
void upgrade_binary_trace_to_v7(
    const std::string& input_path, const std::string& output_path,
    SyscallAbi syscall_abi = SyscallAbi::kLinuxX86_64);

SyscallAbi parse_syscall_abi(const std::string& name);
std::string syscall_abi_name(SyscallAbi abi);

}  // namespace fastsim
