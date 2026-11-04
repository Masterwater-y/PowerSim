#include "fastsim/trace.hpp"
#include <iostream>
#include <stdexcept>
#include <map>
#include <set>

int main(int argc, char** argv) {
    try {
        if (argc < 2) throw std::runtime_error("expected one or more FST paths");
        std::uint64_t records = 0, spans = 0, slots = 0, partial = 0, pseudo = 0;
        std::uint64_t paged_requests=0;
        std::uint64_t instruction_files=0,instruction_spans=0,instruction_paths=0,instruction_leaves=0;
        std::uint64_t instruction_unknown=0,instruction_pseudo=0,instruction_partial=0;
        std::array<std::uint64_t,7> instruction_reasons{};
        std::array<std::uint64_t,3> instruction_versions{};
        std::uint64_t decode_context_known=0,decode_context_unknown=0,origin_decode_mismatch=0;
        std::uint64_t origin_retirement_mismatch=0,decode_retirement_mismatch=0,context_crossings=0;
        std::uint64_t mixed_origins=0,historical_walk_roots=0;
        std::uint64_t fact_files=0, memory=0, loads=0, load_proven=0, load_unproven=0;
        std::set<std::string> static_policies;
        std::uint64_t static_records=0, static_execution=0, operand_roles=0, address_data_roles=0, paired_members=0, partial_groups=0;
        std::uint64_t stores=0, store_proven=0, store_incomplete=0, address_uops=0, data_uops=0;
        for (int file = 1; file < argc; ++file) {
            fastsim::BinaryTraceSource source(argv[file]);
            fact_files += source.store_facts_complete();
            instruction_files += source.instruction_translations_declared();
            if(source.instruction_translations_declared())++instruction_versions.at(source.instruction_translation_version());
            std::map<std::uint64_t,unsigned> store_groups;
            std::uint64_t largest_store_group=0;
            if(!source.static_uop_policy_identity().empty()) static_policies.insert(source.static_uop_policy_identity());
            fastsim::TraceRecord record;
            while (source.next(record)) {
                const auto* macro = source.current_frontend_macro();
                const auto ordinal = source.current_frontend_record_ordinal();
                if (!macro || !ordinal || macro->known_flags != fastsim::fst::kFrontendRequiredKnown)
                    throw std::runtime_error("missing or unknown retired frontend facts");
                if (!record.retires() || !(macro->flags & fastsim::fst::kFrontendStartsMacro))
                    throw std::runtime_error("frontend input lacks retirement or retained functional start");
                ++records;
                if(const auto* f=source.current_static_uop_facts()) {
                    ++static_records;
                    static_execution += bool(f->flags&fastsim::fst::kStaticExecutionKnown);
                    operand_roles += bool(f->flags&fastsim::fst::kStaticOperandRolesKnown);
                    address_data_roles += bool(f->flags&fastsim::fst::kStaticAddressDataKnown);
                    if(f->flags&fastsim::fst::kStaticAbandonStoreGroups) {
                        for(const auto& group:store_groups) partial_groups += group.second!=3;
                        store_groups.clear();
                    }
                    if(f->store_group) {
                        ++paired_members;
                        const unsigned bit=f->store_role==fastsim::fst::StaticStoreRole::Address?1:2;
                        if(!store_groups.count(f->store_group)) {
                            if(f->store_group<=largest_store_group) throw std::runtime_error("reused static store-group identity");
                            largest_store_group=f->store_group;
                        }
                        auto& group=store_groups[f->store_group];
                        if(group&bit) throw std::runtime_error("duplicate static store-group role");
                        group |= bit;
                        if(group==3) store_groups.erase(f->store_group);
                    }
                }
                address_uops += record.canonical_op_class()==fastsim::kSprStoreAddress;
                data_uops += record.canonical_op_class()==fastsim::kSprStoreData;
                if(record.is_memory()) {
                    ++memory;
                    const auto* all_facts=source.current_memory_facts();
                    paged_requests += all_facts && (all_facts->attributes&fastsim::fst::kStorePagedFragments);
                    if(record.is_write()) {
                        ++stores;
                        const auto* facts=source.current_store_facts();
                        store_proven += facts && (facts->attributes&fastsim::fst::kStoreOrdinary);
                        store_incomplete += !facts || !(facts->attributes&fastsim::fst::kStoreFactsComplete);
                    } else {
                        ++loads;
                        const auto eligible=source.current_ordinary_load_eligible();
                        load_proven += eligible.value_or(false);
                        load_unproven += !eligible.has_value();
                    }
                }
                if (*ordinal == macro->begin_record) {
                    ++spans;
                    if (source.instruction_translations_declared()) {
                        const auto facts=source.current_instruction_fetch();
                        if (!facts) throw std::runtime_error("declared instruction translation lacks current macro binding");
                        ++instruction_spans;
                        instruction_partial += !(macro->flags&fastsim::fst::kFrontendComplete);
                        using fastsim::fst::InstructionFetchStatus;
                        if(facts->status!=InstructionFetchStatus::NotApplicable) {
                            decode_context_known += facts->decode_context_known;
                            decode_context_unknown += !facts->decode_context_known;
                            const bool origin_decode=fastsim::fst::instruction_origin_decode_asid_mismatch(*facts);
                            const bool origin_retirement=fastsim::fst::instruction_origin_retirement_asid_mismatch(*facts,*macro);
                            const bool decode_retirement=facts->decode_context_known && facts->decode_address_space_id!=macro->address_space_id;
                            origin_decode_mismatch += origin_decode;origin_retirement_mismatch += origin_retirement;
                            decode_retirement_mismatch += decode_retirement;
                            context_crossings += origin_decode || origin_retirement || decode_retirement;
                            bool mixed=false,historical=false;
                            for(const auto& fragment:facts->fragments) {
                                mixed |= fragment.mapping.address_space_id!=facts->fragments.front().mapping.address_space_id;
                                historical |= (fragment.mapping.flags&fastsim::fst::kTranslationPathKnown) &&
                                    fragment.mapping.walk_root!=fragment.mapping.address_space_id;
                            }
                            mixed_origins += mixed;historical_walk_roots += historical;
                        }
                        if(facts->status==InstructionFetchStatus::Mapped) {
                            instruction_paths += facts->path_complete;
                            instruction_leaves += !facts->path_complete;
                        } else if(facts->status==InstructionFetchStatus::NotApplicable) ++instruction_pseudo;
                        else {++instruction_unknown;++instruction_reasons.at(unsigned(facts->reason));}
                    }
                    slots += macro->flags & fastsim::fst::kFrontendSharedAllocation ? 1 : macro->observed_uops;
                    partial += !(macro->flags & fastsim::fst::kFrontendComplete);
                    pseudo += bool(macro->flags & fastsim::fst::kFrontendPseudo);
                }
            }
            for(const auto& group:store_groups) partial_groups += group.second!=3;
        }
        std::cout << "{\"status\":\"passed\",\"scope\":\"full-retired-functional-stream\","
                  << "\"files\":" << argc-1 << ",\"records\":" << records
                  << ",\"macro_spans\":" << spans << ",\"allocation_slots\":" << slots
                  << ",\"partial_spans\":" << partial << ",\"pseudo_spans\":" << pseudo
                  << ",\"functional_memory_facts\":{\"files_declared\":" << fact_files
                  << ",\"paged_fragment_descriptors\":" << paged_requests
                  << ",\"memory_records\":" << memory << ",\"read_records\":" << loads
                  << ",\"ordinary_loads_proven\":" << load_proven << ",\"load_eligibility_unproven\":" << load_unproven
                  << ",\"write_records\":" << stores << ",\"ordinary_stores_proven\":" << store_proven
                  << ",\"store_request_descriptors_incomplete\":" << store_incomplete
                  << ",\"store_address_uops\":" << address_uops << ",\"store_data_uops\":" << data_uops
                  << "},\"static_uop_facts\":{\"records\":" << static_records
                  << ",\"execution_descriptors\":" << static_execution
                  << ",\"operand_role_descriptors\":" << operand_roles
                  << ",\"address_data_descriptors\":" << address_data_roles
                  << ",\"paired_store_members\":" << paired_members
                  << ",\"partial_store_groups\":" << partial_groups
                  << ",\"policy_identities\":[";
        bool first=true;
        for(const auto& policy:static_policies) {if(!first)std::cout << ',';first=false;std::cout << '\"' << policy << '\"';}
        std::cout << "]},\"instruction_translation_facts\":{\"schema\":\"fst-instruction-translations-v2\","
                  << "\"files_declared\":" << instruction_files << ",\"macro_spans\":" << instruction_spans
                  << ",\"observed_path_spans\":" << instruction_paths << ",\"leaf_only_spans\":" << instruction_leaves
                  << ",\"unknown_spans\":" << instruction_unknown << ",\"pseudo_not_applicable_spans\":" << instruction_pseudo
                  << ",\"partial_macro_spans\":" << instruction_partial
                  << ",\"wire_version_1_files\":" << instruction_versions[1] << ",\"wire_version_2_files\":" << instruction_versions[2]
                  << ",\"decode_context_known_spans\":" << decode_context_known << ",\"decode_context_unknown_spans\":" << decode_context_unknown
                  << ",\"origin_decode_asid_mismatch_spans\":" << origin_decode_mismatch
                  << ",\"origin_retirement_asid_mismatch_spans\":" << origin_retirement_mismatch
                  << ",\"decode_retirement_asid_mismatch_spans\":" << decode_retirement_mismatch
                  << ",\"context_crossing_spans\":" << context_crossings << ",\"mixed_origin_asid_spans\":" << mixed_origins
                  << ",\"historical_walk_root_spans\":" << historical_walk_roots
                  << ",\"logical_mmu_controls_complete\":false,\"full_spr_profile_qualified\":false,\"unknown_reason_counts\":[";
        for(unsigned reason=0;reason<instruction_reasons.size();++reason) {if(reason)std::cout<<',';std::cout<<instruction_reasons[reason];}
        std::cout << "]}}\n";
    } catch (const std::exception& error) {
        std::cerr << "retired frontend audit: " << error.what() << '\n';
        return 1;
    }
}
