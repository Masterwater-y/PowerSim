#include "fastsim/trace.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {
void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
int wait_writer(const std::filesystem::path& path) {
    for (unsigned i=0;i<500;++i) {
        const int fd=open(path.c_str(), O_WRONLY|O_NONBLOCK);
        if (fd>=0) return fd;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return -1;
}
}

int main() {
    const auto dir=std::filesystem::path("tmp") /
        ("parallel-manifest-test-"+std::to_string(getpid()));
    std::filesystem::create_directories(dir);
    try {
        const auto manifest=dir/"manifest";
        for (unsigned core=0;core<4;++core) {
            fastsim::BinaryTraceWriter writer((dir/("core"+std::to_string(core))).string(),core+4);
            writer.enable_frontend_facts();
            writer.set_address_space_id(0x1000);
            for (unsigned n=0;n<3;++n) {
                fastsim::TraceRecord r; r.pc=0x1000+core*16+n;
                fastsim::fst::RetiredFrontendMacro m;
                m.begin_record=n; m.end_record=n+1; m.observed_uops=1;
                m.pc=r.pc; m.address_space_id=0x1000;
                m.code_identity=100+n; m.semantic_identity=200+n;
                m.known_flags=fastsim::fst::kFrontendRequiredKnown;
                m.flags=fastsim::fst::kFrontendComplete|fastsim::fst::kFrontendStartsMacro|
                        fastsim::fst::kFrontendDsbEligible;
                m.instruction_size=1; m.bytes[0]=0x90;
                writer.register_frontend_macro(m);
                writer.append(r);
            }
            writer.close();
        }
        {
            std::ofstream out(manifest);
            for (int core=3;core>=0;--core)
                out<<core<<" fastsim-binary-score-range-v1 "
                   <<std::filesystem::absolute(dir/("core"+std::to_string(core))).string()
                   <<' '<<core+4<<" 1 2 3\n";
        }
        auto sources=fastsim::open_trace_manifest(manifest.string(),4);
        require(sources.size()==4,"missing manifest source");
        for (unsigned core=0;core<4;++core) {
            fastsim::TraceRecord record;
            require(sources[core]->next(record) && record.pc==0x1000+core*16 &&
                    sources[core]->score_begin_reached() &&
                    sources[core]->next(record) && sources[core]->score_boundary_reached() &&
                    sources[core]->next(record) && !sources[core]->next(record),
                    "sorted source, remap or score bounds changed");
        }
        {
            std::ofstream bad(dir/"core3.ffe.gz",std::ios::binary|std::ios::trunc);
            bad<<"corrupt";
        }
        bool corrupt_rejected=false;
        try { (void)fastsim::open_trace_manifest(manifest.string(),4); }
        catch (const std::exception&) { corrupt_rejected=true; }
        require(corrupt_rejected,"parallel open must retain frontend companion validation");
        {
            std::ofstream out(manifest);
            out<<"0 fastsim-binary "<<(dir/"missing-first").string()<<"\n"
               <<"1 fastsim-binary "<<(dir/"missing-second").string()<<"\n";
        }
        bool first_error=false;
        try { (void)fastsim::open_trace_manifest(manifest.string(),4); }
        catch (const std::exception& e) {
            first_error=std::string(e.what()).find("missing-first")!=std::string::npos;
        }
        require(first_error,"source errors must propagate in core order");

        const auto a=dir/"fifo0", b=dir/"fifo1";
        require(mkfifo(a.c_str(),0600)==0 && mkfifo(b.c_str(),0600)==0,"mkfifo failed");
        {
            std::ofstream out(manifest);
            out<<"0 gem5-jsonl "<<std::filesystem::absolute(a).string()<<"\n"
               <<"1 gem5-jsonl "<<std::filesystem::absolute(b).string()<<"\n";
        }
        auto pending=std::async(std::launch::async,[&]{
            return fastsim::open_trace_manifest(manifest.string(),2);
        });
        // The second reader must already be open while the first reader is
        // still blocked waiting for its writer: a sequential open cannot pass.
        const int second=wait_writer(b);
        const int first=wait_writer(a);
        int released_second=second;
        if (second<0) released_second=wait_writer(b);
        auto parallel_sources=pending.get();
        if (first>=0) close(first);
        if (released_second>=0) close(released_second);
        require(second>=0 && first>=0 && parallel_sources.size()==2,
                "manifest sources were not opened concurrently");
        std::filesystem::remove_all(dir);
        std::cout<<"parallel manifest tests passed\n";
    } catch (const std::exception& e) {
        std::filesystem::remove_all(dir);
        std::cerr<<e.what()<<'\n';return 1;
    }
}
