#pragma once
#include "fastsim/fst_static_uops.hpp"
#include <optional>
namespace fastsim::fst {
void validate_static_uop_projection(const StaticUopFactsRow&,const std::vector<StaticUopOperand>&,
                                   const void* record,const std::vector<std::uint32_t>& extra);
class StaticUopReader {
    gzFile in_=nullptr;
    StaticUopHeader header_;
    std::optional<StaticUopFactsRow> current_;
    std::vector<StaticUopOperand> operands_;
    std::uint64_t records_=0,observed_=0,hash_=14695981039346656037ull;
    bool finished_=false;
    void read(void* p,unsigned n);
public:
    StaticUopReader(const std::string& path,std::uint32_t core,std::uint64_t records);
    ~StaticUopReader();
    void observe(std::uint64_t ordinal,const void* record,const std::vector<std::uint32_t>& extra);
    void finish();
    const StaticUopFactsRow* current() const {return current_?&*current_:nullptr;}
    const std::vector<StaticUopOperand>& operands() const {return operands_;}
    std::string policy_identity() const;
};
} // namespace fastsim::fst
