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
        std::array<std::uint64_t,4> data_versions{};
        std::uint64_t data_files=0,data_records=0,data_bytes=0,data_mapped=0,data_paths=0,data_leaves=0,data_unknown=0;
        std::uint64_t data_mapped_bytes=0,data_path_bytes=0,data_leaf_bytes=0,data_unknown_bytes=0,data_special_bytes=0;
        std::uint64_t data_cross=0,data_mixed=0,data_historical=0;
        std::uint64_t instruction_files=0,instruction_spans=0,instruction_paths=0,instruction_leaves=0;
        std::uint64_t instruction_unknown=0,instruction_pseudo=0,instruction_partial=0;
        std::array<std::uint64_t,7> instruction_reasons{};
        std::array<std::uint64_t,3> instruction_versions{};
        std::uint64_t decode_context_known=0,decode_context_unknown=0,origin_decode_mismatch=0;
        std::uint64_t origin_retirement_mismatch=0,decode_retirement_mismatch=0,context_crossings=0;
        std::uint64_t mixed_origins=0,historical_walk_roots=0;
        std::uint64_t fact_files=0, memory=0, loads=0, load_proven=0, load_unproven=0;
        std::set<std::string> static_policies;
        std::set<std::string> core_policies;
        std::map<std::uint32_t,std::array<std::uint32_t,8>> core_layouts;
        fastsim::fst::CoreFunctionalCoverage core_totals;
        std::uint64_t core_files=0;
        std::uint64_t mmu_files=0,mmu_ready=0,mmu_baselines=0,mmu_rows=0,mmu_sealed=0;
        fastsim::fst::mmuctl::Footer mmu_totals;
        std::set<std::string> mmu_policies;

        std::uint64_t static_records=0, static_execution=0, operand_roles=0, address_data_roles=0, paired_members=0, partial_groups=0;
        std::uint64_t stores=0, store_proven=0, store_incomplete=0, address_uops=0, data_uops=0;
        for (int file = 1; file < argc; ++file) {
            fastsim::BinaryTraceSource source(argv[file]);
            if(const auto* coverage=source.core_functional_coverage()) {
                ++core_files;core_policies.insert(source.core_functional_policy_identity());
                if(!core_layouts.emplace(source.core_id(),source.core_functional_header()->architectural_registers).second)
                    throw std::runtime_error("duplicate core functional source core ID");
                core_totals.records+=coverage->records;core_totals.macros+=coverage->macros;
                for(unsigned i=0;i<8;++i)core_totals.known[i]+=coverage->known[i];
                for(unsigned i=0;i<4;++i)core_totals.normalization[i]+=coverage->normalization[i];
                for(unsigned i=0;i<9;++i){core_totals.source_classes[i]+=coverage->source_classes[i];core_totals.destination_classes[i]+=coverage->destination_classes[i];}
                core_totals.frontend_retirement_mismatch+=coverage->frontend_retirement_mismatch;
                core_totals.decoder_frontend_user_mismatch+=coverage->decoder_frontend_user_mismatch;
                core_totals.gpr_candidates+=coverage->gpr_candidates;core_totals.xmm_candidates+=coverage->xmm_candidates;
                core_totals.microfused+=coverage->microfused;core_totals.stack+=coverage->stack;
                core_totals.serialize_before+=coverage->serialize_before;core_totals.serialize_after+=coverage->serialize_after;
                core_totals.read_barriers+=coverage->read_barriers;core_totals.write_barriers+=coverage->write_barriers;
            }
            if (const auto* c=source.mmu_control_coverage()) {
                ++mmu_files;mmu_ready+=source.mmu_controls_ready();mmu_rows+=c->hot;mmu_sealed+=c->sealed;
                const auto* baseline=source.mmu_control_baseline();
                mmu_baselines+=baseline&&baseline->known==15&&baseline->cut==1;
                mmu_policies.insert(source.mmu_control_policy_identity());
                mmu_totals.total_writes+=c->total_writes;mmu_totals.retained_writes+=c->retained_writes;mmu_totals.unbound_writes+=c->unbound_writes;
                mmu_totals.total_commands+=c->total_commands;mmu_totals.retained_commands+=c->retained_commands;mmu_totals.unbound_commands+=c->unbound_commands;
                mmu_totals.visits+=c->visits;mmu_totals.groups+=c->groups;mmu_totals.suppressed+=c->suppressed;mmu_totals.pending+=c->pending;
                for(unsigned i=0;i<16;++i)mmu_totals.reasons[i]+=c->reasons[i];
            }
            fact_files += source.store_facts_complete();
            data_files += source.dynamic_translations();
            if(source.dynamic_translations())++data_versions.at(source.dynamic_translation_version());
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
                    if(source.dynamic_translations() && record.size) {
                        ++data_records;data_bytes+=record.size;
                        bool mapped=true,path=true,cross=false,mixed=false,historical=false;
                        std::optional<std::uint64_t> first_origin;
                        for(const auto& f:source.current_translation_fragments()) {
                            const auto& m=f.mapping;
                            const bool known=(m.flags&fastsim::fst::kTranslationLeafKnown) || m.kind!=fastsim::fst::TranslationKind::kPaged;
                            const bool observed=m.flags&fastsim::fst::kTranslationPathKnown;
                            mapped &= known;path &= observed;
                            data_mapped_bytes+=known?f.size:0;data_path_bytes+=observed?f.size:0;
                            data_leaf_bytes+=(known&&!observed)?f.size:0;data_unknown_bytes+=known?0:f.size;
                            data_special_bytes+=m.kind!=fastsim::fst::TranslationKind::kPaged?f.size:0;
                            if(known) {
                                const auto retirement=source.current_translation_retirement_asid().value_or(source.current_address_space_id());
                                cross |= m.address_space_id!=retirement;
                                if(first_origin)mixed |= *first_origin!=m.address_space_id;else first_origin=m.address_space_id;
                                historical |= observed && m.walk_root!=m.address_space_id;
                            }
                        }
                        data_mapped+=mapped;data_paths+=mapped&&path;data_leaves+=mapped&&!path;data_unknown+=!mapped;
                        data_cross+=cross;data_mixed+=mixed;data_historical+=historical;
                    }
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
        std::cout << "]},\"data_translation_facts\":{\"schema\":\"fst-dynamic-translations-v3\","
                  << "\"files_declared\":" << data_files << ",\"wire_version_1_files\":" << data_versions[1]
                  << ",\"wire_version_2_files\":" << data_versions[2] << ",\"wire_version_3_files\":" << data_versions[3]
                  << ",\"memory_records\":" << data_records << ",\"memory_bytes\":" << data_bytes
                  << ",\"byte_mapping_records\":" << data_mapped << ",\"observed_path_records\":" << data_paths
                  << ",\"leaf_only_records\":" << data_leaves << ",\"unknown_mapping_records\":" << data_unknown
                  << ",\"byte_mapping_bytes\":" << data_mapped_bytes << ",\"observed_path_bytes\":" << data_path_bytes
                  << ",\"leaf_only_bytes\":" << data_leaf_bytes << ",\"unknown_mapping_bytes\":" << data_unknown_bytes
                  << ",\"special_request_bytes\":" << data_special_bytes
                  << ",\"origin_retirement_asid_mismatch_records\":" << data_cross
                  << ",\"mixed_origin_asid_records\":" << data_mixed << ",\"historical_walk_root_records\":" << data_historical
                  << ",\"logical_mmu_controls_complete\":false,\"full_spr_profile_qualified\":false},\"instruction_translation_facts\":{\"schema\":\"fst-instruction-translations-v2\","
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
        std::cout << "]},\"core_functional_facts\":{\"schema\":\"fst-core-functional-v1\",\"files_declared\":" << core_files
                  << ",\"wire_version_1_files\":" << core_files << ",\"records\":" << core_totals.records
                  << ",\"macro_spans\":" << core_totals.macros << ",\"known_records\":{";
        const char* names[]={"decoder","frontend","allocation","static","fusion","operands","pins","normalization"};
        for(unsigned i=0;i<8;++i){if(i)std::cout<<',';std::cout<<'\"'<<names[i]<<"\":"<<core_totals.known[i];}
        std::cout<<"},\"unknown_records\":{";
        for(unsigned i=0;i<8;++i){if(i)std::cout<<',';std::cout<<'\"'<<names[i]<<"\":"<<core_totals.records-core_totals.known[i];}
        auto array=[](const auto& values){std::cout<<'[';bool first=true;for(const auto& v:values){if(!first)std::cout<<',';first=false;std::cout<<v;}std::cout<<']';};
        std::cout<<"},\"normalization_status_counts\":";array(core_totals.normalization);
        std::cout<<",\"source_class_counts\":";array(core_totals.source_classes);
        std::cout<<",\"destination_class_counts\":";array(core_totals.destination_classes);
        std::cout<<",\"policy_identities\":[";first=true;
        for(const auto& policy:core_policies){if(!first)std::cout<<',';first=false;std::cout<<'\"'<<policy<<'\"';}
        std::cout<<"],\"architectural_registers_by_core\":{";first=true;
        for(const auto& entry:core_layouts){if(!first)std::cout<<',';first=false;std::cout<<'\"'<<entry.first<<"\":";array(entry.second);}
        std::cout<<"},\"frontend_retirement_asid_mismatch_records\":"<<core_totals.frontend_retirement_mismatch
                 <<",\"decoder_frontend_user_mismatch_records\":"<<core_totals.decoder_frontend_user_mismatch
                 <<",\"static_gpr_move_candidates\":"<<core_totals.gpr_candidates<<",\"static_xmm_move_candidates\":"<<core_totals.xmm_candidates
                 <<",\"microfused_memory_records\":"<<core_totals.microfused<<",\"stack_allocation_records\":"<<core_totals.stack
                 <<",\"serialize_before_records\":"<<core_totals.serialize_before<<",\"serialize_after_records\":"<<core_totals.serialize_after
                 <<",\"read_barrier_records\":"<<core_totals.read_barriers<<",\"write_barrier_records\":"<<core_totals.write_barriers
                 <<",\"runtime_qualified\":false,\"full_spr_profile_qualified\":false},\"logical_mmu_controls\":{\"schema\":\"fst-logical-mmu-controls-v1\",\"files_declared\":"<<mmu_files
                 <<",\"wire_version_1_files\":"<<mmu_files<<",\"structurally_valid_files\":"<<mmu_files
                 <<",\"known_baseline_files\":"<<mmu_baselines<<",\"control_ready_files\":"<<mmu_ready
                 <<",\"records\":"<<mmu_rows<<",\"sealed_records\":"<<mmu_sealed
                 <<",\"total_writes\":"<<mmu_totals.total_writes<<",\"retained_writes\":"<<mmu_totals.retained_writes<<",\"unbound_writes\":"<<mmu_totals.unbound_writes
                 <<",\"total_commands\":"<<mmu_totals.total_commands<<",\"retained_commands\":"<<mmu_totals.retained_commands<<",\"unbound_commands\":"<<mmu_totals.unbound_commands
                 <<",\"propagation_visits\":"<<mmu_totals.visits<<",\"diagnostic_groups\":"<<mmu_totals.groups<<",\"suppressed_operations\":"<<mmu_totals.suppressed<<",\"pending_owners\":"<<mmu_totals.pending
                 <<",\"reason_counts\":{";
        for(unsigned i=0;i<16;++i){if(i)std::cout<<',';std::cout<<'"'<<fastsim::fst::mmuctl::ReasonNames[i]<<"\":"<<mmu_totals.reasons[i];}
        std::cout<<"},\"policy_identities\":[";first=true;
        for(const auto& policy:mmu_policies){if(!first)std::cout<<',';first=false;std::cout<<'"'<<policy<<'"';}
        std::cout<<"],\"initial_tlb_residency_known\":false,\"full_spr_profile_qualified\":false}}\n";
    } catch (const std::exception& error) {
        std::cerr << "retired frontend audit: " << error.what() << '\n';
        return 1;
    }
}
