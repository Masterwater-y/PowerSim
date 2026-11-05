#pragma once
#include "fastsim/fst_core_functional_facts.hpp"
#include "fastsim/fst_static_uops.hpp"
#include <memory>
#include <optional>
namespace fastsim::fst {
struct CoreFunctionalCoverage {
    std::uint64_t records=0,macros=0;
    std::array<std::uint64_t,8> known{},not_applicable{},unknown{};
    std::uint64_t na_rom_rows=0,boundary_rows=0,unknown_applicable_rows=0;
    std::array<std::uint64_t,4> normalization{};
    std::array<std::uint64_t,9> source_classes{},destination_classes{};
    std::uint64_t frontend_retirement_mismatch=0,decoder_frontend_user_mismatch=0;
    std::uint64_t gpr_candidates=0,xmm_candidates=0,microfused=0,stack=0;
    std::uint64_t serialize_before=0,serialize_after=0,read_barriers=0,write_barriers=0;
};
void validate_core_projection(const CoreFunctionalRow&,const std::vector<CoreFunctionalOperand>&,
                              const void* hot,const StaticUopFactsRow&,const std::vector<StaticUopOperand>&);
class CoreFunctionalReader {
    struct Impl;std::unique_ptr<Impl> impl_;
public:
    CoreFunctionalReader(const std::string& path,std::uint32_t core,std::uint64_t records,const std::string& policy,bool architectural_bundle=false);
    ~CoreFunctionalReader();
    void observe(std::uint64_t ordinal,const void* hot,const RetiredFrontendMacro& macro,
                 const StaticUopFactsRow&,const std::vector<StaticUopOperand>&);
    void clear_current();
    const CoreFunctionalRow* current() const;
    const std::vector<CoreFunctionalOperand>& operands() const;
    const CoreFunctionalHeader& header() const;
    const CoreFunctionalCoverage& coverage() const;
    std::string policy_identity() const;
};
}
