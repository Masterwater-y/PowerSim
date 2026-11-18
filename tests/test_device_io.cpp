#include "fastsim/device_io.hpp"
#include "test_spr_store_group_runtime.cpp"
#include <limits>

namespace {
SimulatorConfig device_config(bool enabled, unsigned cores = 1) {
    auto c = group_config(false, 4, 16);
    c.cores = cores;
    c.allow_mmio_escape = true;
    c.interval_max_cycles = 32;
    c.device_io.enabled = enabled;
    c.device_io.clock_period_ticks = 385;
    c.device_io.uart_latency_ticks = 100000;
    c.device_io.apic_latency_ticks = 100000;
    c.validate();
    return c;
}
SimulationStats io_run(const std::vector<std::vector<Step>>& streams, bool on,
                       bool certificate = true) {
    auto c = device_config(on, streams.size());
    c.response_activity_certificate = certificate;
    std::vector<std::unique_ptr<TraceSource>> traces;
    for (const auto& s : streams)
        traces.push_back(std::make_unique<FunctionalStoreSource>(s));
    return Simulator(c, std::move(traces)).run();
}
void classifier() {
    const auto profile = load_simulator_config("configs/spr-evaluation-default.cfg");
    check(profile.dtlb.functional_model && profile.dtlb.page_walkers == 4 &&
          profile.device_io.enabled && profile.device_io.clock_period_ticks == 385 &&
          profile.device_io.uart_address == 0x80000000000003f8ull &&
          profile.device_io.uart_size == 8 && profile.device_io.uart_latency_ticks == 100000 &&
          profile.device_io.apic_latency_ticks == 100000,
          "SPR default enables functional translation and platform-configured device service");
    auto c = device_config(true);
    check(device_io_service(c.device_io, 0x80000000000003f8ull, 1).cycles == 260,
          "physical device ticks must round up once to CPU cycles");
    check(device_io_service(c.device_io, 0x80000000000003ffull, 2).kind == DeviceIoKind::Unknown,
          "a split device-range request cannot be silently truncated");
    check(device_io_service(c.device_io, 0x20000000000010b0ull, 4).kind == DeviceIoKind::Apic,
          "APIC per-core address prefix must be recognized");
    check(device_io_service(c.device_io, 0x40000, 8).kind == DeviceIoKind::Unknown,
          "ordinary RAM is not a device");
    check(device_io_service(c.device_io, ~std::uint64_t{0}, 8).kind == DeviceIoKind::Unknown,
          "wrapped address range must be rejected");
    check(device_io_service(c.device_io, 0x20000000001000b0ull, 4).kind == DeviceIoKind::Unknown,
          "APIC identifier must fit the functional uint8 encoding");
    c.device_io.enabled = false;
    check(!device_io_service(c.device_io, 0x80000000000003f8ull, 1).cycles,
          "disabled path is neutral");
}
void atomic_device() {
    const auto r = io_run({{{Kind::kAtomic,0x80000000000003f8ull}}},true);
    check(r.total_core().device_io_uart_requests == 0 &&
          r.total_core().device_io_unmodeled_requests == 1,
          "unsupported atomic I/O must remain explicitly unmodeled, without two services");
}
void ram_overlap() {
    auto c=device_config(true);c.device_io.uart_address=0x40000;
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(
        std::vector<Step>{{Kind::kLoad,0x40000}}));
    const auto r=Simulator(c,std::move(traces)).run();
    check(r.total_core().device_io_uart_requests==0 && r.total_core().l1d.accesses>0,
          "a RAM-mapped request cannot also receive device service");
}
void invalid_configs() {
    auto c = device_config(true,2);
    c.core_frequencies_hz = {c.core_frequency_hz,c.core_frequency_hz+1};
    bool rejected=false;
    try {c.validate();} catch(const std::invalid_argument&) {rejected=true;}
    check(rejected,"single tick-period device descriptor must reject heterogeneous core clocks");
    c=device_config(true);c.response_retire_exposure=0.5;rejected=false;
    try {c.validate();} catch(const std::invalid_argument&) {rejected=true;}
    check(rejected,"device read response cannot use fractional retirement exposure");
    c=device_config(true);c.device_io.clock_period_ticks=0;rejected=false;
    try {c.validate();} catch(const std::invalid_argument&) {rejected=true;}
    check(rejected,"missing source tick period must reject");
}
void causal_transfer() {
    auto run=[](bool enabled, bool device = true) {
        auto c=device_config(true);c.cpi_attribution=false;c.response_frontier_audit_stride_uops=0;
        c.tso_store=TsoStoreMode::kSerial;c.response_causal_block_transfer=enabled;
        c.chunk_instructions=c.interval_target_uops=64;c.interval_max_cycles=4096;
        c.response_memory_descriptor=true;c.response_block_summary=true;
        c.response_monotone_iq_calendar=true;
        c.minimum_load_latency=c.l1d.hit_latency+c.load_response_to_ready;c.validate();
        std::vector<Step> steps{{Kind::kLoad,0x40000}};
        for(unsigned i=0;i<30;++i)steps.push_back({Kind::kAlu,0});
        steps.back()={Kind::kFence,0};
        steps.push_back(device ? Step{Kind::kStore,0x80000000000003f8ull} : Step{Kind::kAlu,0});
        for(unsigned i=0;i<16;++i)steps.push_back({Kind::kLoad,0x40000});
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<WarmupInstructionTraceSource>(
            std::make_unique<FunctionalStoreSource>(steps),16,32));
        return Simulator(c,std::move(traces)).run();
    };
    const auto exact=run(false),fast=run(true);
    check(fast.cores[0].cycles==exact.cores[0].cycles,
          "causal transfer cannot bypass a prior device write's memory-order carry");
    const auto control=run(true,false),scalar_control=run(false,false);
    check(control.response_causal_block_transferred_uops >= 16 &&
          control.cores[0].cycles == scalar_control.cores[0].cycles,
          "control must exercise eligible block transfer without a device carry");
}
void behavioral() {
    const auto port = 0x80000000000003f8ull;
    const std::vector<Step> stream{{Kind::kLoad, port}, {Kind::kAlu,0,true},
                                 {Kind::kStore,port}, {Kind::kLoad,port},
                                 {Kind::kAlu,0,true}};
    const auto off = io_run({stream}, false), on = io_run({stream}, true);
    check(frame(on,0).actual_completion_cycle >= frame(on,0).actual_issue_cycle + 260,
          "device read must wait for its configured response");
    check(frame(on,1).actual_issue_cycle >= frame(on,0).actual_completion_cycle,
          "dependent ALU must observe device read completion");
    check(frame(on,2).store_sq_release_cycle >= frame(on,2).actual_retire_cycle + 260,
          "device write service must follow retirement and hold SQ credit");
    check(frame(on,2).actual_completion_cycle < frame(on,2).store_sq_release_cycle,
          "write execution completion must not be conflated with device response");
    check(frame(on,3).actual_issue_cycle >= frame(on,2).store_sq_release_cycle,
          "later strict device read must wait for previous write response across epochs");
    check(on.total_core().retired_uops == off.total_core().retired_uops &&
          on.total_sequencer().requests == off.total_sequencer().requests &&
          on.total_core().l1d.accesses == off.total_core().l1d.accesses,
          "device service preserves retired and ordinary cache request populations");
    const auto exact = io_run({stream},true,false);
    check(frame(exact,4).actual_retire_cycle == frame(on,4).actual_retire_cycle,
          "activity certificate must not bypass device service");
    const auto parallel = io_run({stream,stream,stream,stream},true);
    for(unsigned core=0;core<4;++core)
        check(frame(parallel,4,core).actual_retire_cycle == frame(on,4).actual_retire_cycle,
              "independent cores must not share a global device completion frontier");
}
}
int main(int argc,char** argv) {
    try {
        const std::string mode=argc>1?argv[1]:"all";
        if(mode=="all"||mode=="classifier")classifier();
        if(mode=="all"||mode=="atomic")atomic_device();
        if(mode=="all"||mode=="overlap")ram_overlap();
        if(mode=="all"||mode=="config")invalid_configs();
        if(mode=="all"||mode=="causal")causal_transfer();
        if(mode=="all"||mode=="behavioral")behavioral();
    }
    catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
    std::cout << "device I/O service tests passed\n";
}
