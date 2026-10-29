#include "fastsim/functional_sync.hpp"
#include "fastsim/trace.hpp"
#include "fastsim/interval_core.hpp"
#include "fastsim/simulator.hpp"
#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace {
void check(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
}
void test_functional_sync_runtime() {
    const auto path = std::filesystem::current_path()/"tmp/functional-sync-consumer/test.plan";
    std::filesystem::create_directories(path.parent_path());
    { std::ofstream out(path); out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 3\n"
        "CORE 0 2 0 2\nCORE 1 1 0 1\n"
        "EVENT 0 0 0 4096 1 5 1 0\n"
        "EVENT 1 0 1 4096 1 6 1 2 0 2\n"
        "EVENT 2 1 0 4096 1 5 1 0\n"; }
    fastsim::FunctionalSyncPlan plan(path.string(),2);
    check(plan.event_count()==3,"sync population");
    plan.observe(0,100);
    check(plan.can_release(0),"arrival nonblocking");
    plan.release(0);
    plan.observe(1,120);
    check(!plan.can_release(1),"departure blocked until remote arrival");
    plan.observe(2,400); plan.release(2);
    check(plan.can_release(1),"departure released after remote arrival");
    check(plan.predecessor_ready(1)==400,"actual remote readiness");
    check(plan.release(1)==400,"release max frontier");
    check(plan.all_released(),"all events acknowledged");
    fastsim::SimulatorConfig config;
    config.core_model="interval_weave";
    fastsim::IntervalCoreModel model(config);
    model.set_frontend_floor(1000);
    fastsim::TraceRecord record; record.pc=4096; record.flags=fastsim::kRetires;
    auto timing=model.schedule(record,false);
    check(timing.fetch_cycle>=1000 && timing.issue_cycle>=1000,"frontend floor prevents early issue");
    { std::ofstream out(path); out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 2\n"
        "CORE 0 1 0 1\nCORE 1 1 0 1\n"
        "EVENT 0 0 0 4096 1 6 1 1 1\nEVENT 1 1 0 4096 1 6 1 1 0\n"; }
    bool rejected=false;
    try { fastsim::FunctionalSyncPlan bad(path.string(),2); } catch(const std::exception&) { rejected=true; }
    check(rejected,"cycle must reject");
    // Two cores execute memory runtime between arrival and departure. Remote
    // work is long enough to expose an ordering wait; arrival must not park it.
    const auto fst0=path.parent_path()/"sync-test-core0.fst";
    const auto fst1=path.parent_path()/"sync-test-core1.fst";
    for(std::uint32_t core=0;core<2;++core) {
        fastsim::BinaryTraceWriter writer((core==0?fst0:fst1).string(),core);
        writer.set_address_space_id(1);
        for(std::uint64_t i=0;i<(core==0?4u:104u);++i) {
            fastsim::TraceRecord r;
            r.pc=4096; r.flags=fastsim::kRetires;
            const bool marker = core==0 ? (i==0 || i==2) : (i==100 || i==102);
            if(!marker) { r.pc=8192; r.flags|=fastsim::kLoad|fastsim::kPhysicalAddress; r.address=0x80000+core*0x100000+i*4096; r.size=8; }
            writer.append(r);
        }
    }
    { std::ofstream out(path); out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 4\n"
        "CORE 0 4 0 4\nCORE 1 104 0 104\n"
        "EVENT 0 0 0 4096 1 5 1 0\nEVENT 1 0 2 4096 1 6 1 2 0 2\n"
        "EVENT 2 1 100 4096 1 5 1 0\nEVENT 3 1 102 4096 1 6 1 3 0 1 2\n"; }
    const auto run=[&](bool enabled, bool warmup=false, bool heterogeneous=false, bool feedback=false) {
        auto cfg=config;
        cfg.cores=2; cfg.cha_count=1; cfg.interval_scheduler="time_epoch";
        cfg.chunk_instructions=7; cfg.interval_target_uops=7; cfg.interval_max_cycles=64;
        cfg.functional_sync_plan=enabled ? path.string() : "";
        if(feedback) {
            cfg.response_queue_feedback=true;
            cfg.response_sparse_scoreboard=true;

        }
        if(heterogeneous) cfg.core_frequencies_hz={1500000000,3000000000};
        std::vector<std::unique_ptr<fastsim::TraceSource>> sources;
        for(const auto& fst:{fst0,fst1}) {
            auto source=std::make_unique<fastsim::BinaryTraceSource>(fst.string());
            if(warmup) {
                const auto n=source->record_count();
                sources.push_back(std::make_unique<fastsim::WarmupInstructionTraceSource>(
                    std::move(source),n-1,1,n-1,1,true));
            } else sources.push_back(std::move(source));
        }
        fastsim::Simulator sim(cfg,std::move(sources));
        return sim.run();
    };
    const auto off=run(false), on=run(true);
    check(!off.functional_sync_enabled && on.functional_sync_enabled,"sync explicit opt-in");
    check(on.functional_sync_released_events==4,"all runtime markers released");
    check(on.functional_sync_dependency_waits>0,"remote delay exposes barrier wait");
    check(on.cores[0].retired_uops==4 && on.cores[1].retired_uops==104,"no synthetic or lost UOPs");
    check(on.cores[0].cycles>off.cores[0].cycles,"post barrier work is delayed");
    check(on.cores[1].memory_accesses==off.cores[1].memory_accesses,"runtime memory continues");
    for(const auto& a:on.functional_sync_audit) {
        if(a.first_post_boundary_memory_issue_reference_q16)
            check(*a.first_post_boundary_memory_issue_reference_q16>=a.admission_reference_q16,"new work cannot enter already closed marker time window");
    }
    bool checked_post_memory=false;
    for(const auto& a:on.functional_sync_audit) {
        if(a.kind==6 && a.core==0) {
            check(a.first_post_boundary_memory_issue_reference_q16.has_value(),"post-departure memory is audited");
            check(*a.first_post_boundary_memory_issue_reference_q16>=a.predecessor_ready_reference_q16,"post-departure memory follows all arrivals");
            checked_post_memory=true;
        }
    }
    check(checked_post_memory,"departure boundary audit present");
    const auto different_clocks=run(true,false,true);
    check(different_clocks.functional_sync_released_events==4,"unequal clocks release all events");
    check(different_clocks.functional_sync_dependency_waits>0,"reference-time waits with unequal clocks");
    const auto with_feedback=run(true,false,false,true);
    check(with_feedback.total_core().memory_penalty_cycles>0,"test exercises nonzero response feedback");
    for(const auto& a:with_feedback.functional_sync_audit) {
        if(a.first_post_boundary_memory_issue_reference_q16)
            check(*a.first_post_boundary_memory_issue_reference_q16>=a.predecessor_ready_reference_q16,"feedback-corrected boundary ordering");
    }
    // Exact ordinal delegation, including a wrapper which rejects sync layout.
    {
        auto src=std::make_unique<fastsim::BinaryTraceSource>(fst0.string());
        fastsim::InstructionSliceTraceSource slice(std::move(src),1,1);
        fastsim::TraceRecord r;
        check(slice.next(r) && *slice.current_source_record_ordinal()==1,"slice preserves raw ordinal");
        check(!slice.functional_sync_layout(),"prefix slices reject full-context sync");
    }
    // Both departures are terminal records: ack must precede finished.
    for(std::uint32_t core=0;core<2;++core) {
        fastsim::BinaryTraceWriter writer((core==0?fst0:fst1).string(),core);
        writer.set_address_space_id(1);
        const auto n=core==0?4u:104u;
        for(std::uint32_t j=0;j<n;++j) {
            fastsim::TraceRecord r; r.pc=4096; r.flags=fastsim::kRetires;
            writer.append(r);
        }
    }
    { std::ofstream out(path); out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 4\n"
        "CORE 0 4 0 4\nCORE 1 104 0 104\n"
        "EVENT 0 0 0 4096 1 5 1 0\nEVENT 1 0 3 4096 1 6 1 2 0 2\n"
        "EVENT 2 1 100 4096 1 5 1 0\nEVENT 3 1 103 4096 1 6 1 2 0 2\n"; }
    const auto terminal=run(true);
    check(terminal.functional_sync_total_released_events==4,"terminal measurement markers fully acknowledged");
    // Marker at the final warmup record must be acknowledged before phase end.
    for(std::uint32_t core=0;core<2;++core) {
        fastsim::BinaryTraceWriter writer((core==0?fst0:fst1).string(),core);
        writer.set_address_space_id(1);
        const auto n=core==0?4u:104u;
        for(std::uint32_t i=0;i<n;++i) {
            fastsim::TraceRecord r; r.pc=i==n-2 ? 4096 : 8192; r.flags=fastsim::kRetires;
            writer.append(r);
        }
    }
    { std::ofstream out(path); out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 2\n"
        "CORE 0 4 3 1\nCORE 1 104 103 1\n"
        "EVENT 0 0 2 4096 1 1 0 0\nEVENT 1 1 102 4096 1 3 0 1 0\n"; }
    const auto seam=run(true,true);
    check(seam.functional_warmup_enabled,"warmup phase is completed");
    check(seam.cores[0].retired_uops==1 && seam.cores[1].retired_uops==1,"warmup marker ack preserves measurement population");
    check(seam.functional_sync_released_events==0,"warmup sync counters not charged to measurement");
    // Ordinary warmup suffix after the last marker also requires a seam floor.
    for(std::uint32_t core=0;core<2;++core) {
        fastsim::BinaryTraceWriter writer((core==0?fst0:fst1).string(),core);
        writer.set_address_space_id(1);
        const auto n=core==0?4u:104u;
        for(std::uint32_t j=0;j<n;++j) {
            fastsim::TraceRecord r; r.pc=j==n-3 ? 4096 : 8192; r.flags=fastsim::kRetires;
            if(j==n-1) { r.flags|=fastsim::kLoad|fastsim::kPhysicalAddress; r.address=0x80000+core*4096; r.size=8; }
            writer.append(r);
        }
    }
    { std::ofstream out(path); out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 2\n"
        "CORE 0 4 3 1\nCORE 1 104 103 1\n"
        "EVENT 0 0 1 4096 1 1 0 0\nEVENT 1 1 101 4096 1 3 0 1 0\n"; }
    const auto ordinary_suffix=run(true,true);
    check(ordinary_suffix.cores[0].memory_accesses==1 && ordinary_suffix.cores[1].memory_accesses==1,"ordinary warmup suffix and seam memory preserved");
    { std::ofstream out(path); out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 2\n"
        "CORE 0 4 3 1\nCORE 1 104 103 1\n"
        "EVENT 0 0 2 99 1 1 0 0\nEVENT 1 1 102 4096 1 3 0 1 0\n"; }
    bool bad_anchor=false;
    try { run(true,true); } catch(const std::runtime_error&) { bad_anchor=true; }
    check(bad_anchor,"incorrect anchor PC is rejected");
    for(const auto& fst:{fst0,fst1}) {
        std::filesystem::remove(fst); std::filesystem::remove(fst.string()+".asmap");
    }
    std::filesystem::remove(path);
}
