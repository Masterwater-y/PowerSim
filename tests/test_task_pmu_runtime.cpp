#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <type_traits>

namespace {
void require_task_pmu(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
template<class T, class = void> struct has_task_pmu : std::false_type {};
template<class T> struct has_task_pmu<T, std::void_t<decltype(T{}.task_pmu)>> : std::true_type {};

template<class Counters> void task_pmu_cases() {
    require_task_pmu(has_task_pmu<Counters>::value, "task PMU subset is missing");
    if constexpr (has_task_pmu<Counters>::value) {
        const auto dir=std::filesystem::current_path()/"tmp/task-pmu-repair/runtime-test";
        std::filesystem::create_directories(dir);
        const auto fst=dir/"core0.fst", plan=dir/"state.plan";
        {
            fastsim::BinaryTraceWriter writer(fst.string(),0);
            writer.set_address_space_id(1);
            for (unsigned i=0;i<10;++i) {
                fastsim::TraceRecord r; r.pc=4096+4*i; r.set_kernel_mode(i<7);
                if (i==2 || i==7 || i==8) {
                    r.flags|=fastsim::kLoad|fastsim::kPhysicalAddress;
                    r.address=i==8?0x90000:0x80000; r.size=8;
                }
                if (i==0 || i==4) {
                    r.flags|=fastsim::kBranch|fastsim::kBranchOutcomeValid|fastsim::kTaken;
                    r.target=r.next_pc=r.pc+4;
                }
                if(i==9) r.op_class=fastsim::kSyscallOpClass;
                writer.append(r);
            }
            std::ofstream out(plan);
            out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS 3 TASKS 1\n"
                "CORE 0 10 0 10 101\nTASK 101 101 1\n"
                "ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\n"
                "EVENT 0 0 0 4096 1 1 101 101 1 0\n"
                "EVENT 1 0 1 4100 6 1 101 303 1 1 0\n"
                "EVENT 2 0 5 4116 6 1 303 101 1 1 1\n";
        }
        fastsim::SimulatorConfig config;
        config.cores=1; config.cha_count=1; config.core_model="interval_weave";
        config.interval_scheduler="time_epoch"; config.native_kernel_trace=true;
        config.measurement_scope=fastsim::MeasurementScope::kUserPlusKernel;
        config.thread_state_plan=plan.string(); config.syscall_restart_latency=0;
        config.chunk_instructions=16; config.interval_target_uops=16;
        std::vector<std::unique_ptr<fastsim::TraceSource>> sources;
        sources.push_back(std::make_unique<fastsim::BinaryTraceSource>(fst.string()));
        fastsim::Simulator simulation(config,std::move(sources));
        const auto result=simulation.run(); const Counters& c=result.cores.at(0);
        require_task_pmu(c.task_pmu.retired_instructions==6 && c.other_task_pmu.retired_instructions==4,
                         "switch anchor belongs to previous owner and kernel prefix is counted");
        require_task_pmu(c.task_pmu.retired_uops==30 && c.full_task_pmu.retired_uops==34,
                         "task syscall restores 24 collapsed native transition UOPs");
        require_task_pmu(c.task_pmu.memory_uops==2 && c.other_task_pmu.memory_uops==1,
                         "data memory UOPs follow immutable source owners");
        require_task_pmu(c.task_pmu.l1d_tag_misses==1 && c.other_task_pmu.l1d_tag_misses==1,
                         "non-task request warms task cache without joining its PMU");
        require_task_pmu(c.task_pmu.branches==2 && c.other_task_pmu.branches==1,
                         "native branches and collapsed syscall branch follow owners");
        auto sum=c.task_pmu; sum+=c.other_task_pmu;
        require_task_pmu(sum==c.full_task_pmu,"task plus other conserves full demand PMU");
        require_task_pmu(c.retired_instructions==10 && c.retired_uops==10,
                         "task PMU expansion does not change physical model counters");

        const auto run=[&](const std::vector<fastsim::TraceRecord>& records,
                           fastsim::SimulatorConfig cfg, bool page_maps=false,
                           unsigned switch_at=0) {
            {
                fastsim::BinaryTraceWriter writer(fst.string(),0);
                writer.set_address_space_id(1);
                for(auto r:records) writer.append(r);
                if(page_maps) {
                    fastsim::VirtualPageMapping mapping{};
                    mapping.token=7;
                    mapping.virtual_page=0x12345; mapping.physical_page=0x200;
                    mapping.physical_page_valid=true;
                    mapping.initial_page_table_path.valid=true;
                    mapping.initial_page_table_path.levels=4;
                    mapping.initial_page_table_path.page_size_bits=12;
                    const std::array<std::uint64_t,4> addresses{0x1008,0x2040,0x3180,0x4a28};
                    for(unsigned level=0;level<4;++level)
                        mapping.initial_page_table_path.pte_physical_addresses[level]=addresses[level];
                    mapping.roi_entry_page_table_path=mapping.initial_page_table_path;
                    writer.register_virtual_page_mapping(mapping);
                    writer.register_instruction_page_mapping({0,1,1,0x500});
                }
            }
            {
                std::ofstream out(plan);
                out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS " << (switch_at?2:1)
                    << " TASKS 1\nCORE 0 " << records.size() << " 0 " << records.size()
                    << " 101\nTASK 101 101 1\nACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\n"
                    "EVENT 0 0 0 " << records[0].pc << " 1 1 101 101 1 0\n";
                if(switch_at) out << "EVENT 1 0 " << switch_at << ' ' << records[switch_at].pc
                    << " 6 1 101 303 1 1 0\n";
            }
            cfg.thread_state_plan=plan.string();
            std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
            traces.push_back(std::make_unique<fastsim::BinaryTraceSource>(fst.string()));
            fastsim::Simulator simulator(cfg,std::move(traces));
            return simulator.run();
        };
        auto load=[](std::uint64_t address, unsigned i) {
            fastsim::TraceRecord r; r.pc=4096+4*i;
            r.flags|=fastsim::kLoad|fastsim::kPhysicalAddress; r.address=address; r.size=8;
            r.set_kernel_mode(true); return r;
        };
        {
            auto cfg=config;
            cfg.interval_private_preview=false;
            auto cold_store=load(0x88000,0);
            cold_store.flags=fastsim::kRetires|fastsim::kStore|fastsim::kPhysicalAddress;
            cold_store.set_kernel_mode(true);
            const auto cold=run({cold_store},cfg);
            const Counters& h=cold.cores[0];
            require_task_pmu(h.l2_write_demand_misses==1 && h.l2_read_tag_misses==0,
                             "cold store must execute a write demand private-L2 miss");
            require_task_pmu(h.l2_data_demand_tag_misses==1 &&
                             h.full_task_pmu.private_l2_tag_misses==1 &&
                             h.task_pmu.private_l2_tag_misses==1,
                             "native private-L2 demand miss includes cold write/RFO");
        }
        {
            auto cfg=config;
            cfg.interval_private_preview=true;
            cfg.domain_min_events=1;
            cfg.l1d.size_bytes=64; cfg.l1d.associativity=1;
            cfg.l2.size_bytes=4096; cfg.l2.associativity=2;
            const auto private_hit=run({load(0x8000,0),load(0x8040,1),load(0x8000,2)},cfg);
            const Counters& h=private_hit.cores[0];
            require_task_pmu(h.l2.hits>0 && private_hit.private_preview_events>0,
                             "evicted L1 data must actually hit private L2 in private preview");
            require_task_pmu(h.full_task_pmu.l1d_tag_misses==3 && h.task_pmu.l1d_tag_misses==3 &&
                             h.full_task_pmu.private_l2_tag_misses==2 && h.full_task_pmu.llc_tag_misses==2,
                             "private-preview L2 hit remains a task data L1 tag miss");
            require_task_pmu(h.l1d_read_tag_misses==2,"new task population preserves legacy physical tag bucket");
        }
        {
            auto cfg=config;
            cfg.interval_private_preview=false;
            cfg.l1i_enabled=true; cfg.instruction_address_mode="trace";
            cfg.fetch_buffer_bytes=64;
            cfg.fetch_supply_model=true; cfg.fetch_supply_physical_request_ledger=true;
            cfg.fetch_supply_lower_hierarchy=true;
            cfg.response_queue_feedback=true; cfg.response_sparse_scoreboard=true;
            cfg.ruby_sequencer_max_outstanding=16;
            cfg.dtlb.enabled=true; cfg.dtlb.miss_model="timing_walk";
            cfg.dtlb.hit_latency=0; cfg.dtlb.page_walkers=1; cfg.dtlb.coalesce_misses=false;
            cfg.dtlb.page_walk_levels=4; cfg.dtlb.hierarchy_walk=true;
            cfg.dtlb.page_walk_latency=4*cfg.l1d.hit_latency+cfg.dtlb.page_walk_restart_latency;
            cfg.dtlb.page_walk_address_mode="physical_sidecar";
            auto memory=load(0x200000,0);
            memory.flags|=fastsim::kVirtualPageToken; memory.reserved=7;
            const auto hierarchy=run({memory},cfg,true);
            const Counters& h=hierarchy.cores[0];
            require_task_pmu(h.dtlb_hierarchy_requests==4 && h.instruction_l2.accesses>0,
                             "PTE and instruction requests must actually execute");
            require_task_pmu(h.full_task_pmu.memory_uops==1 && h.full_task_pmu.l1d_tag_misses==1 &&
                             h.full_task_pmu.private_l2_tag_misses==1 && h.full_task_pmu.llc_tag_misses==1,
                             "task data PMU excludes cold PTE and instruction traffic");
        }
        {
            auto cfg=config;
            cfg.response_queue_feedback=true; cfg.response_sparse_scoreboard=true;
            cfg.response_block_summary=true; cfg.response_memory_descriptor=true;
            cfg.response_monotone_iq_calendar=true;
            cfg.needs_tso=true; cfg.tso_store=fastsim::TsoStoreMode::kBuffer;
            cfg.tso_store_buffer_size=8; cfg.buffer_tso_prepare_lifecycle=true;
            cfg.chunk_instructions=128; cfg.interval_target_uops=128;
            cfg.interval_max_cycles=4096; cfg.l1d.store_hit_latency=1;
            cfg.l2.store_hit_latency=5; cfg.store_commit_to_iew_delay=1;
            std::vector<fastsim::TraceRecord> stores;
            for(unsigned i=0;i<66;++i) {
                auto r=load(0x10000+64*i,i);
                r.flags=fastsim::kRetires|fastsim::kStore|fastsim::kPhysicalAddress;
                r.set_kernel_mode(true); stores.push_back(r);
            }
            auto fence=stores.back(); fence.pc+=4;
            fence.flags=fastsim::kRetires|fastsim::kSerialize; fence.set_kernel_mode(true);
            stores.push_back(fence);
            const auto buffered=run(stores,cfg,false,1);
            const Counters& b=buffered.cores[0];
            require_task_pmu(b.buffer_tso_head_demands>0 && b.l1d_sp_ie_im_misses>0,
                             "buffered store role2 publishing path must execute");
            require_task_pmu(b.task_pmu.memory_uops==2 && b.other_task_pmu.memory_uops==64,
                             "buffered stores retain source owner across switch");
            require_task_pmu(b.task_pmu.l1d_tag_misses>0 && b.other_task_pmu.l1d_tag_misses>0 &&
                             b.full_task_pmu.l1d_tag_misses==b.l1d_read_tag_misses+
                                b.l1d_write_demand_misses+b.l1d_sp_ie_im_misses,
                             "accepted buffered-store publishing counts owner subsets exactly once");
            auto conserved=b.task_pmu; conserved+=b.other_task_pmu;
            require_task_pmu(conserved==b.full_task_pmu,"buffered-store owner conservation");
            require_task_pmu(b.full_task_pmu.private_l2_tag_misses==b.l2_data_demand_tag_misses &&
                             b.l2_data_demand_tag_misses<=b.buffer_tso_head_demands &&
                             b.l2_permission_upgrades>0,
                             "prepare/head transactions conserve accepted native demand misses without counting prepares");
        }
        {
            auto cfg=config;
            cfg.response_queue_feedback=true; cfg.response_sparse_scoreboard=true;
            cfg.response_block_summary=true; cfg.response_memory_descriptor=true;
            cfg.response_monotone_iq_calendar=true; cfg.response_activity_certificate=true;
            cfg.needs_tso=true; cfg.tso_store=fastsim::TsoStoreMode::kBuffer;
            cfg.tso_store_buffer_size=114; cfg.ruby_sequencer_max_outstanding=16;
            cfg.ruby_buffer_service_repair=true; cfg.ruby_buffer_service_local_fallback=true;
            cfg.ruby_buffer_service_work_percent=1000;
            std::vector<fastsim::TraceRecord> records{
                load(0x8000,0),load(0x8040,1),load(0x8048,2),load(0x8008,3)};
            records[3].producer_dists[0]=3; records[3].n_src=1;
            const auto retry=run(records,cfg);
            cfg.ruby_buffer_service_max_passes=1;
            const auto fallback=run(records,cfg);
            const Counters& a=retry.cores[0]; const Counters& z=fallback.cores[0];
            require_task_pmu(retry.buffer_service_passes>1 && fallback.buffer_service_local_broad_fallback_epochs>0,
                             "service candidate retry and transaction rollback must execute");
            require_task_pmu(a.full_task_pmu==z.full_task_pmu && a.task_pmu==a.full_task_pmu &&
                             a.full_task_pmu.memory_uops==4 && a.full_task_pmu.llc_tag_misses==2 &&
                             a.l2_data_demand_tag_misses==2 &&
                             z.l2_data_demand_tag_misses==2 &&
                             a.full_task_pmu.private_l2_tag_misses==2,
                             "rollback restores task PMU and accepted requests count once");
        }
    }
}
}

void test_task_pmu_runtime() { task_pmu_cases<fastsim::CoreCounters>(); }
