"""Build fixtures around literal native producer bodies, with no copied serializers."""
def serializers(gem5_root, producer):
    header=(gem5_root/'src/cpu/o3/probe/tao_trace.hh').read_text()
    start=header.index('    struct NativeHierarchyAggregate {')
    structs=header[start:header.index('    struct NativeAnomalySample {',start)]
    start=producer.index('static void\nwriteNativeHierarchyJson(')
    hierarchy=producer[start:producer.index('\nconst char *',start)]
    result='#include <cstdio>\n'+structs+hierarchy
    result+='const char *nativeDiagScopeName(int){return "user";}\n'
    for method,signature,setup in (
        ('writeNativeResponseCommit','serializeCommit',
         'uint32_t core_id=0,tid=1; uint64_t line_requests=1; int cycle_class=0; bool fallback_source=false; struct Inst{uint64_t seqNum=49188;} object; auto inst=&object; struct Attr{unsigned path_class=0;} attr;'),
        ('refreshNativeResponsePending','serializeResolution',
         'uint32_t core_id=0;bool ruby_response=false;struct Pending{uint32_t thread_id=1;uint64_t inst_seq_num=49188,line_requests=1;int cycle_class=0;bool fallback_source=false;unsigned proxy_path_class=0;} pending;'),
    ):
        start=producer.index('TaoTrace::'+method+'(')
        start=producer.index('std::fprintf(out_native_response_diag_,',start)
        end=producer.index('std::fputs("}\\n", out_native_response_diag_);',start)+len('std::fputs("}\\n", out_native_response_diag_);')
        result+='void '+signature+'(const R::Snapshot&native){std::FILE*out_native_response_diag_=stdout;'+setup+producer[start:end]+'}\n'
    start=producer.index('    auto write_hierarchy = []')
    end=producer.index('\n\n    const double coverage',start)
    result+='void serializePopulation(){std::FILE*summary=stdout;'+producer[start:end]+'''
NativeScopeAggregate p; p.memory_uops=1;p.architectural_line_requests=1;
p.bypass_accepted_fragments=1;p.bypass_response_fragments=1;p.device_bypass_uops=1;
p.no_ruby_uops=1;p.hierarchy_complete_uops=1;write_population(p);std::fputc('\n',stdout);}
'''
    start=producer.index('    const double coverage = native_diag_committed_uops_')
    end=producer.index('    static constexpr const char *path_names[]',start)
    result+='void serializeSummary(){std::FILE*summary=stdout; NativeScopeAggregate total; total.memory_uops=1;total.bypass_accepted_fragments=1;total.bypass_response_fragments=1;total.device_bypass_uops=1;total.no_ruby_uops=1;total.hierarchy_complete_uops=1;'
    result+='uint32_t core_id=0;bool emit_native_response_jsonl_=false;uint64_t native_diag_committed_uops_=1,native_diag_packet_source_uops_=1,native_diag_fallback_source_uops_=0,native_diag_response_at_commit_uops_=0,native_diag_terminal_at_commit_uops_=1,native_diag_late_response_uops_=0,native_diag_late_terminal_uops_=0,native_diag_response_without_fact_uops_=0,functional_target_drain_polls_=0;auto legacyNativePendingCount=[](){return 0;};'
    result+=producer[start:end]+'std::fputs("\\\"fixture\\\":true}\\n",summary);}'
    # Escape the C++ newline character inside the generated source.
    return result.replace("std::fputc('\n',stdout)","std::fputc('\\n',stdout)")
