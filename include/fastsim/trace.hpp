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
#include "fastsim/functional_sync.hpp"
#include "fastsim/fst_dependencies.hpp"
#include "fastsim/fst_memory_requests.hpp"
#include "fastsim/fst_store_facts_io.hpp"
#include "fastsim/fst_static_uop_reader.hpp"
#include "fastsim/fst_core_functional_reader.hpp"
#include "fastsim/fst_mmu_control_reader.hpp"
#include "fastsim/fst_mmu_control_v2_reader.hpp"
#include "fastsim/fst_retirement.hpp"
#include "fastsim/fst_translation_facts.hpp"
#include "fastsim/fst_dynamic_translations.hpp"
#include "fastsim/retired_frontend_facts.hpp"
#include "fastsim/fst_instruction_translations.hpp"

namespace fastsim {

// FST v7 capability: syscall transition work is retained as real micro-ops;
// sparse ABI rows annotate their first retained entry micro-op.
inline constexpr std::uint64_t kFstRawSyscallMicroops = 1ull << 8;

class TraceSource {
  public:
    virtual ~TraceSource() = default;
    virtual const fst::RetiredFrontendMacro* current_frontend_macro() const { return nullptr; }
    virtual std::shared_ptr<const fst::InstructionFetchFacts> current_instruction_fetch() const { return {}; }
    virtual bool instruction_translations_declared() const { return false; }
    virtual unsigned instruction_translation_version() const { return 0; }
    virtual std::optional<std::uint64_t> current_frontend_record_ordinal() const { return std::nullopt; }
    virtual bool dynamic_translations() const { return false; }
    virtual unsigned dynamic_translation_version() const { return 0; }
    virtual std::optional<std::uint64_t> current_translation_record_ordinal() const { return std::nullopt; }
    virtual std::optional<std::uint64_t> current_translation_retirement_asid() const { return std::nullopt; }
    virtual const std::vector<fst::TranslationFragment>& current_translation_fragments() const {
        static const std::vector<fst::TranslationFragment> empty;
        return empty;
    }
    virtual bool next(TraceRecord& record) = 0;
    // Raw FST ordinal, unaffected by wrapper slicing or model-generated UOPs.
    virtual std::optional<std::uint64_t> current_source_record_ordinal() const { return std::nullopt; }
    virtual std::optional<FunctionalSyncLayout> functional_sync_layout() const { return std::nullopt; }
    virtual std::optional<fst::MemoryRequestRole> current_memory_request_role() const { return std::nullopt; }
    virtual bool memory_request_roles_complete() const { return false; }
    virtual const fst::StaticUopFactsRow* current_static_uop_facts() const { return nullptr; }
    virtual const std::vector<fst::StaticUopOperand>& current_static_uop_operands() const { static const std::vector<fst::StaticUopOperand> empty; return empty; }
    virtual std::string static_uop_policy_identity() const { return {}; }
    // Functional descriptor semantics, independent of producer source revision.
    // Version 1 defines the StaticUopFactsRow/Operand field meanings.
    virtual unsigned static_uop_semantics_version() const { return 0; }
    virtual const fst::CoreFunctionalRow* current_core_functional_facts() const { return nullptr; }
    virtual const std::vector<fst::CoreFunctionalOperand>& current_core_functional_operands() const { static const std::vector<fst::CoreFunctionalOperand> empty; return empty; }
    virtual const fst::CoreFunctionalHeader* core_functional_header() const { return nullptr; }
    virtual const fst::CoreFunctionalCoverage* core_functional_coverage() const { return nullptr; }
    virtual unsigned core_functional_version() const { return 0; }
    virtual std::string core_functional_policy_identity() const { return {}; }
    virtual const fst::mmuctl::Group* current_mmu_controls() const { return nullptr; }
    virtual const fst::mmuctl::Baseline* mmu_control_baseline() const { return nullptr; }
    virtual const fst::mmuctl::Footer* mmu_control_coverage() const { return nullptr; }
    virtual unsigned mmu_control_version() const { return 0; }
    virtual std::string mmu_control_policy_identity() const { return {}; }
    virtual bool mmu_controls_ready() const { return false; }
    virtual bool mmu_controls_require_prefix() const { return false; }
    virtual const fst::mmuctl_v2::Group* current_mmu_controls_v2() const { return nullptr; }
    virtual const fst::mmuctl_v2::Baseline* mmu_control_v2_baseline() const { return nullptr; }
    virtual const fst::mmuctl_v2::Footer* mmu_control_v2_coverage() const { return nullptr; }
    virtual const fst::archb::EntryMember* current_architectural_boundary_member() const { return nullptr; }
    virtual const fst::archb::Epoch* architectural_boundary_epoch() const { return nullptr; }
    virtual const fst::archb::Footer* architectural_boundary_coverage() const { return nullptr; }
    virtual const std::vector<fst::ArchitecturalBoundaryEvent>& boundaries_before(std::uint64_t) const { static const std::vector<fst::ArchitecturalBoundaryEvent> empty;return empty; }
    virtual const std::vector<fst::ArchitecturalBoundaryEvent>& initial_architectural_boundaries() const {static const std::vector<fst::ArchitecturalBoundaryEvent> empty;return empty;}
    virtual bool architectural_boundary_bundle_ready() const { return false; }

