#include "fastsim/thread_state.hpp"
#include "fastsim/functional_sync.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace { void require(bool x,const char* message) { if(!x) throw std::runtime_error(message); } }
void test_thread_state_runtime() {
    // A blocked task leaves CPU0 free to run task202. Wake changes readiness,
    // and only a switch back changes ownership. No observed durations exist.
    fastsim::ThreadStateAccounting state({101,202},{101,202});
    state.begin_measurement(0);
    state.switch_owner(0,101,303,2,100);
    state.wakeup(101);
    state.switch_owner(0,303,101,1,400);
    state.finish_core(0,500);
    state.finish_core(1,500);
    require(state.active_q16()[0]==200,"sleep and runnable queue excluded from task active");
    require(state.other_q16()[0]==300,"other context kept on CPU timeline");
    require(state.active_q16()[1]==500,"unaffected running core progresses");
    require(state.owner(0)==101,"wakeup alone did not transfer ownership");
    fastsim::ThreadStateAccounting eagain({101},{101});
    eagain.begin_measurement(0);
    eagain.finish_core(0,200);
    require(eagain.active_q16()[0]==200,"EAGAIN and spin without switch stay active");
    fastsim::ThreadStateAccounting preempt({101},{101});
    preempt.switch_owner(0,101,303,1,50);
    preempt.begin_measurement(100);
    preempt.switch_owner(0,303,101,1,200);
    preempt.finish_core(0,300);
    require(preempt.active_q16()[0]==100 && preempt.other_q16()[0]==100,"warmup ownership survives common start");
    bool rejected=false;
    try { preempt.switch_owner(0,404,101,1,350); } catch(const std::exception&) { rejected=true; }
    require(rejected,"mismatched owner must fail closed");
    fastsim::ThreadStateAccounting lost({101},{101});
    lost.switch_owner(0,101,303,2,10);
    rejected=false;
    try { lost.switch_owner(0,303,101,1,20); } catch(const std::exception&) { rejected=true; }
    require(rejected,"blocked task cannot resume without wake evidence");
    rejected=false;
    try { fastsim::ThreadStateAccounting duplicated({101,101},{101}); } catch(const std::exception&) { rejected=true; }
    require(rejected,"duplicate initial CPU ownership rejects");
    rejected=false;
    try { state.switch_owner(0,101,202,1,600); } catch(const std::exception&) { rejected=true; }
    require(rejected,"duplicate live CPU ownership rejects");
    const auto dir=std::filesystem::current_path()/"tmp/thread-state-active-elapsed/runtime-test";
    std::filesystem::create_directories(dir);
    const auto fst=dir/"core0.fst",path=dir/"state.plan";
    {
        fastsim::BinaryTraceWriter writer(fst.string(),0);
        writer.set_address_space_id(1);
        for(unsigned i=0;i<9;++i) {
            fastsim::TraceRecord r;r.pc=4096+i*4;
            r.set_kernel_mode(i>=2 && i<=6);
            if(i==3 || i==5 || i==8) { r.flags|=fastsim::kLoad|fastsim::kPhysicalAddress;r.address=0x80000+i*4096;r.size=8; }
            writer.append(r);
        }
        std::ofstream out(path);
        out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS 6 TASKS 1\nCORE 0 9 0 9 101\nTASK 101 101 1\n"
            "EVENT 0 0 0 4096 1 1 101 101 1 0\n"
            "EVENT 1 0 1 4100 2 1 101 1 549755813888 1 0\n"
            "EVENT 2 0 2 4104 6 1 101 303 2 1 1\n"
            "EVENT 3 0 4 4112 7 1 101 1 0 2 1 2\n"
            "EVENT 4 0 6 4120 6 1 303 101 1 1 3\n"
            "EVENT 5 0 7 4124 3 1 101 1 0 2 1 4\n";
    }
    fastsim::SimulatorConfig config;config.cores=1;config.cha_count=1;
    {
        std::ifstream input(path);const std::string valid((std::istreambuf_iterator<char>(input)),{});
        const auto reject_edit=[&](const std::string& before,const std::string& after) {
            auto bad=valid;const auto at=bad.find(before);require(at!=std::string::npos,"invalid-plan test edit exists");
            bad.replace(at,before.size(),after);
            { std::ofstream output(path);output << bad; }
            bool invalid=false;try { fastsim::FunctionalSyncPlan p(path.string(),1); } catch(const std::exception&) { invalid=true; }
            require(invalid,"invalid task tuple/request plan must reject");
        };
        reject_edit("1 1 101 101 1 0","1 1 101 1 101 0");
        reject_edit("2 1 101 1 549755813888","2 1 101 0 549755813888");
        reject_edit("3 1 101 1 0 2 1 4","3 1 101 2 0 2 1 4");
        reject_edit("3 1 101 1 0 2 1 4","3 1 101 1 0 1 4");
        { std::ofstream output(path);output << valid; }
    }
    config.core_model="interval_weave";config.interval_scheduler="time_epoch";
    config.native_kernel_trace=true;config.measurement_scope=fastsim::MeasurementScope::kUserPlusKernel;
    config.syscall_restart_latency=0;
    config.thread_state_plan=path.string();config.chunk_instructions=7;config.interval_target_uops=7;config.interval_max_cycles=64;
    std::vector<std::unique_ptr<fastsim::TraceSource>> sources;
    sources.push_back(std::make_unique<fastsim::BinaryTraceSource>(fst.string()));
    fastsim::Simulator sim(config,std::move(sources));const auto result=sim.run();
    require(result.thread_state_enabled && !result.functional_sync_enabled,"separate state opt-in");
    require(result.functional_sync_total_released_events==6,"all state boundaries acknowledged");
    require(result.thread_state_task_macro_instructions==5,"task numerator uses matched task macro denominator");
    require(result.cores[0].retired_instructions==9,"scheduler and other context records execute");
    require(result.thread_state_active_reference_q16[0]>0 && result.thread_state_other_reference_q16[0]>0,"both ownership populations activated");
    require(result.thread_state_active_reference_q16[0]+result.thread_state_other_reference_q16[0]==result.elapsed_reference_q16,"owner time conservation");
    {
        const auto fst1=dir/"core1.fst";
        { fastsim::BinaryTraceWriter writer(fst1.string(),1);writer.set_address_space_id(1);
          for(unsigned i=0;i<20;++i) { fastsim::TraceRecord r;r.pc=8192+i*4;r.set_kernel_mode(i==1);writer.append(r); } }
        std::ifstream input(path);const std::string single((std::istreambuf_iterator<char>(input)),{});
        auto multiple=single;
        auto at=multiple.find("CORES 1 EVENTS 6 TASKS 1");multiple.replace(at,24,"CORES 2 EVENTS 7 TASKS 2");
        at=multiple.find("TASK 101 101 1");multiple.insert(at,"CORE 1 20 0 20 202\n");
        at=multiple.find("EVENT 0");multiple.insert(at,"TASK 202 202 2\n");
        multiple+="EVENT 6 1 0 8192 1 1 202 202 2 0\n";
        { std::ofstream output(path);output << multiple; }
        auto paired_config=config;paired_config.cores=2;
        std::vector<std::unique_ptr<fastsim::TraceSource>> paired;
        paired.push_back(std::make_unique<fastsim::BinaryTraceSource>(fst.string()));
        paired.push_back(std::make_unique<fastsim::BinaryTraceSource>(fst1.string()));
        fastsim::Simulator paired_sim(paired_config,std::move(paired));const auto measured=paired_sim.run();
        std::uint64_t unknown=0;
        for(std::size_t core=0;core<2;++core) {
            unknown+=measured.thread_state_unknown_reference_q16[core];
            require(measured.thread_state_active_reference_q16[core]+measured.thread_state_other_reference_q16[core]+measured.thread_state_unknown_reference_q16[core]==measured.elapsed_reference_q16,"common elapsed partitions without extrapolated owner");
        }
        require(unknown>0,"unequal core completions retain unknown common tail");
        { std::ofstream output(path);output << single; }
    }
    {
        std::ifstream input(path);std::string warm((std::istreambuf_iterator<char>(input)),{});
        const auto full=warm;
        const auto edit=[&](const std::string& before,const std::string& after) { const auto at=warm.find(before);require(at!=std::string::npos,"warmup plan edit exists");warm.replace(at,before.size(),after); };
        edit("CORE 0 9 0 9 101","CORE 0 9 4 5 101");
        edit("EVENT 0 0 0 4096 1 1","EVENT 0 0 0 4096 1 0");
        edit("EVENT 1 0 1 4100 2 1","EVENT 1 0 1 4100 2 0");
        edit("EVENT 2 0 2 4104 6 1","EVENT 2 0 2 4104 6 0");
        { std::ofstream output(path);output << warm; }
        std::vector<std::unique_ptr<fastsim::TraceSource>> warmed;
        warmed.push_back(std::make_unique<fastsim::WarmupInstructionTraceSource>(std::make_unique<fastsim::BinaryTraceSource>(fst.string()),4,5,4,5,true));
        fastsim::Simulator warm_sim(config,std::move(warmed));const auto measured=warm_sim.run();
        require(measured.thread_state_task_macro_instructions==2,"warmup macros excluded while owner state preserved");
        require(measured.functional_sync_total_released_events==6 && measured.functional_sync_released_events==3,"phase event conservation");
        require(measured.thread_state_total_released_events==6,"thread-state totals include warmup controls after measurement reset");
        require(measured.thread_state_active_reference_q16[0]+measured.thread_state_other_reference_q16[0]==measured.elapsed_reference_q16,"common warmup seam owner conservation");
        { std::ofstream output(path);output << full; }
    }
}
