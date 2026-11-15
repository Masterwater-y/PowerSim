#pragma once
#include "fastsim/fst_static_uops.hpp"
#include <cstdint>

namespace fastsim {
// Only target-policy static decoder facts enter this classifier. Unknown is
// not an implicit generic route. Dynamic move elimination and fusion remain
// the caller's modeled decisions, never native observations.
struct SprIntegerFamilyInput {
    enum class Status {Unknown,Generic,Managed,StaticZero};
    Status status=Status::Unknown;
    std::uint8_t family=0;
    std::uint16_t candidates=0,nominal_latency=0;
    static SprIntegerFamilyInput classify(const fst::StaticUopFactsRow* row,
                                           bool matching_policy) {
        if(!row || !matching_policy || !(row->flags & fst::kStaticExecutionKnown) ||
           row->integer_family>5 || row->candidate_ports>0xfff || !row->nominal_latency ||
           row->zero_idiom>4 || ((row->integer_family==0)!=(row->candidate_ports==0)))
            return {};
        if(row->zero_idiom) {
            if(row->integer_family || row->candidate_ports)return {};
            return {Status::StaticZero};
        }
        if(!row->integer_family)return {Status::Generic};
        return {Status::Managed,row->integer_family,row->candidate_ports,row->nominal_latency};
    }
};
} // namespace fastsim