    virtual bool store_facts_complete() const { return false; }
    virtual const fst::StoreRequestFacts* current_store_facts() const { return nullptr; }
    virtual const fst::StoreRequestFacts* current_memory_facts() const { return nullptr; }
    // Complete functional cacheability/order/operation certificate; unknown is not ordinary.
    virtual std::optional<bool> current_ordinary_load_eligible() const { return std::nullopt; }
    virtual bool contains_software_prefetch() const { return false; }
    virtual bool raw_syscall_microops() const { return false; }
    virtual bool functional_retirement_complete() const { return false; }
    virtual bool current_hardware_entry() const { return false; }
    virtual bool current_precise_fault_abandonment() const { return false; }
    virtual std::uint32_t current_memory_request_micro_pc() const { return 0; }
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
    // Current address-space view; no cross-ASID fallback is permitted.
    virtual const std::map<std::uint64_t, StaticInstructionInfo>*
    all_static_instructions() const { return nullptr; }
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
    virtual bool has_score_range() const { return false; }
    virtual bool score_begin_reached() const { return true; }
    virtual std::uint64_t score_begin_record() const { return 0; }
    virtual std::uint64_t score_end_record() const { return score_records(); }
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
    const fst::RetiredFrontendMacro* current_frontend_macro() const override { return current_frontend_macro_; }
    std::shared_ptr<const fst::InstructionFetchFacts> current_instruction_fetch() const override { return current_instruction_fetch_; }
    bool instruction_translations_declared() const override { return bool(instruction_translations_); }
    unsigned instruction_translation_version() const override { return instruction_translations_?instruction_translations_->version():0; }
    std::optional<std::uint64_t> current_frontend_record_ordinal() const override {
        return current_frontend_macro_ && records_read_ ? std::optional<std::uint64_t>(records_read_-1) : std::nullopt;
    }
    bool dynamic_translations() const override { return bool(dynamic_translations_); }
    unsigned dynamic_translation_version() const override { return dynamic_translations_?dynamic_translations_->version():0; }
    std::optional<std::uint64_t> current_translation_record_ordinal() const override {
        if (preloaded_) return cached_memory() ? cached_memory()->translation_ordinal : std::nullopt;
        return dynamic_translations_&&!translation_fragments_.empty()?dynamic_translations_->source_ordinal():std::nullopt;
    }
    std::optional<std::uint64_t> current_translation_retirement_asid() const override {
        if (preloaded_) return cached_memory() ? cached_memory()->translation_asid : std::nullopt;
        return dynamic_translations_&&!translation_fragments_.empty()?dynamic_translations_->retirement_asid():std::nullopt;
    }
    const std::vector<fst::TranslationFragment>& current_translation_fragments() const override {
        if (preloaded_) return cached_memory() ? cached_memory()->translations : TraceSource::current_translation_fragments();
        return translation_fragments_;
    }
    std::optional<fst::MemoryRequestRole> current_memory_request_role() const override {
        return current_memory_request_role_;
    }
    bool memory_request_roles_complete() const override { return memory_request_roles_complete_; }
    const fst::StaticUopFactsRow* current_static_uop_facts() const override {
        if (preloaded_) return cached_current_ && cached_current_->uop_index != kNoCacheIndex ? &cached_uops_[cached_current_->uop_index] : nullptr;
        return static_uop_reader_?static_uop_reader_->current():nullptr;
    }
    const std::vector<fst::StaticUopOperand>& current_static_uop_operands() const override {
        if (preloaded_) {
            if (!cached_current_ || !cached_current_->operand_count) return TraceSource::current_static_uop_operands();
            if (cached_operand_ordinal_ != records_read_) {
                const auto begin = cached_operand_data_.begin() + static_cast<std::ptrdiff_t>(cached_current_->operand_begin);
                cached_operand_view_.assign(begin, begin + cached_current_->operand_count);
                cached_operand_ordinal_ = records_read_;
            }
            return cached_operand_view_;
        }
        return static_uop_reader_?static_uop_reader_->operands():TraceSource::current_static_uop_operands();
    }
    std::string static_uop_policy_identity() const override { return static_uop_reader_?static_uop_reader_->policy_identity():std::string{}; }
    unsigned static_uop_semantics_version() const override { return static_uop_reader_?1:0; }
    const fst::CoreFunctionalRow* current_core_functional_facts() const override { return core_functional_reader_?core_functional_reader_->current():nullptr; }
    const std::vector<fst::CoreFunctionalOperand>& current_core_functional_operands() const override { return core_functional_reader_?core_functional_reader_->operands():TraceSource::current_core_functional_operands(); }
    const fst::CoreFunctionalHeader* core_functional_header() const override { return core_functional_reader_?&core_functional_reader_->header():nullptr; }
    const fst::CoreFunctionalCoverage* core_functional_coverage() const override { return core_functional_reader_?&core_functional_reader_->coverage():nullptr; }
    unsigned core_functional_version() const override { return core_functional_reader_?core_functional_reader_->header().version:0; }
    std::string core_functional_policy_identity() const override { return core_functional_reader_?core_functional_reader_->policy_identity():std::string{}; }
    const fst::mmuctl::Group* current_mmu_controls() const override { return mmu_control_reader_?mmu_control_reader_->current():nullptr; }
    const fst::mmuctl::Baseline* mmu_control_baseline() const override { return mmu_control_reader_?&mmu_control_reader_->baseline():nullptr; }
    const fst::mmuctl::Footer* mmu_control_coverage() const override { return mmu_control_reader_?&mmu_control_reader_->coverage():nullptr; }
    unsigned mmu_control_version() const override { return mmu_control_v2_reader_?2:mmu_control_reader_?1:0; }
    std::string mmu_control_policy_identity() const override { return mmu_control_v2_reader_?fst::mmuctl::hex(mmu_control_v2_reader_->header().policy):mmu_control_reader_?mmu_control_reader_->policy_identity():std::string{}; }
    bool mmu_controls_ready() const override { return mmu_control_reader_&&mmu_control_reader_->ready(); }
    const fst::mmuctl_v2::Group* current_mmu_controls_v2() const override { return mmu_control_v2_reader_?mmu_control_v2_reader_->current():nullptr; }
    const fst::mmuctl_v2::Baseline* mmu_control_v2_baseline() const override { return mmu_control_v2_reader_?&mmu_control_v2_reader_->baseline():nullptr; }
    const fst::mmuctl_v2::Footer* mmu_control_v2_coverage() const override { return mmu_control_v2_reader_?&mmu_control_v2_reader_->coverage():nullptr; }
    const fst::archb::EntryMember* current_architectural_boundary_member() const override { return architectural_boundary_reader_?architectural_boundary_reader_->current():nullptr; }
    const fst::archb::Epoch* architectural_boundary_epoch() const override { return architectural_boundary_reader_?&architectural_boundary_reader_->epoch():nullptr; }
    const fst::archb::Footer* architectural_boundary_coverage() const override { return architectural_boundary_reader_?&architectural_boundary_reader_->coverage():nullptr; }
    const std::vector<fst::ArchitecturalBoundaryEvent>& boundaries_before(std::uint64_t ordinal) const override { return mmu_control_v2_reader_?mmu_control_v2_reader_->boundaries_before(ordinal):TraceSource::boundaries_before(ordinal); }
    const std::vector<fst::ArchitecturalBoundaryEvent>& initial_architectural_boundaries() const override {return mmu_control_v2_reader_?mmu_control_v2_reader_->initial_boundaries():TraceSource::initial_architectural_boundaries();}
    bool architectural_boundary_bundle_ready() const override { return architectural_boundary_reader_&&architectural_boundary_reader_->ready()&&mmu_control_v2_reader_&&mmu_control_v2_reader_->ready(); }


