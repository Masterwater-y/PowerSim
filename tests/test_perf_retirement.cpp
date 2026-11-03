#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <type_traits>

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
template<class T, class = void> struct has_retirement : std::false_type {};
template<class T> struct has_retirement<T, std::void_t<decltype(T{}.perf_retirement)>> : std::true_type {};
fastsim::TraceRecord memory(unsigned kind, std::uint64_t address=0x8000) {
    fastsim::TraceRecord r;
    r.pc=4096; r.address=address; r.size=8;
    r.flags=fastsim::kRetires|fastsim::kPhysicalAddress;
    r.flags=static_cast<std::uint16_t>(r.flags|kind);
    return r;
}
fastsim::SimulatorConfig config() {
    fastsim::SimulatorConfig c;
    c.cores=1; c.cha_count=1; c.core_model="interval_weave";
    c.interval_scheduler="time_epoch"; c.chunk_instructions=2;
    c.interval_target_uops=2; c.syscall_restart_latency=0;
    c.response_queue_feedback=true; c.response_sparse_scoreboard=true;
    return c;
}
fastsim::SimulationStats run(const std::vector<fastsim::TraceRecord>& records,
                            fastsim::SimulatorConfig cfg=config(), bool warmup=false) {
    const auto dir=std::filesystem::current_path()/"tmp/perf-pmu-repair-20261014/task1/runtime";
    std::filesystem::create_directories(dir);
    const auto fst=dir/"core0.fst";
    {
        fastsim::BinaryTraceWriter writer(fst.string(),0);
        writer.set_address_space_id(1);
        for(const auto& record:records) writer.append(record);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> sources;
    const auto warmup_macros=(!fastsim::has_flag(records[0].flags,fastsim::kMicroOp) ||
        fastsim::has_flag(records[0].flags,fastsim::kLastMicroOp))?1u:0u;
    if(warmup) sources.push_back(std::make_unique<fastsim::WarmupInstructionTraceSource>(
        std::make_unique<fastsim::BinaryTraceSource>(fst.string()),warmup_macros,records.size()-1,1,records.size()-1,true));
    else sources.push_back(std::make_unique<fastsim::BinaryTraceSource>(fst.string()));
    fastsim::Simulator simulator(cfg,std::move(sources));
    return simulator.run();
}
template<class C> void cases() {
    require(has_retirement<C>::value,"accepted architectural perf retirement ledger missing");
    if constexpr(has_retirement<C>::value) {
        // A line classifier would count this split instruction twice.
        auto split=memory(fastsim::kLoad,0x803c);
        const C a=run({split}).cores[0];
        require(a.perf_retirement.physical.perf_retired_loads==1,"split load is one architectural load");
        require(a.perf_retirement.physical.perf_l1d_load_misses==1,"split cold load has one joined miss");
        require(a.perf_retirement.physical.l2_miss_qualified==1 &&
                a.perf_retirement.physical.l3_miss_qualified==1,
                "accepted split cold service retires one L2/L3 miss per macro");
        require(a.perf_retirement.physical.l2_miss_unknown==0 &&
                a.perf_retirement.physical.l3_miss_unknown==0,
                "direct cold-service descriptor supplies complete lower-level facts");
        require(a.memory_accesses==2,"split fixture really issues two data fragments");
        require(a.perf_retirement.physical.estimated_l1_misses==1 &&
                a.perf_retirement.physical.estimated_l2_misses==1 &&
                a.perf_retirement.physical.estimated_l3_misses==1,
                "three miss point estimates count a cold split macro once");
        require(a.perf_cache.physical.data_reads==2 && a.perf_cache.physical.l2_demand_misses==2,
                "v2 actual fragment L2 lookups count once, independently of macro retirement");
        require(a.perf_cache.user.data_reads==0 && a.perf_cache.physical.unknown_context_requests==2,
                "missing event-clock context does not silently borrow originating user scope");
        const C mixed_source=run({memory(fastsim::kLoad),split}).cores[0];
        require(mixed_source.perf_retirement.physical.perf_l1d_load_misses==2 &&
                mixed_source.perf_retirement.physical.mixed_source_loads==1,
                "one hot and one cold fragment join to one split miss");
        require(mixed_source.perf_retirement.physical.l1_hit_qualified==1 &&
                mixed_source.perf_retirement.physical.l1_miss_qualified==2,
                "mixed-source macro independently qualifies L1 hit and miss events");
        auto escape_cfg=config(); escape_cfg.allow_mmio_escape=true;
        const C escape=run({memory(fastsim::kLoad,escape_cfg.dram.size_bytes-4)},escape_cfg).cores[0];
        require(escape.perf_retirement.physical.perf_retired_loads==1 &&
                escape.perf_retirement.physical.unclassified_loads==1 &&
                escape.perf_retirement.physical.classified_loads==0,
                "partial MMIO fragment must leave whole architectural source unclassified");
        // CMPS-like double read, with a non-memory last UOP in the next chunk.
        auto first=memory(fastsim::kLoad); first.flags|=fastsim::kMicroOp;
        auto second=first; second.address+=64;
        fastsim::TraceRecord last; last.pc=4096; last.flags|=fastsim::kMicroOp|fastsim::kLastMicroOp;
        const C b=run({first,second,last}).cores[0];
        require(b.perf_retirement.physical.perf_retired_loads==1,"multi-UOP cross-chunk macro counts once");
        require(b.perf_retirement.physical.retired_macro_instructions==1,"denominator follows completed macro");
        require(b.perf_retirement.physical.retired_uops==3,"all accepted UOPs accounted independently");
        require(b.perf_retirement.physical.incomplete_macros==0,"cross-chunk macro closes");
        const C spanning=run({first,last},config(),true).cores[0];
        require(spanning.perf_retirement.physical.perf_retired_loads==1 &&
                spanning.perf_retirement.physical.retired_macro_instructions==1 &&
                spanning.perf_retirement.physical.retired_uops==1 &&
                spanning.perf_retirement.physical.warmup_spanning_macros==1,
                "warmup member contributes to macro retiring inside measurement, not UOP denominator");
        auto cfg=config(); cfg.needs_tso=true; cfg.tso_store=fastsim::TsoStoreMode::kBuffer;
        cfg.tso_store_buffer_size=8; cfg.buffer_tso_prepare_lifecycle=false;
        const C s=run({memory(fastsim::kStore)},cfg).cores[0];
        require(s.perf_retirement.physical.perf_retired_stores==1,"buffered store retirement is independent of publication");
        const C ss=run({memory(fastsim::kStore,0x803c)},cfg).cores[0];
        require(ss.perf_retirement.physical.perf_retired_stores==1,"split store counts once");
        const C atomic=run({memory(fastsim::kAtomic)}).cores[0];
        require(atomic.perf_retirement.physical.perf_retired_loads==1 &&
                atomic.perf_retirement.physical.perf_retired_stores==1,"atomic has independent read and write qualification");
        const C incomplete=run({first,second}).cores[0];
        require(incomplete.perf_retirement.physical.retired_macro_instructions==0 &&
                incomplete.perf_retirement.physical.perf_retired_loads==0 &&
                incomplete.perf_retirement.physical.incomplete_macros==1,"incomplete tail is reported without padding retirement");
        cfg=config(); cfg.store_forwarding=true;
        const C forwarded=run({memory(fastsim::kStore),memory(fastsim::kLoad)},cfg).cores[0];
        require(forwarded.forwarded_loads==1,"forward fixture actually takes forwarding path");
        require(forwarded.perf_retirement.physical.estimated_l1_misses==0 &&
                forwarded.perf_retirement.physical.estimated_l2_misses==0 &&
                forwarded.perf_retirement.physical.estimated_l3_misses==0,
                "forwarding does not copy the store's cache misses");
        require(forwarded.perf_retirement.physical.perf_l1d_load_hits==1 &&
                forwarded.perf_retirement.physical.perf_l1d_load_fb_hits==0,"forwarding is an explicit scalar L1 estimate, never unconditional FB");
        require(forwarded.perf_retirement.physical.l1_hit_unknown==0 &&
                forwarded.perf_retirement.physical.l1_hit_qualified==1 &&
                forwarded.perf_retirement.physical.l1_miss_unknown==0 &&
                forwarded.perf_retirement.physical.l2_miss_unknown==0 &&
                forwarded.perf_retirement.physical.l3_miss_unknown==0 &&
                forwarded.perf_cache.physical.data_reads==0,
                "full forwarding proves local data and no retired cache miss without a request");
        cfg=config(); cfg.native_kernel_trace=true;
        cfg.measurement_scope=fastsim::MeasurementScope::kUserPlusKernel;
        auto kernel=memory(fastsim::kStore); kernel.set_kernel_mode(true);
        const C mixed=run({memory(fastsim::kLoad),kernel},cfg).cores[0];
        require(mixed.perf_retirement.user.retired_macro_instructions==1 &&
                mixed.perf_retirement.kernel.retired_macro_instructions==1 &&
                mixed.perf_retirement.kernel.perf_retired_stores==1,"CPL scopes have independent macro denominators");
        const auto plan=std::filesystem::current_path()/"tmp/perf-pmu-repair-20261014/task1/runtime/owner.plan";
        {
            std::ofstream out(plan);
            out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS 2 TASKS 1\n"
                "CORE 0 3 0 3 101\nTASK 101 101 1\n"
                "ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\n"
                "EVENT 0 0 0 4096 1 1 101 101 1 0\n"
                "EVENT 1 0 1 4096 6 1 101 303 1 1 0\n";
        }
        cfg=config(); cfg.chunk_instructions=16; cfg.interval_target_uops=16;
        cfg.thread_state_plan=plan.string(); cfg.native_kernel_trace=true;
        cfg.measurement_scope=fastsim::MeasurementScope::kUserPlusKernel;
        auto owned_kernel=memory(fastsim::kLoad); owned_kernel.set_kernel_mode(true);
        const C owned=run({owned_kernel,memory(fastsim::kStore),memory(fastsim::kLoad,0x9000)},cfg).cores[0];
        require(owned.perf_retirement.workload_task.retired_macro_instructions==2 &&
                owned.perf_retirement.other_task.retired_macro_instructions==1 &&
                owned.perf_retirement.workload_task.perf_retired_stores==1 &&
                owned.perf_retirement.other_task.perf_retired_loads==1,
                "switch anchor retires for previous owner, subsequent macro belongs to other task");
        cfg=config(); cfg.chunk_instructions=8; cfg.interval_target_uops=8;
        cfg.interval_max_cycles=1024; cfg.lookahead_chunks=2;
        cfg.ruby_sequencer_max_outstanding=16; cfg.ruby_sequencer_line_coalescing=true;
        const auto follower=run({memory(fastsim::kLoad),memory(fastsim::kLoad)},cfg);
        const C fc=follower.cores[0];
        require(follower.total_sequencer().coalesced_reads==1,"FB fixture actually coalesces into modeled in-flight fill");
        require(fc.perf_retirement.physical.perf_retired_loads==2 &&
                fc.perf_retirement.physical.perf_l1d_load_misses==1 &&
                fc.perf_retirement.physical.perf_l1d_load_fb_hits==1,
                "primary load and real modeled fill follower retain distinct macro source estimates");
        require(fc.perf_cache.physical.l2_demand_misses==1 &&
                fc.perf_cache.physical.data_reads==2 && fc.perf_cache.physical.llc_merges==0,
                "Sequencer follower cannot manufacture second L2 lookup or LLC merge");
        const auto hot_alias=run({memory(fastsim::kLoad),memory(fastsim::kLoad),memory(fastsim::kLoad)},cfg,true);
        const C hc=hot_alias.cores[0];
        require(hot_alias.total_sequencer().coalesced_l1_parents>0,
                "hot alias fixture must actually coalesce with an L1-hit parent");
        require(hc.perf_retirement.physical.perf_retired_loads==2 &&
                hc.perf_retirement.physical.perf_l1d_load_hits==2 &&
                hc.perf_retirement.physical.perf_l1d_load_fb_hits==0,
                "hot L1 alias is not an outstanding cache fill");
        cfg=config(); cfg.chunk_instructions=8; cfg.interval_target_uops=8;
        cfg.interval_private_preview=true; cfg.domain_min_events=1;
        cfg.l1d.size_bytes=64; cfg.l1d.associativity=1;
        cfg.l2.size_bytes=4096; cfg.l2.associativity=2;
        const auto preview=run({memory(fastsim::kLoad),memory(fastsim::kLoad,0x8040),memory(fastsim::kLoad)},cfg);
        const C pc=preview.cores[0];
        require(preview.private_preview_events>0 && pc.l2.hits>0,"private preview fixture really takes L2 hit");
        require(pc.perf_retirement.physical.estimated_miss_fallback_loads>0,
                "private-only synthesized request clock stays an explicit approximation");
        require(pc.perf_retirement.physical.perf_retired_loads==3 &&
                pc.perf_retirement.physical.perf_l1d_load_misses==3,
                "private preview classifies source independently of legacy classifier path");
        require(pc.perf_cache.physical.data_reads==3 && pc.perf_cache.physical.l2_accesses==3 &&
                pc.perf_cache.physical.l2_demand_misses==2,
                "private-only preview L2 hit counted once alongside shared escapes");
        // Forces real candidate retry / transaction rollback, same accepted units.
        cfg=config(); cfg.chunk_instructions=128; cfg.interval_target_uops=128;
        cfg.response_queue_feedback=true; cfg.response_sparse_scoreboard=true;
        cfg.response_block_summary=true; cfg.response_memory_descriptor=true;
        cfg.response_monotone_iq_calendar=true; cfg.response_activity_certificate=true;
        cfg.needs_tso=true; cfg.tso_store=fastsim::TsoStoreMode::kBuffer;
        cfg.tso_store_buffer_size=114; cfg.ruby_sequencer_max_outstanding=16;
        cfg.ruby_buffer_service_repair=true; cfg.ruby_buffer_service_local_fallback=true;
        cfg.ruby_buffer_service_work_percent=1000;
        std::vector<fastsim::TraceRecord> loads{memory(fastsim::kLoad),memory(fastsim::kLoad,0x8040),
            memory(fastsim::kLoad,0x8048),memory(fastsim::kLoad,0x8008)};
        loads[3].producer_dists[0]=3; loads[3].n_src=1;
        const auto retry=run(loads,cfg);
        cfg.ruby_buffer_service_max_passes=1;
        const auto fallback=run(loads,cfg);
        require(retry.buffer_service_passes>1 && fallback.buffer_service_local_broad_fallback_epochs>0,
                "rollback fixture actually retries and rolls back");
        const C r=retry.cores[0], f=fallback.cores[0];
        require(r.perf_retirement.physical.perf_retired_loads==4 &&
                f.perf_retirement.physical.perf_retired_loads==4,"trials never duplicate accepted retirement");
        require(r.perf_cache.physical.data_reads==4 && f.perf_cache.physical.data_reads==4 &&
                r.perf_cache.physical.l2_demand_misses==f.perf_cache.physical.l2_demand_misses,
                "rejected computational reweaves roll back actual request observations");
    }
}
}
int main() {
    try { cases<fastsim::CoreCounters>(); std::cout << "perf retirement tests passed\n"; }
    catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
