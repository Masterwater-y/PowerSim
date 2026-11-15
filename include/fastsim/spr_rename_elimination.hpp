#pragma once
#include "fastsim/trace.hpp"
#include <array>

namespace fastsim {
enum class SprRenameKind : std::uint8_t { Execute, GprAlias, XmmAlias, Zero };
struct SprRenameDecision {
    enum class Reason : std::uint8_t { Ordinary, Missing, Shape, Pins, Resources };
    SprRenameKind kind = SprRenameKind::Execute;
    Reason reason = Reason::Ordinary;
    std::array<std::uint32_t, 2> producers{};
    bool eliminated() const { return kind != SprRenameKind::Execute; }
};

// A fixed-size static witness, not a physical rename-map proof. The two
// existing functional lane producers conservatively stand in for an XMM
// alias; response replay propagates their corrected ready edges normally.
inline SprRenameDecision spr_rename_decision(
    const TraceRecord& record, const TraceSource* source,
    bool matching_policy, bool unsupported_resources) {
    using Reason = SprRenameDecision::Reason;
    if (!source || !matching_policy) return {SprRenameKind::Execute, Reason::Missing};
    const auto* row = source->current_static_uop_facts();
    if (!row || !(row->flags & fst::kStaticExecutionKnown) ||
        !(row->flags & fst::kStaticOperandRolesKnown))
        return {SprRenameKind::Execute, Reason::Missing};
    const auto* macro = source->current_frontend_macro();
    unsigned opcode = 0, prefix = 0;
    bool rex_w = false, register_move = false, xmm = false;
    if (macro && macro->pc == record.pc &&
        macro->instruction_size <= macro->bytes.size()) {
        unsigned i = 0;
        while (i < macro->instruction_size) {
            const auto b = macro->bytes[i];
            if (b == 0x66 || b == 0xf2 || b == 0xf3) { prefix = b; ++i; }
            else if ((b & 0xf0) == 0x40) { rex_w = b & 8; ++i; }
            else break;
        }
        if (i < macro->instruction_size) {
            opcode = macro->bytes[i++];
            if (opcode == 0x0f && i < macro->instruction_size) {
                opcode = 0x100 | macro->bytes[i++];
                xmm = (opcode == 0x128 || opcode == 0x129 ||
                       ((opcode == 0x110 || opcode == 0x111) && prefix != 0xf2 && prefix != 0xf3) ||
                       ((opcode == 0x16f || opcode == 0x17f) && (prefix == 0x66 || prefix == 0xf3)));
            }
            register_move = i + 1 == macro->instruction_size &&
                            (macro->bytes[i] & 0xc0) == 0xc0;
        }
    }
    const bool gpr = register_move && !prefix && rex_w &&
                     (opcode == 0x89 || opcode == 0x8b);
    const bool move = gpr || (register_move && xmm);
    if (!move && !row->zero_idiom) return {};
    if (unsupported_resources) return {SprRenameKind::Execute, Reason::Resources};
    constexpr auto required = fst::kFrontendComplete | fst::kFrontendInstructionBytes |
                              fst::kFrontendSharedAllocation | fst::kFrontendMicrocode |
                              fst::kFrontendPseudo;
    if (!macro || !macro->instruction_size || (macro->known_flags & required) != required ||
        !(macro->flags & fst::kFrontendComplete) ||
        (macro->flags & (fst::kFrontendSharedAllocation | fst::kFrontendMicrocode | fst::kFrontendPseudo)) ||
        macro->observed_uops != 1 || macro->end_record != macro->begin_record + 1 ||
        macro->begin_record != row->record_ordinal ||
        record.is_memory() || record.is_syscall() || record.is_serializing() ||
        has_flag(record.flags, kBranch) ||
        source->current_precise_fault_abandonment() ||
        row->source_count > 4 || row->destination_count != record.n_dst ||
        row->destination_count == 0)
        return {SprRenameKind::Execute, Reason::Shape};
    const auto* core = source->current_core_functional_facts();
    if (core && (core->root_allocation_flags ||
                 (core->static_flags & (fst::kCoreMicrocodeControl | fst::kCoreSerializing |
                                       fst::kCoreNonSpeculative | fst::kCoreDelayedCommit))))
        return {SprRenameKind::Execute, Reason::Shape};
    for (const auto& operand : source->current_core_functional_operands())
        if ((operand.flags & fst::kCorePinKnown) && operand.static_num_pinned_writes != 0)
            return {SprRenameKind::Execute, Reason::Pins};
    const auto& operands = source->current_static_uop_operands();
    if (operands.size() != row->source_count)
        return {SprRenameKind::Execute, Reason::Missing};
    SprRenameDecision result;
    unsigned count = 0;
    for (const auto& operand : operands) {
        if (row->zero_idiom) {
            if (operand.producer_distance) return {SprRenameKind::Execute, Reason::Shape};
            continue;
        }
        if (operand.reg_class == 255 && operand.producer_distance == 0) continue;
        if (count == result.producers.size() ||
            operand.reg_class != (gpr ? 0 : 1))
            return {SprRenameKind::Execute, Reason::Shape};
        result.producers[count++] = operand.producer_distance;
    }
    if (row->zero_idiom) {
        if (row->zero_idiom > 4 || row->integer_family || row->candidate_ports)
            return {SprRenameKind::Execute, Reason::Shape};
        result.kind = SprRenameKind::Zero;
    } else {
        if (count != (gpr ? 1u : 2u) || row->destination_count != count ||
            record.canonical_op_class() != (gpr ? 1 : 10))
            return {SprRenameKind::Execute, Reason::Shape};
        if (core && (core->known_flags & fst::kCoreStaticKnown) &&
            !(core->static_flags & (gpr ? fst::kCoreFullGprMove : fst::kCoreFullXmmMove)))
            return {SprRenameKind::Execute, Reason::Shape};
        result.kind = gpr ? SprRenameKind::GprAlias : SprRenameKind::XmmAlias;
    }
    return result;
}
} // namespace fastsim
