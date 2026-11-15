#include "fastsim/retired_frontend_facts.hpp"
#include "fastsim/trace.hpp"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

using namespace fastsim;
namespace {
void require(bool ok, const char *message) {
    if (!ok) throw std::runtime_error(message);
}
void set_workers(const char *n) {
    if (setenv("FASTSIM_TRACE_OPEN_WORKERS",n,1)) throw std::runtime_error("setenv failed");
}
void write_trace(const std::string &path, unsigned core) {
    BinaryTraceWriter writer(path,core);
    writer.enable_frontend_facts();
    writer.set_address_space_id(0x1000+core);
    for (unsigned i=0;i<5;++i) {
        fst::RetiredFrontendMacro m;
        m.begin_record=i;m.end_record=i+1;m.observed_uops=1;
        m.pc=0x1000+core*0x100+i;m.address_space_id=0x1000+core;
        m.code_identity=100+core*5+i;m.semantic_identity=200+core*5+i;
        m.known_flags=fst::kFrontendRequiredKnown;
        m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;
        m.instruction_size=1;m.bytes[0]=0x90;
        writer.register_frontend_macro(m);
        TraceRecord record;record.pc=m.pc;
        writer.append(record);
    }
    writer.close();
}
std::vector<std::uint64_t> read_all(const std::string &manifest) {
    auto sources=open_trace_manifest(manifest,4);
    require(sources.size()==4,"wrong core count");
    std::vector<std::uint64_t> result;
    for (unsigned core=0;core<4;++core) {
        const auto layout=sources[core]->functional_sync_layout();
        require(layout && layout->source_core==core && layout->records==5,"wrong source identity");
        TraceRecord record;
        for(unsigned i=0;i<5;++i) {
            require(sources[core]->next(record),"truncated trace");
            const auto *macro=sources[core]->current_frontend_macro();
            require(record.pc==0x1000+core*0x100+i && macro && macro->pc==record.pc &&
                    macro->address_space_id==0x1000+core && macro->begin_record==i,
                    "wrong source data or companion");
            result.push_back(record.pc);
        }
        require(!sources[core]->next(record),"extra source data");
    }
    return result;
}
std::string failure(const std::string &manifest) {
    try { auto unused=open_trace_manifest(manifest,4); (void)unused; }
    catch(const std::exception &e) { return e.what(); }
    throw std::runtime_error("expected source error");
}
void benchmark(const std::string &manifest) {
    const auto started=std::chrono::steady_clock::now();
    auto sources=open_trace_manifest(manifest,4);
    const auto ended=std::chrono::steady_clock::now();
    require(sources.size()==4,"benchmark expects C4");
    std::cout << "{\"trace_open_seconds\":"
              << std::chrono::duration<double>(ended-started).count()
              << ",\"cores\":[";
    for(unsigned core=0;core<4;++core) {
        const auto l=sources[core]->functional_sync_layout();
        require(l && l->source_core==core,"benchmark source identity mismatch");
        if(core)std::cout << ',';
        const auto *coverage=sources[core]->core_functional_coverage();
        std::cout << "{\"core\":" << core << ",\"source_core\":" << l->source_core
                  << ",\"records\":" << l->records << ",\"warmup_records\":" << l->warmup_records
                  << ",\"measurement_records\":" << l->measurement_records
                  << ",\"core_companion_records\":" << (coverage?coverage->records:0) << '}';
    }
    std::cout << "]}\n";
}
}
int main(int argc,char **argv) {
    if(argc==3 && std::string(argv[1])=="--benchmark") {benchmark(argv[2]);return 0;}
    const auto dir=std::filesystem::path("tmp")/("parallel-trace-open-"+std::to_string(getpid()));
    std::filesystem::create_directories(dir);
    const auto manifest=(dir/"manifest.txt").string();
    try {
        for(unsigned core=0;core<4;++core)write_trace((dir/(std::to_string(core)+".fst")).string(),core);
        { std::ofstream out(manifest);for(int core=3;core>=0;--core)
            out << core << " binary " << std::filesystem::absolute(dir/(std::to_string(core)+".fst")).string() << '\n'; }
        set_workers("1");const auto serial=read_all(manifest);
        set_workers("4");require(read_all(manifest)==serial,"parallel contents differ");
        // Two different simultaneous failures: the earlier core's error must
        // be the one reported regardless of completion order.
        { auto f=gzopen((dir/"1.fst.ffe.gz").c_str(),"wb");require(f,"cannot corrupt companion");
          const char bad[]="bad";require(gzwrite(f,bad,sizeof(bad))==int(sizeof(bad)),"write failed");gzclose(f); }
        { std::fstream f(dir/"3.fst",std::ios::in|std::ios::out|std::ios::binary);f.put('X'); }
        set_workers("1");const auto serial_error=failure(manifest);
        set_workers("4");require(failure(manifest)==serial_error,"nondeterministic source error");
        set_workers("0");require(failure(manifest).find("FASTSIM_TRACE_OPEN_WORKERS")!=std::string::npos,
                                   "invalid worker count was accepted");
        unsetenv("FASTSIM_TRACE_OPEN_WORKERS");
        std::filesystem::remove_all(dir);
        std::cout << "parallel trace open checks passed\n";
    } catch (...) {unsetenv("FASTSIM_TRACE_OPEN_WORKERS");std::filesystem::remove_all(dir);throw;}
}
