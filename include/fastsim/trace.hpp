#pragma once

#include <cstdint>
#include <fstream>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "fastsim/types.hpp"
#include "fastsim/fst_dependencies.hpp"

namespace fastsim {

class TraceSource {
  public:
    virtual ~TraceSource() = default;
    virtual bool next(TraceRecord& record) = 0;
    virtual std::string description() const = 0;
    // Completeness is declared for the entire stream, independently of n_src
    // (several source registers can have the same producing UOP).
    virtual bool complete_dependencies() const { return false; }
    virtual bool has_dependency_extensions() const { return false; }
    // Distances after the four inline entries, valid until next(). Slicing
    // preserves distances; it never silently drops pre-slice producers.
    virtual const std::vector<std::uint32_t>& current_dependency_extensions() const {
        static const std::vector<std::uint32_t> empty;
        return empty;
    }
    // Canonical FST v7 declares privilege-domain support in its header even
    // when a short per-core window happens to contain no CPL0 record.  Other
    // source formats return nullopt because they have no static declaration.
    virtual std::optional<bool> privilege_records_capability() const {
        return std::nullopt;
    }
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
    // Resolve an instruction virtual address in the address-space/mapping
    // state of the record most recently returned by next().
    virtual const InstructionPageMapping* instruction_page_mapping(
        std::uint64_t) const {
        return nullptr;
    }
    virtual const std::vector<InstructionPageMapping>*
    all_instruction_page_mappings() const {
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
    // Explicit context bounds are immutable and measured after warmup.
    // The marker is producer-owned: snapshot it between next() calls rather
    // than reading it from a concurrent consumer. It becomes sticky only
    // after the final score record has been returned; it never inserts EOF.
    virtual bool has_execution_context() const { return false; }
    virtual bool score_boundary_reached() const { return false; }
    virtual std::uint64_t score_records() const { return 0; }
    virtual std::uint64_t execution_records() const { return 0; }
    // Present only on explicitly state-seeded two-phase sources. An empty
    // vector means the producer certified that this core had no omitted
    // committed data accesses at the boundary; nullptr means no sidecar was
    // supplied.
    virtual const std::vector<MeasurementBoundaryMemoryAccess>*
    measurement_boundary_memory_accesses() const {
        return nullptr;
    }
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
    bool complete_dependencies() const override { return complete_dependencies_; }
    bool has_dependency_extensions() const override { return dependency_rows_total_ != 0; }
    const std::vector<std::uint32_t>& current_dependency_extensions() const override {
        return current_dependency_extensions_;
    }
    std::optional<bool> privilege_records_capability() const override {
        return has_privilege_records_;
    }
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
    const InstructionPageMapping* instruction_page_mapping(
        std::uint64_t virtual_address) const override;
    const std::vector<InstructionPageMapping>*
    all_instruction_page_mappings() const override {
        return instruction_page_mappings_.empty()
                   ? nullptr
                   : &instruction_page_mappings_;
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
    void read_dependency_row();
    bool complete_dependencies_ = false;
    std::ifstream dependency_input_;
    std::uint64_t dependency_rows_total_ = 0, dependency_rows_read_ = 0;
    std::uint64_t dependency_distances_left_ = 0;
    fst::DependencyRow next_dependency_row_{};
    std::vector<std::uint32_t> next_dependency_extensions_, current_dependency_extensions_;
    std::string path_;
    std::ifstream input_;
    std::uint32_t core_id_ = 0;
    std::uint64_t record_count_ = 0;
    std::uint64_t records_read_ = 0;
    std::uint32_t trace_version_ = 0;
    bool legacy_v2_ = false;
    bool has_syscall_metadata_ = false;
    bool has_privilege_records_ = false;
    bool saw_kernel_record_ = false;
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
    std::vector<InstructionPageMapping> instruction_page_mappings_;
    std::size_t instruction_page_mapping_cursor_ = 0;
    std::map<std::pair<std::uint64_t, std::uint64_t>,
             const InstructionPageMapping*>
        active_instruction_page_mappings_;
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
    bool complete_dependencies() const override { return source_->complete_dependencies(); }
    bool has_dependency_extensions() const override { return source_->has_dependency_extensions(); }
    const std::vector<std::uint32_t>& current_dependency_extensions() const override {
        return source_->current_dependency_extensions();
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
    const InstructionPageMapping* instruction_page_mapping(
        std::uint64_t virtual_address) const override {
        return source_->instruction_page_mapping(virtual_address);
    }
    const std::vector<InstructionPageMapping>*
    all_instruction_page_mappings() const override {
        return source_->all_instruction_page_mappings();
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
    std::optional<bool> privilege_records_capability() const override {
        return source_->privilege_records_capability();
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
// The legacy simulator releases all streams at a global drained barrier.
// The causal event driver instead records each functional boundary, resumes
// bounded decoding, and preserves in-flight state across the statistics cut.
// Explicit execution_records continues decoding past the score marker without
// pausing, through an exact post-warmup execution boundary.
class WarmupInstructionTraceSource final : public TraceSource {
  public:
    WarmupInstructionTraceSource(
        std::unique_ptr<TraceSource> source,
        std::uint64_t warmup_instructions,
        std::uint64_t take_instructions,
        std::uint64_t warmup_records = 0,
        std::uint64_t take_records = 0,
        bool has_record_counts = false,
        std::optional<std::vector<MeasurementBoundaryMemoryAccess>>
            measurement_boundary_memory_accesses = std::nullopt,
        std::optional<std::uint64_t> execution_records = std::nullopt);
    bool next(TraceRecord& record) override;
    std::string description() const override;
    const SyscallMetadata* current_syscall_metadata() const override {
        return source_->current_syscall_metadata();
    }
    bool complete_dependencies() const override { return source_->complete_dependencies(); }
    bool has_dependency_extensions() const override { return source_->has_dependency_extensions(); }
    const std::vector<std::uint32_t>& current_dependency_extensions() const override {
        return source_->current_dependency_extensions();
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
    const InstructionPageMapping* instruction_page_mapping(
        std::uint64_t virtual_address) const override {
        return source_->instruction_page_mapping(virtual_address);
    }
    const std::vector<InstructionPageMapping>*
    all_instruction_page_mappings() const override {
        return source_->all_instruction_page_mappings();
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
    std::optional<bool> privilege_records_capability() const override {
        return source_->privilege_records_capability();
    }
    bool has_measurement_boundary() const override { return true; }
    bool measurement_boundary_pending() const override {
        return boundary_pending_;
    }
    bool has_execution_context() const override {
        return execution_records_.has_value();
    }
    bool score_boundary_reached() const override {
        return score_boundary_reached_;
    }
    std::uint64_t score_records() const override {
        return has_execution_context() ? take_records_ : 0;
    }
    std::uint64_t execution_records() const override {
        return execution_records_.value_or(0);
    }
    const std::vector<MeasurementBoundaryMemoryAccess>*
    measurement_boundary_memory_accesses() const override {
        return measurement_boundary_memory_accesses_.has_value()
                   ? &*measurement_boundary_memory_accesses_
                   : nullptr;
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
    std::optional<std::vector<MeasurementBoundaryMemoryAccess>>
        measurement_boundary_memory_accesses_;
    std::optional<std::uint64_t> execution_records_;
    bool score_boundary_reached_ = false;
    bool incomplete_macro_ = false;
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
    // Must be enabled before appending any record. Every inline edge then
    // claims completeness unless accompanied by the supplied extension.
    void enable_complete_dependencies();
    void append(const TraceRecord& record, const SyscallMetadata* syscall_metadata,
                const std::vector<std::uint32_t>& dependency_extensions);
    void register_virtual_page_mapping(
        const VirtualPageMapping& mapping);
    // Set the address space for the next appended record. Repeated values are
    // run-length encoded in `<trace>.asmap`; zero preserves the legacy
    // single/unspecified-address-space contract and cannot be mixed with
    // explicit non-zero IDs.
    void set_address_space_id(std::uint64_t address_space_id);
    void register_instruction_page_mapping(
        const InstructionPageMapping& mapping);
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
    std::fstream dependency_output_;
    std::uint64_t dependency_rows_ = 0, dependency_distances_ = 0;
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
    std::vector<InstructionPageMapping> instruction_page_mappings_;
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
    std::string measurement_boundary_memory_state_path;
    bool has_measurement_boundary_memory_state = false;
    // Exact number of records after warmup, including score and context.
    // Absent on all legacy formats, even when execution stops at score EOF.
    std::optional<std::uint64_t> execution_records;
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
