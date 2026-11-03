#include "fastsim/config.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/dram_refresh.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <unistd.h>

namespace {
void require(bool value,const char* message) {
    if (!value) throw std::runtime_error(message);
}
}

void test_dram_refresh() {
    const auto path=std::filesystem::path("tmp")/("dram-refresh-unit-"+std::to_string(::getpid())+".cfg");
    std::filesystem::create_directories(path.parent_path());
    std::ofstream(path) << "dram.channels=1\ndram.ranks_per_channel=2\n"
        "dram.banks_per_channel=4\ndram.bank_groups_per_rank=1\n"
        "dram.t_cl=4\ndram.t_rcd=4\ndram.t_rp=4\ndram.t_ras=8\n"
        "dram.t_rtp=2\ndram.burst_cycles=2\ndram.frontend_latency=1\n"
        "dram.backend_latency=1\ndram.scheduler=frfcfs\n"
        "dram.frfcfs_causal_selection=true\ndram.refresh_enabled=true\n"
        "dram.t_refi_ticks=100\ndram.t_rfc_ticks=20\n"
        "dram.refresh_ticks_per_cycle=1\ndram.refresh_start_tick=0\n";
    const auto config=fastsim::load_simulator_config(path.string());
    const auto r=fastsim::testing::run_dram_schedule_probe(config.dram,64,
        {{10,0,0},{100,1,1},{150,2,2}},64);
    require(r.command_cycles[1]==124,
        "refresh must close the row and block issue until REF end plus tRCD");
    require(r.row_hits[1]==0 && r.row_hits[2]==1,
        "refresh invalidates the old row; later columns reopen and hit normally");
    auto off=config.dram;off.refresh_enabled=false;
    const auto legacy=fastsim::testing::run_dram_schedule_probe(off,64,
        {{10,0,0},{100,1,1},{150,2,2}},64);
    require(legacy.command_cycles[1]==100 && legacy.row_hits[1],"disabled option preserves legacy row hit");
    const auto parallel=fastsim::testing::run_dram_schedule_probe(config.dram,64,
        {{10,0,0},{100,1,1},{100,512,2}},64);
    require(parallel.service_order==std::vector<std::size_t>({0,2,1}),
        "rank with earlier refresh end must run before the blocked open rank");
    auto fractional=config.dram;fractional.refresh_ticks_per_cycle=3;
    const auto f=fastsim::testing::run_dram_schedule_probe(fractional,64,
        {{10,0,0},{30,1,1}},64);
    require(f.command_cycles[1]==44,"fractional preparation retains exact ticks without phase drift");
    fastsim::DramRankRefresh calendar;calendar.initialize(config.dram);
    require(calendar.advance(config.dram,1000000000,0,0,false),"long idle gap crosses refresh windows");
    require(calendar.preparations==10416666 && calendar.next_prepare==1000000032 &&
        calendar.available==999999956 && calendar.precharged_ranks==0,
        "idle skip preserves every preparation and adds no unconditional PRE");
    auto copied=calendar;
    calendar.advance(config.dram,1000001000,0,0,false);
    copied.advance(config.dram,1000001000,0,0,false);
    require(calendar.next_prepare==copied.next_prepare && calendar.available==copied.available,
        "rank snapshot retains phase across epoch copies");
    fastsim::DramRankRefresh observed;observed.initialize(config.dram);observed.reset_observation(1000);
    observed.advance(config.dram,1100,0,0,false);
    require(observed.preparations==1,"measurement reset excludes lazily processed warmup refreshes");
    auto invalid=config;invalid.dram.t_refi_ticks=0;
    bool rejected=false;try {invalid.validate();} catch(const std::invalid_argument&) {rejected=true;}
    require(rejected,"enabled refresh rejects missing physical timing identity");
    invalid=config;invalid.cross_q_service_mode="controller";
    invalid.core_model="interval_weave";invalid.interval_scheduler="time_epoch";
    invalid.response_queue_feedback=true;invalid.response_sparse_scoreboard=true;
    invalid.dram.refresh_enabled=false;invalid.validate();
    invalid.dram.refresh_enabled=true;
    rejected=false;try {invalid.validate();} catch(const std::invalid_argument& e) {
        rejected=std::string(e.what()).find("compact DramModel")!=std::string::npos;
    }
    require(rejected,"mixed controller cannot silently ignore compact refresh");
    rejected=false;try {fastsim::refresh_multiply(~std::uint64_t{0},3);} catch(const std::overflow_error&) {rejected=true;}
    require(rejected,"tick conversion fails on overflow");
    std::vector<fastsim::testing::DramControllerProbeEvent> backlog;
    for (unsigned i=0;i<100;++i)backlog.push_back({10,i%16,false});
    backlog.push_back({100,0,false});
    const auto saturated=fastsim::testing::run_dram_controller_probe(config.dram,64,backlog);
    require(saturated.completions[99]>240,
        "canonical same-arrival backlog must advance refresh instead of reserving through it");
    backlog.clear();
    for (unsigned i=0;i<130;++i)backlog.push_back({10,i%16,true});
    backlog.push_back({100,0,false});
    const auto written=fastsim::testing::run_dram_controller_probe(config.dram,64,backlog);
    require(written.writes_drained>0 && written.completions.back()>100,
        "queued write drain must preserve refresh progress without a false overrun");
    std::filesystem::remove(path);
}

#ifdef FASTSIM_DRAM_REFRESH_STANDALONE
int main() {
    try { test_dram_refresh(); std::cout<<"DRAM refresh tests passed\n"; }
    catch(const std::exception& e) { std::cerr<<e.what()<<'\n';return 1; }
}
#endif
