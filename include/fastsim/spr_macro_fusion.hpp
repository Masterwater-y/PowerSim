#pragma once
#include <cstdint>
#include <limits>
#include <optional>

namespace fastsim {
// A candidate decision from functional facts and the model's own frontend
// grouping. It does not reproduce gem5's dynamic fetch or prove native fusion.
enum class SprFusionDecision { Ineligible, Eligible, Unknown };
struct SprFusionPair {
    std::optional<std::uint64_t> producer_pc, branch_pc, producer_asid, branch_asid;
    std::optional<std::uint8_t> producer_size, branch_condition;
    std::optional<std::uint16_t> allowed_jcc_mask;
    std::optional<bool> producer_from_user, branch_from_user, producer_alu;
    std::optional<bool> producer_group_ready, producer_has_fused_branch;
    std::optional<bool> branch_single, branch_microcode, same_delivery_batch;
};
inline SprFusionDecision spr_fusion_pair(const SprFusionPair& p) {
    if (!p.producer_pc || !p.branch_pc || !p.producer_asid || !p.branch_asid ||
        !p.producer_size || !p.branch_condition || !p.allowed_jcc_mask ||
        !p.producer_from_user || !p.branch_from_user || !p.producer_alu ||
        !p.producer_group_ready || !p.producer_has_fused_branch ||
        !p.branch_single || !p.branch_microcode || !p.same_delivery_batch)
        return SprFusionDecision::Unknown;
    if (*p.producer_size==0 || *p.producer_size>15 ||
        *p.producer_pc>std::numeric_limits<std::uint64_t>::max()-*p.producer_size ||
        (*p.branch_condition>15 && *p.branch_condition!=255))
        return SprFusionDecision::Unknown;
    if (!*p.producer_alu || !*p.producer_group_ready || *p.producer_has_fused_branch ||
        !*p.branch_single || *p.branch_microcode || !*p.same_delivery_batch ||
        *p.producer_asid!=*p.branch_asid || *p.producer_from_user!=*p.branch_from_user ||
        *p.producer_pc+*p.producer_size!=*p.branch_pc ||
        *p.producer_pc/64!=*p.branch_pc/64 || *p.branch_condition==255)
        return SprFusionDecision::Ineligible;
    return (*p.allowed_jcc_mask & (std::uint16_t{1} << *p.branch_condition))
        ? SprFusionDecision::Eligible : SprFusionDecision::Ineligible;
}
}