    bool store_facts_complete() const override { return store_facts_reader_ != nullptr; }
    const fst::StoreRequestFacts* current_memory_facts() const override {
        if (preloaded_) return cached_memory() && cached_memory()->memory ? &*cached_memory()->memory : nullptr;
        return store_facts_reader_?store_facts_reader_->current():nullptr;
    }
    const fst::StoreRequestFacts* current_store_facts() const override {
        const auto* row=current_memory_facts();
        return row && !(row->attributes&fst::kMemoryRead)?row:nullptr;
    }
    std::optional<bool> current_ordinary_load_eligible() const override;
    bool contains_software_prefetch() const override { return software_prefetch_present_; }
    bool raw_syscall_microops() const override { return raw_syscall_microops_; }
    bool functional_retirement_complete() const override { return functional_retirement_complete_; }
    bool current_hardware_entry() const override { return current_hardware_entry_; }
    bool current_precise_fault_abandonment() const override { return current_precise_fault_abandonment_; }
    std::uint32_t current_memory_request_micro_pc() const override { return current_memory_request_micro_pc_; }

    explicit BinaryTraceSource(std::string path);
    // Evaluation-only, opt-in full functional decode. The original readers
    // perform every validation before the immutable cache is exposed.
    void preload_validated();
    bool next(TraceRecord& record) override;
    std::optional<std::uint64_t> current_source_record_ordinal() const override {
        return records_read_ == 0 ? std::nullopt : std::optional<std::uint64_t>(records_read_-1);
    }
    std::optional<FunctionalSyncLayout> functional_sync_layout() const override {
        return FunctionalSyncLayout{record_count_,0,record_count_,core_id_};
    }
    std::string description() const override;
    bool complete_dependencies() const override { return complete_dependencies_; }
    bool has_dependency_extensions() const override { return dependency_rows_total_ != 0; }
    const std::vector<std::uint32_t>& current_dependency_extensions() const override {
        if (preloaded_) {
            if (!cached_current_ || !cached_current_->dependency_count) return TraceSource::current_dependency_extensions();
            if (cached_dependency_ordinal_ != records_read_) {
                const auto begin = cached_dependency_data_.begin() + static_cast<std::ptrdiff_t>(cached_current_->dependency_begin);
                cached_dependency_view_.assign(begin, begin + cached_current_->dependency_count);
                cached_dependency_ordinal_ = records_read_;
            }
            return cached_dependency_view_;
        }
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
    const std::map<std::uint64_t, StaticInstructionInfo>*
    all_static_instructions() const override;
    bool asid_static_instruction_map() const {
        return asid_static_instruction_map_;
    }
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
    static constexpr std::uint64_t kNoCacheIndex = UINT64_MAX;
    struct CachedMemory {
        std::optional<fst::StoreRequestFacts> memory;
        std::vector<fst::TranslationFragment> translations;
        std::optional<std::uint64_t> translation_ordinal, translation_asid;
    };
    struct CachedRecord {
        TraceRecord record;
        std::uint64_t frontend_index = kNoCacheIndex;
        std::uint64_t uop_index = kNoCacheIndex, memory_index = kNoCacheIndex;
        std::uint64_t operand_begin = 0, dependency_begin = 0;
        std::uint32_t operand_count = 0, dependency_count = 0;
        std::shared_ptr<const fst::InstructionFetchFacts> fetch;
        const SyscallMetadata* syscall = nullptr;
        std::uint64_t asid = 0;
        std::optional<fst::MemoryRequestRole> role;
        std::uint32_t micro_pc = 0;
        bool hardware_entry = false, precise_fault = false;
    };
    std::vector<CachedRecord> cached_records_;
    std::vector<fst::RetiredFrontendMacro> cached_frontend_;
    std::vector<fst::StaticUopFactsRow> cached_uops_;
    std::vector<CachedMemory> cached_memory_;
    std::vector<fst::StaticUopOperand> cached_operand_data_;
    std::vector<std::uint32_t> cached_dependency_data_;
    mutable std::vector<fst::StaticUopOperand> cached_operand_view_;
    mutable std::vector<std::uint32_t> cached_dependency_view_;
    mutable std::uint64_t cached_operand_ordinal_ = kNoCacheIndex;
    mutable std::uint64_t cached_dependency_ordinal_ = kNoCacheIndex;
    const CachedRecord* cached_current_ = nullptr;
    const CachedMemory* cached_memory() const {
        return cached_current_ && cached_current_->memory_index != kNoCacheIndex
            ? &cached_memory_[cached_current_->memory_index] : nullptr;
    }
    bool preloaded_ = false;
    std::unique_ptr<fst::RetiredFrontendFacts> frontend_facts_;
    std::unique_ptr<fst::InstructionTranslations> instruction_translations_;
    std::shared_ptr<const fst::InstructionFetchFacts> current_instruction_fetch_;
    std::unique_ptr<fst::StoreFactsReader> store_facts_reader_;
    std::unique_ptr<fst::StaticUopReader> static_uop_reader_;
    std::unique_ptr<fst::CoreFunctionalReader> core_functional_reader_;
    std::unique_ptr<fst::MmuControlReader> mmu_control_reader_;
    std::unique_ptr<fst::MmuControlV2Reader> mmu_control_v2_reader_;
    std::unique_ptr<fst::ArchitecturalBoundaryReader> architectural_boundary_reader_;
    const fst::RetiredFrontendMacro* current_frontend_macro_ = nullptr;
    std::unique_ptr<fst::DynamicTranslations> dynamic_translations_;
    std::vector<fst::TranslationFragment> translation_fragments_;
    bool functional_retirement_complete_=false,current_hardware_entry_=false,current_precise_fault_abandonment_=false;
    std::vector<fst::RetirementRow> retirement_rows_;
    std::size_t retirement_cursor_=0;
    std::uint64_t expected_retirement_hash_=0;
    std::vector<fst::MemoryRequestRow> memory_request_rows_;
    std::size_t memory_request_cursor_ = 0;
    bool memory_request_roles_complete_ = false;
    bool software_prefetch_present_ = false;
    std::uint64_t memory_request_stream_hash_ = fst::kRequestStreamHashSeed;
    std::uint64_t expected_memory_request_hash_ = 0;
    std::optional<fst::MemoryRequestRole> current_memory_request_role_;
    std::uint32_t current_memory_request_micro_pc_ = 0;
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
    bool spr_op_classes_ = false;
    bool raw_syscall_microops_ = false;
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
    std::map<std::uint64_t, std::map<std::uint64_t, StaticInstructionInfo>>
        asid_static_instruction_maps_;
    bool asid_static_instruction_map_ = false;
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
class InstructionSliceTraceSource : public TraceSource {
  public:
    const fst::RetiredFrontendMacro* current_frontend_macro() const override { return source_->current_frontend_macro(); }
    std::shared_ptr<const fst::InstructionFetchFacts> current_instruction_fetch() const override { return source_->current_instruction_fetch(); }
    bool instruction_translations_declared() const override { return source_->instruction_translations_declared(); }
    unsigned instruction_translation_version() const override { return source_->instruction_translation_version(); }
    std::optional<std::uint64_t> current_frontend_record_ordinal() const override { return source_->current_frontend_record_ordinal(); }
    bool dynamic_translations() const override { return source_->dynamic_translations(); }
    unsigned dynamic_translation_version() const override { return source_->dynamic_translation_version(); }
    std::optional<std::uint64_t> current_translation_record_ordinal() const override { return source_->current_translation_record_ordinal(); }
    std::optional<std::uint64_t> current_translation_retirement_asid() const override { return source_->current_translation_retirement_asid(); }
    const std::vector<fst::TranslationFragment>& current_translation_fragments() const override { return source_->current_translation_fragments(); }
    std::optional<fst::MemoryRequestRole> current_memory_request_role() const override {
        return source_->current_memory_request_role();
    }
    bool memory_request_roles_complete() const override { return source_->memory_request_roles_complete(); }
    const fst::StaticUopFactsRow* current_static_uop_facts() const override { return source_->current_static_uop_facts(); }
    const std::vector<fst::StaticUopOperand>& current_static_uop_operands() const override { return source_->current_static_uop_operands(); }
    std::string static_uop_policy_identity() const override { return source_->static_uop_policy_identity(); }
    unsigned static_uop_semantics_version() const override { return source_->static_uop_semantics_version(); }
    const fst::CoreFunctionalRow* current_core_functional_facts() const override { return core_functional_current_visible_?source_->current_core_functional_facts():nullptr; }
    const std::vector<fst::CoreFunctionalOperand>& current_core_functional_operands() const override { return core_functional_current_visible_?source_->current_core_functional_operands():TraceSource::current_core_functional_operands(); }
    const fst::CoreFunctionalHeader* core_functional_header() const override { return source_->core_functional_header(); }
    const fst::CoreFunctionalCoverage* core_functional_coverage() const override { return source_->core_functional_coverage(); }
    unsigned core_functional_version() const override { return source_->core_functional_version(); }
    std::string core_functional_policy_identity() const override { return source_->core_functional_policy_identity(); }
    const fst::mmuctl::Group* current_mmu_controls() const override { return core_functional_current_visible_?source_->current_mmu_controls():nullptr; }
    const fst::mmuctl::Baseline* mmu_control_baseline() const override { return source_->mmu_control_baseline(); }
    const fst::mmuctl::Footer* mmu_control_coverage() const override { return source_->mmu_control_coverage(); }
    unsigned mmu_control_version() const override { return source_->mmu_control_version(); }
    std::string mmu_control_policy_identity() const override { return source_->mmu_control_policy_identity(); }
    bool mmu_controls_ready() const override { return false; }
    bool mmu_controls_require_prefix() const override { return source_->mmu_control_version()!=0; }
    const fst::mmuctl_v2::Group* current_mmu_controls_v2() const override { return core_functional_current_visible_?source_->current_mmu_controls_v2():nullptr; }
    const fst::mmuctl_v2::Baseline* mmu_control_v2_baseline() const override { return source_->mmu_control_v2_baseline(); }
    const fst::mmuctl_v2::Footer* mmu_control_v2_coverage() const override { return source_->mmu_control_v2_coverage(); }
    const fst::archb::EntryMember* current_architectural_boundary_member() const override { return core_functional_current_visible_?source_->current_architectural_boundary_member():nullptr; }
    const fst::archb::Epoch* architectural_boundary_epoch() const override { return source_->architectural_boundary_epoch(); }
    const fst::archb::Footer* architectural_boundary_coverage() const override { return source_->architectural_boundary_coverage(); }
    const std::vector<fst::ArchitecturalBoundaryEvent>& boundaries_before(std::uint64_t ordinal) const override { return core_functional_current_visible_?source_->boundaries_before(ordinal):TraceSource::boundaries_before(ordinal); }
    bool architectural_boundary_bundle_ready() const override { return false; }


    bool store_facts_complete() const override { return source_->store_facts_complete(); }
    const fst::StoreRequestFacts* current_store_facts() const override { return source_->current_store_facts(); }
    const fst::StoreRequestFacts* current_memory_facts() const override { return source_->current_memory_facts(); }
    std::optional<bool> current_ordinary_load_eligible() const override { return source_->current_ordinary_load_eligible(); }
    bool contains_software_prefetch() const override { return source_->contains_software_prefetch(); }
    bool raw_syscall_microops() const override { return source_->raw_syscall_microops(); }
    bool functional_retirement_complete() const override { return source_->functional_retirement_complete(); }
    bool current_hardware_entry() const override { return source_->current_hardware_entry(); }
    bool current_precise_fault_abandonment() const override { return source_->current_precise_fault_abandonment(); }
    std::uint32_t current_memory_request_micro_pc() const override { return source_->current_memory_request_micro_pc(); }

    InstructionSliceTraceSource(
        std::unique_ptr<TraceSource> source,
        std::uint64_t skip_instructions,
        std::uint64_t take_instructions);
    bool next(TraceRecord& record) override;
    std::optional<std::uint64_t> current_source_record_ordinal() const override {
        return source_->current_source_record_ordinal();
    }
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
    const std::map<std::uint64_t, StaticInstructionInfo>*
    all_static_instructions() const override {
        return source_->all_static_instructions();
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

  protected:
    std::unique_ptr<TraceSource> source_;
    bool core_functional_current_visible_=false;
  private:
    std::uint64_t skip_instructions_ = 0;
    std::uint64_t take_instructions_ = 0;
    std::uint64_t skipped_instructions_ = 0;
    std::uint64_t emitted_instructions_ = 0;
    bool prefix_skipped_ = false;
};

// Continuous physical stream with independent (BEGIN, END] score anchors.
// Shares the slice wrapper's metadata forwarding, but never skips or pauses.
class ScoreRangeTraceSource final : public InstructionSliceTraceSource {
  public:
    ScoreRangeTraceSource(std::unique_ptr<TraceSource> source,
                         std::uint64_t begin, std::uint64_t end,
                         std::uint64_t execution);
    bool next(TraceRecord& record) override;
    bool has_execution_context() const override { return true; }
    bool has_score_range() const override { return true; }
    bool score_begin_reached() const override { return emitted_ >= begin_; }
    bool score_boundary_reached() const override { return emitted_ >= end_; }
    std::uint64_t score_begin_record() const override { return begin_; }
    std::uint64_t score_end_record() const override { return end_; }
    std::uint64_t score_records() const override { return end_ - begin_; }
    std::uint64_t execution_records() const override { return execution_; }
    std::string description() const override;
  private:
    std::uint64_t begin_, end_, execution_, emitted_ = 0;
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
    const fst::RetiredFrontendMacro* current_frontend_macro() const override { return source_->current_frontend_macro(); }
    std::shared_ptr<const fst::InstructionFetchFacts> current_instruction_fetch() const override { return source_->current_instruction_fetch(); }
    bool instruction_translations_declared() const override { return source_->instruction_translations_declared(); }
    unsigned instruction_translation_version() const override { return source_->instruction_translation_version(); }
    std::optional<std::uint64_t> current_frontend_record_ordinal() const override { return source_->current_frontend_record_ordinal(); }
    bool dynamic_translations() const override { return source_->dynamic_translations(); }
    unsigned dynamic_translation_version() const override { return source_->dynamic_translation_version(); }
    std::optional<std::uint64_t> current_translation_record_ordinal() const override { return source_->current_translation_record_ordinal(); }
    std::optional<std::uint64_t> current_translation_retirement_asid() const override { return source_->current_translation_retirement_asid(); }
    const std::vector<fst::TranslationFragment>& current_translation_fragments() const override { return source_->current_translation_fragments(); }
    std::optional<fst::MemoryRequestRole> current_memory_request_role() const override {
        return source_->current_memory_request_role();
    }
    bool memory_request_roles_complete() const override { return source_->memory_request_roles_complete(); }
    const fst::StaticUopFactsRow* current_static_uop_facts() const override { return source_->current_static_uop_facts(); }
    const std::vector<fst::StaticUopOperand>& current_static_uop_operands() const override { return source_->current_static_uop_operands(); }
    std::string static_uop_policy_identity() const override { return source_->static_uop_policy_identity(); }
    unsigned static_uop_semantics_version() const override { return source_->static_uop_semantics_version(); }
    const fst::CoreFunctionalRow* current_core_functional_facts() const override { return core_functional_current_visible_?source_->current_core_functional_facts():nullptr; }
    const std::vector<fst::CoreFunctionalOperand>& current_core_functional_operands() const override { return core_functional_current_visible_?source_->current_core_functional_operands():TraceSource::current_core_functional_operands(); }
    const fst::CoreFunctionalHeader* core_functional_header() const override { return source_->core_functional_header(); }
    const fst::CoreFunctionalCoverage* core_functional_coverage() const override { return source_->core_functional_coverage(); }
    unsigned core_functional_version() const override { return source_->core_functional_version(); }
    std::string core_functional_policy_identity() const override { return source_->core_functional_policy_identity(); }
    const fst::mmuctl::Group* current_mmu_controls() const override { return core_functional_current_visible_?source_->current_mmu_controls():nullptr; }
    const fst::mmuctl::Baseline* mmu_control_baseline() const override { return source_->mmu_control_baseline(); }
    const fst::mmuctl::Footer* mmu_control_coverage() const override { return source_->mmu_control_coverage(); }
    unsigned mmu_control_version() const override { return source_->mmu_control_version(); }
    std::string mmu_control_policy_identity() const override { return source_->mmu_control_policy_identity(); }
    bool mmu_controls_ready() const override { return false; }
    bool mmu_controls_require_prefix() const override { return source_->mmu_control_version()!=0; }
    const fst::mmuctl_v2::Group* current_mmu_controls_v2() const override { return core_functional_current_visible_?source_->current_mmu_controls_v2():nullptr; }
    const fst::mmuctl_v2::Baseline* mmu_control_v2_baseline() const override { return source_->mmu_control_v2_baseline(); }
    const fst::mmuctl_v2::Footer* mmu_control_v2_coverage() const override { return source_->mmu_control_v2_coverage(); }
    const fst::archb::EntryMember* current_architectural_boundary_member() const override { return core_functional_current_visible_?source_->current_architectural_boundary_member():nullptr; }
    const fst::archb::Epoch* architectural_boundary_epoch() const override { return source_->architectural_boundary_epoch(); }
    const fst::archb::Footer* architectural_boundary_coverage() const override { return source_->architectural_boundary_coverage(); }
    const std::vector<fst::ArchitecturalBoundaryEvent>& boundaries_before(std::uint64_t ordinal) const override { return core_functional_current_visible_?source_->boundaries_before(ordinal):TraceSource::boundaries_before(ordinal); }
    bool architectural_boundary_bundle_ready() const override { return false; }


    bool store_facts_complete() const override { return source_->store_facts_complete(); }
    const fst::StoreRequestFacts* current_store_facts() const override { return source_->current_store_facts(); }
    const fst::StoreRequestFacts* current_memory_facts() const override { return source_->current_memory_facts(); }
    std::optional<bool> current_ordinary_load_eligible() const override { return source_->current_ordinary_load_eligible(); }
    bool contains_software_prefetch() const override { return source_->contains_software_prefetch(); }
    bool raw_syscall_microops() const override { return source_->raw_syscall_microops(); }
    bool functional_retirement_complete() const override { return source_->functional_retirement_complete(); }
    bool current_hardware_entry() const override { return source_->current_hardware_entry(); }
    bool current_precise_fault_abandonment() const override { return source_->current_precise_fault_abandonment(); }
    std::uint32_t current_memory_request_micro_pc() const override { return source_->current_memory_request_micro_pc(); }

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
    std::optional<std::uint64_t> current_source_record_ordinal() const override {
        return source_->current_source_record_ordinal();
    }
    std::optional<FunctionalSyncLayout> functional_sync_layout() const override {
        const auto l=source_->functional_sync_layout();
        if (!l || !has_record_counts_ || execution_records_.has_value() ||
            warmup_records_ > l->records || take_records_ != l->records-warmup_records_)
            return std::nullopt;
        return FunctionalSyncLayout{l->records,warmup_records_,take_records_,l->source_core};
    }
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
    const std::map<std::uint64_t, StaticInstructionInfo>*
    all_static_instructions() const override {
        return source_->all_static_instructions();
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
    bool core_functional_current_visible_=false;
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
    void enable_memory_request_roles();
    void enable_store_facts();
    void enable_static_uop_facts(const std::array<std::uint8_t,32>& policy);
    void enable_core_functional_facts(const fst::CoreFunctionalHeader& header);
    void enable_functional_retirement();
    void enable_raw_syscall_microops();
    void enable_frontend_facts();
    void register_frontend_macro(const fst::RetiredFrontendMacro& macro);
    void append(const TraceRecord& record, const SyscallMetadata* syscall_metadata,
                const std::vector<std::uint32_t>& dependency_extensions,
                fst::MemoryRequestRole role = fst::MemoryRequestRole::Data,
                std::uint32_t micro_pc = 0, bool hardware_entry = false, bool precise_fault_abandonment = false,
                const fst::StoreRequestFacts* store_facts = nullptr,
                const fst::StaticUopFactsRow* static_facts = nullptr,
                const std::vector<fst::StaticUopOperand>* static_operands = nullptr,
                const fst::CoreFunctionalRow* core_facts = nullptr,
                const std::vector<fst::CoreFunctionalOperand>* core_operands = nullptr);
    void register_virtual_page_mapping(
        const VirtualPageMapping& mapping);
    // Set the address space for the next appended record. Repeated values are
    // run-length encoded in `<trace>.asmap`; zero preserves the legacy
    // single/unspecified-address-space contract and cannot be mixed with
    // explicit non-zero IDs.
    void set_address_space_id(std::uint64_t address_space_id);
    void register_instruction_page_mapping(
        const InstructionPageMapping& mapping);
    void enable_asid_static_instruction_map();
    void register_static_instruction(std::uint64_t address_space_id,
                                     const StaticInstructionInfo& instruction);
    void register_static_instruction(
        const StaticInstructionInfo& instruction);
    void set_static_instruction_map_complete(bool complete = true) {
        static_instruction_map_complete_ = complete;
    }
    // Default output infers operand coverage from rows. Converters may
    // retain an intentionally weaker declaration without strengthening it.
    void set_static_instruction_operands_complete(bool complete) {
        if (closed_) throw std::logic_error("binary trace writer is closed");
        static_instruction_operands_complete_override_ = complete;
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
    std::unique_ptr<fst::RetiredFrontendWriter> frontend_writer_;
    std::unique_ptr<fst::StoreFactsWriter> store_facts_writer_;
    std::unique_ptr<fst::StaticUopWriter> static_uop_writer_;
    std::array<std::uint8_t,32> static_uop_policy_{};
    std::unique_ptr<fst::CoreFunctionalWriter> core_functional_writer_;
    std::optional<fst::CoreFunctionalHeader> core_functional_header_;
    bool memory_request_roles_enabled_ = false,functional_retirement_enabled_=false;
    std::vector<fst::RetirementRow> retirement_rows_;
    std::uint64_t memory_request_stream_hash_ = fst::kRequestStreamHashSeed;
    std::vector<fst::MemoryRequestRow> memory_request_rows_;

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
    std::map<std::pair<std::uint64_t, std::uint64_t>, StaticInstructionInfo>
        static_instruction_map_;
    std::vector<AddressSpaceTransition> address_space_transitions_;
    std::vector<InstructionPageMapping> instruction_page_mappings_;
    std::uint64_t current_address_space_id_ = 0;
    std::optional<bool> static_instruction_operands_complete_override_;
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
    std::optional<std::uint64_t> score_begin_record, score_end_record;
};

std::vector<TraceManifestEntry> read_trace_manifest(
    const std::string& manifest_path);
std::vector<std::unique_ptr<TraceSource>> open_trace_manifest(
    const std::string& manifest_path, std::uint32_t maximum_cores);

// Explicit host input policy; does not alter the process environment.
std::vector<std::unique_ptr<TraceSource>> open_trace_manifest(
    const std::string& manifest_path, std::uint32_t maximum_cores,
    bool preload_validated);

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
