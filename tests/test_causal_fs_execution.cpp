#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <iostream>
#include <algorithm>
#include <thread>
#include <chrono>
#include "fastsim/op_traits.hpp"
#include "fastsim/dram_address.hpp"
using namespace fastsim;
namespace {
void require(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
std::string contents(const std::string& path){std::ifstream in(path);return {std::istreambuf_iterator<char>(in),{}};}
SimulatorConfig config(unsigned chunk,unsigned cores=1){
 SimulatorConfig c;c.cores=cores;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.causal_runtime=true;c.causal_fs_execution=true;c.coherence=false;c.chunk_instructions=chunk;c.interval_target_uops=1;c.cha_count=1;
 c.dispatch_to_issue=0;c.issue_to_execute=0;c.execute_to_commit=0;c.load_response_to_ready=0;
 c.fetch_to_decode=0;c.decode_to_rename=0;c.rename_to_dispatch=0;
 return c;
}
TraceRecord alu(std::uint64_t pc=4096){TraceRecord r;r.pc=pc;r.op_class=1;return r;}
TraceRecord load(){auto r=alu();r.op_class=56;r.flags=kRetires|kLoad|kPhysicalAddress;r.address=4096;r.size=8;return r;}
void write(const std::string& path,unsigned core,const std::vector<TraceRecord>& rows){
 BinaryTraceWriter out(path,core);out.set_address_space_id(1);
 out.enable_complete_dependencies();out.enable_memory_request_roles();out.enable_functional_retirement();out.enable_store_facts();
 for(const auto& r:rows){
  fst::StoreRequestFacts f;f.address=r.address;f.size=r.size;f.byte_enable=255;
  f.attributes=fst::kStoreFactsComplete|(r.is_write()?fst::kStoreOrdinary:(fst::kMemoryRead|fst::kOrdinaryLoad));
  out.append(r,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,r.is_memory()?&f:nullptr);
 }
 out.close();
}
// Only host delivery timing/intentional corruption changes. All rows and facts
// still come from the production binary reader used by WarmupInstructionTraceSource.
class DeliveredBinary final:public TraceSource {
 BinaryTraceSource binary_;bool jitter_,bad_ordinal_,special_;std::vector<fst::TranslationFragment> fragments_;
public:
 DeliveredBinary(const std::string& path,bool jitter,bool bad=false,bool special=false):binary_(path),jitter_(jitter),bad_ordinal_(bad),special_(special){}
 bool next(TraceRecord& r)override{if(jitter_)std::this_thread::sleep_for(std::chrono::microseconds(30));const bool ok=binary_.next(r);if(ok && special_){fst::TranslationFragment f;f.physical_address=r.address;f.size=r.size;f.mapping.kind=fst::TranslationKind::kIo;fragments_={f};}return ok;}
 bool dynamic_translations()const override{return special_;}
 std::optional<std::uint64_t> current_translation_record_ordinal()const override{return binary_.current_source_record_ordinal();}
 const std::vector<fst::TranslationFragment>& current_translation_fragments()const override{return fragments_;}
 std::string description()const override{return binary_.description();}
 const fst::RetiredFrontendMacro* current_frontend_macro()const override{return binary_.current_frontend_macro();}
 std::optional<std::uint64_t> current_frontend_record_ordinal()const override{return binary_.current_frontend_record_ordinal();}
 const fst::StaticUopFactsRow* current_static_uop_facts()const override{return binary_.current_static_uop_facts();}
 const std::vector<fst::StaticUopOperand>& current_static_uop_operands()const override{return binary_.current_static_uop_operands();}
 std::string static_uop_policy_identity()const override{return binary_.static_uop_policy_identity();}

 bool complete_dependencies()const override{return binary_.complete_dependencies();}
 bool functional_retirement_complete()const override{return binary_.functional_retirement_complete();}
 bool memory_request_roles_complete()const override{return binary_.memory_request_roles_complete();}
 std::optional<FunctionalSyncLayout> functional_sync_layout()const override{return binary_.functional_sync_layout();}
 std::optional<std::uint64_t> current_source_record_ordinal()const override{auto o=binary_.current_source_record_ordinal();if(o&&bad_ordinal_)++*o;return o;}
 std::optional<fst::MemoryRequestRole> current_memory_request_role()const override{return binary_.current_memory_request_role();}
 std::optional<bool> current_ordinary_load_eligible()const override{return binary_.current_ordinary_load_eligible();}
 const fst::StoreRequestFacts* current_memory_facts()const override{return binary_.current_memory_facts();}
 const fst::StoreRequestFacts* current_store_facts()const override{return binary_.current_store_facts();}
 const std::vector<std::uint32_t>& current_dependency_extensions()const override{return binary_.current_dependency_extensions();}
 std::uint64_t address_space_id_for_record(std::uint64_t ordinal)const override{return binary_.address_space_id_for_record(ordinal);}
};
std::unique_ptr<TraceSource> source(const std::string& path,std::uint64_t warm,std::uint64_t measured,
                                  std::uint64_t warm_macros=UINT64_MAX,std::uint64_t measured_macros=UINT64_MAX,bool jitter=false){
 std::unique_ptr<TraceSource> input=jitter?std::unique_ptr<TraceSource>(new DeliveredBinary(path,true)):std::unique_ptr<TraceSource>(new BinaryTraceSource(path));
 return std::make_unique<WarmupInstructionTraceSource>(std::move(input),
  warm_macros==UINT64_MAX?warm:warm_macros,measured_macros==UINT64_MAX?measured:measured_macros,warm,measured,true);
}
void continuous_warmup(){
 const std::string path="tmp/f1-fs-bridge/seam.fst";auto dependent=alu(4100);dependent.n_src=1;dependent.producer_dists[0]=1;
 write(path,0,{load(),dependent,alu(4104)});
 std::string reference;
 for(unsigned chunk:{1,2,7,128,129}){
  auto c=config(chunk==129?7:chunk);c.causal_runtime_audit_path="tmp/f1-fs-bridge/seam"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,1,2,UINT64_MAX,UINT64_MAX,chunk==129));
  const auto result=Simulator(c,std::move(inputs)).run();
  require(result.cores[0].records==2 && result.cores[0].retired_instructions==2,"warmup pause must resume full measured source");
  const auto audit=contents(c.causal_runtime_audit_path);
  if(reference.empty())reference=audit;else require(audit==reference,"chunks preserve retirement/request/accounting tuples");
 }
}
struct AuditRow {std::string kind;std::uint64_t cycle,core,sequence,fragment,line,issue,completion;};
std::vector<AuditRow> audit_rows(const std::string& path){
 std::istringstream in(contents(path));std::string line;std::getline(in,line);std::vector<AuditRow> rows;
 while(std::getline(in,line)){for(auto& ch:line)if(ch==',')ch=' ';std::istringstream fields(line);AuditRow r;
  if(fields>>r.kind>>r.cycle>>r.core>>r.sequence>>r.fragment>>r.line>>r.issue>>r.completion)rows.push_back(r);}
 return rows;
}
AuditRow find(const std::vector<AuditRow>& rows,const std::string& kind,unsigned core,unsigned seq){
 for(const auto& r:rows)if(r.kind==kind&&r.core==core&&r.sequence==seq)return r;
 throw std::runtime_error("missing audit witness "+kind);
}
void cross_core_graph(){
 const std::string dir="tmp/f1-fs-bridge/";
 write(dir+"graph0.fst",0,{load()});write(dir+"graph1.fst",1,{alu(8192),load()});
 std::ofstream plan(dir+"graph.plan");plan<<"FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 2\nCORE 0 1 0 1\nCORE 1 2 0 2\n"
  "EVENT 0 0 0 4096 1 5 1 0\nEVENT 1 1 0 8192 1 6 1 1 0\n";plan.close();
 std::string reference;
 for(unsigned chunk:{1,2,7,128}){
  auto c=config(chunk,2);c.functional_sync_plan=dir+"graph.plan";c.causal_runtime_audit_path=dir+"graph"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"graph0.fst",0,1));inputs.push_back(source(dir+"graph1.fst",0,2));
  const auto result=Simulator(c,std::move(inputs)).run();const auto rows=audit_rows(c.causal_runtime_audit_path);
  const auto parent=find(rows,"retire",0,0);const auto release=find(rows,"graph_release",1,0);
  require(release.cycle==parent.cycle && release.issue<parent.cycle,"remote release must wait for modeled miss retirement");
  require(find(rows,"private_probe",1,1).cycle>=release.cycle,"postgate memory must not issue before graph release");
  require(result.functional_sync_dependency_waits==1 && result.cores[0].records==1 && result.cores[1].records==2,"final-row boundary and zero-prefix source close exactly");
  const auto audit=contents(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"graph lifecycle chunk invariant");
 }
}
void local_clocks_and_kernel(){
 const std::string dir="tmp/f1-fs-bridge/";std::vector<TraceRecord> rows;
 for(unsigned i=0;i<10;++i){auto r=alu(4096+4*i);if(i>=2)r.set_kernel_mode(true);if(i==1 || i==6 || i==9)r.pc=0xffffffff81517a0aULL;else if(i==3 || i==8)r.pc=0xffffffff81067eefULL;rows.push_back(r);}
 write(dir+"tasks0.fst",0,rows);auto r1=alu(8196);r1.n_src=1;r1.producer_dists[0]=1;auto first=load();first.pc=8192;first.set_kernel_mode(true);
 write(dir+"tasks1.fst",1,{first,r1,alu(8200)});
 std::ofstream plan(dir+"tasks.plan");plan<<"FASTSIM_THREAD_STATE_PLAN_V1\nCORES 2 EVENTS 10 TASKS 2\n"
 "CORE 0 10 2 8 101\nCORE 1 3 1 2 202\nTASK 101 101 1\nTASK 202 202 2\n"
 "ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nCLOSURE 1 1\n"
 "TASK_CLOCK task-lifecycle-basecpu-active-v1\nCLOCK_STATE 0 1 1 101 1\nCLOCK_STATE 1 1 1 202 1\n"
 "EVENT 0 0 0 4096 1 0 101 101 1 0\n"
 "EVENT 1 0 1 18446744071584184842 8 0 101 303 0 1 0\n"
 "EVENT 2 0 2 4104 6 1 101 303 2 1 1\n"
 "EVENT 3 0 3 18446744071579270895 9 1 101 303 0 1 2\n"
 "EVENT 4 0 5 4116 7 1 101 0 0 1 3\n"
 "EVENT 5 0 6 18446744071584184842 8 1 303 101 0 1 4\n"
 "EVENT 6 0 7 4124 6 1 303 101 1 1 5\n"
 "EVENT 7 0 8 18446744071579270895 9 1 303 101 0 1 6\n"
 "EVENT 8 0 9 18446744071584184842 8 1 101 303 0 1 7\n"
 "EVENT 9 1 0 8192 1 0 202 202 2 0\n";plan.close();
 std::string reference;
 for(unsigned chunk:{1,2,7,128}){
  auto c=config(chunk,2);c.native_kernel_trace=true;c.syscall_restart_latency=0;c.measurement_scope=MeasurementScope::kUserPlusKernel;
  c.thread_state_plan=dir+"tasks.plan";c.causal_runtime_audit_path=dir+"tasks"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"tasks0.fst",2,8));inputs.push_back(source(dir+"tasks1.fst",1,2));
  const auto result=Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
  require(result.cores[0].records==8 && result.cores[0].native_kernel_records==8 && result.cores[0].native_kernel_retired_uops==8 && result.cores[0].native_kernel_retired_instructions==8,"blocked task still executes retained kernel stream");
  require(result.task_clock_complete && result.thread_state_accounting_complete,"real plans/clocks close certified population");
  require(result.task_clock_macro_instructions==3,"task macros use pre-boundary eligibility");
  require(result.thread_state_task_macro_instructions==5,"assembly owner macro population remains independent");
  const auto begin0=find(events,"measurement_begin",0,1).cycle,begin1=find(events,"measurement_begin",1,0).cycle;
  require(begin0<begin1 && find(events,"core_accounting_finish",0,10).cycle<begin1,"short core finishes before other core local origin");
  for(unsigned core=0;core<2;++core)require(result.task_clock_active_q16[core]+result.task_clock_other_q16[core]+
   result.task_clock_transition_q16[core]+result.task_clock_unknown_q16[core]==result.cores[core].cycles*65536,"per-core task clocks conserve without global reset");
  require(result.task_clock_transition_q16[0]>0 && result.task_clock_other_q16[0]>0,"pending warmup account-out survives local begin and wake does not own CPU");
  const auto audit=contents(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"task lifecycle chunk invariant");
 }
}
void partial_macro_and_corrupt_source(){
 const std::string path="tmp/f1-fs-bridge/partial.fst";auto first=alu();first.flags=kRetires|kMicroOp;
 auto last=alu(4100);last.flags=kRetires|kMicroOp|kLastMicroOp;last.n_src=1;last.producer_dists[0]=1;
 write(path,0,{first,last});
 auto c=config(7);std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,1,1,0,1));
 const auto result=Simulator(c,std::move(inputs)).run();require(result.cores[0].retired_instructions==1 && result.functional_warmup_instructions==0,"partial macro survives phase seam");
 for(unsigned variant=0;variant<3;++variant){bool rejected=false;
  try{std::vector<std::unique_ptr<TraceSource>> bad;
   if(variant==0)bad.push_back(source(path,0,1)); // crops original source
   if(variant==1)bad.push_back(source(path,1,1,1,1)); // corrupt macro expectation
   if(variant==2)bad.push_back(source(path,1,2)); // source shorter than declaration
   Simulator(c,std::move(bad)).run();
  }catch(const std::exception&){rejected=true;}require(rejected,"corrupt layout/count must fail closed");
 }
}

SimulatorConfig native_config(unsigned chunk,bool paired=false){
 auto c=config(chunk);c.causal_native_memory=true;c.store_commit_to_iew_delay=3;
 c.causal_mandatory_queue_latency=1;c.causal_l1_data_access_latency=3;c.causal_mandatory_queue_ordered=false;c.causal_l1_request_latency=2;
 c.l1d.replacement=c.l2.replacement=c.llc.replacement=ReplacementPolicy::kLru;
 c.causal_l1_response_latency=2;c.causal_l2_request_latency=2;c.causal_l2_response_latency=2;c.causal_llc_to_l2_latency=1;
 c.spr_profile.limits={16,16,16,48,256,256,256,112,4,4};
 if(paired){
  c.causal_spr_resources=true;c.spr_profile.generic_fu_preset=spr::kGenericFuPreset;c.spr_profile.iq_entries={128,72,48};
  c.spr_profile.load_bank_conflicts=true;c.spr_profile.load_banks=16;c.spr_profile.load_bank_ports=2;c.spr_profile.cache_load_ports=3;c.spr_profile.cache_store_ports=2;
  c.rob_entries=512;c.lq_entries=240;c.cache_load_ports=3;c.cache_store_ports=2;c.memory_units=12;c.dispatch_width=6;c.issue_width=12;
  c.fetch_width=c.decode_width=c.rename_width=6;
  c.spr_profile.static_uop_policy_sha256=std::string(64,'4');
 }
 return c;
}
std::string normalized(const std::string& path){std::istringstream in(contents(path));std::vector<std::string> rows;std::string line;
 while(std::getline(in,line))if(line.rfind("core_resource_cost,",0)!=0)rows.push_back(line);
 std::sort(rows.begin(),rows.end());std::ostringstream out;for(const auto& row:rows)out<<row<<'\n';return out.str();}
void stores_across_seam(){
 const std::string dir="tmp/f1-fs-bridge/";auto store=load();store.op_class=57;store.flags=kRetires|kStore|kPhysicalAddress;
 write(dir+"store.fst",0,{store,alu(4100)});
 std::string ordinary_reference,paired_reference;
 for(unsigned chunk:{1,2,7,128}){
  auto c=native_config(chunk);c.causal_runtime_audit_path=dir+"store"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"store.fst",1,1));
  const auto result=Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
  const auto finish=find(events,"core_accounting_finish",0,2);
  bool detached=false;for(const auto& e:events)if(e.kind=="store_write_complete")detached=e.cycle>finish.cycle;
  require(detached && result.cores[0].records==1,"late detached store completion cannot extend measured core lifetime");
  const auto audit=normalized(c.causal_runtime_audit_path);if(ordinary_reference.empty())ordinary_reference=audit;else require(audit==ordinary_reference,"store seam lifecycle chunk invariant");
 }
 // SA belongs to warmup; SD ends the same architectural macro in measurement.
 {
  BinaryTraceWriter out(dir+"pair.fst",0);std::array<std::uint8_t,32> policy;policy.fill(0x44);
  out.enable_complete_dependencies();out.enable_memory_request_roles();out.enable_functional_retirement();out.enable_store_facts();out.enable_static_uop_facts(policy);
  for(bool address:{true,false}){
   auto r=address?store:alu(4100);r.op_class=address?kSprStoreAddress:kSprStoreData;
   r.flags|=kMicroOp;if(!address)r.flags|=kLastMicroOp;
   fst::StaticUopFactsRow f;f.flags=fst::kStaticExecutionKnown|fst::kStaticOperandRolesKnown|fst::kStaticAddressDataKnown|fst::kStaticStorePairKnown;
   f.nominal_latency=1;f.store_role=address?fst::StaticStoreRole::Address:fst::StaticStoreRole::Data;f.store_group=1;
   fst::StoreRequestFacts memory;memory.address=r.address;memory.size=r.size;memory.byte_enable=255;memory.attributes=fst::kStoreFactsComplete|fst::kStoreOrdinary;
   const std::vector<fst::StaticUopOperand> operands;
   out.append(r,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,address?&memory:nullptr,&f,&operands);
  }out.close();
 }
 for(unsigned chunk:{1,2,7,128}){
  auto c=native_config(chunk,true);c.causal_runtime_audit_path=dir+"pair"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"pair.fst",1,1,0,1));
  const auto result=Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
  unsigned allocations=0,commits=0,writes=0;for(const auto& e:events){allocations+=e.kind=="store_allocate";commits+=e.kind=="store_commit";writes+=e.kind=="store_write_complete";}
  require(allocations==1&&commits==1&&writes==1&&result.cores[0].retired_instructions==1,"SA/SD survives seam with one store and measured macro");
  require(find(events,"store_commit",0,0).cycle==find(events,"retire",0,1).cycle+3,"pair commit retains configured delay across seam");
  const auto audit=normalized(c.causal_runtime_audit_path);if(paired_reference.empty())paired_reference=audit;else require(audit==paired_reference,"SA/SD seam lifecycle chunk invariant");
 }
}
void corrupted_origins_and_anchors(){
 const std::string dir="tmp/f1-fs-bridge/";write(dir+"corrupt.fst",0,{alu(),alu(4100)});
 for(unsigned mode=0;mode<5;++mode){
  auto c=config(1);std::vector<std::unique_ptr<TraceSource>> inputs;
  if(mode==0)inputs.push_back(std::make_unique<WarmupInstructionTraceSource>(std::make_unique<DeliveredBinary>(dir+"corrupt.fst",false,true),0,2,0,2,true));
  else {inputs.push_back(source(dir+"corrupt.fst",0,2));
   std::ofstream plan(dir+"corrupt.plan");plan<<"FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 1 EVENTS 1\nCORE 0 2 0 2\nEVENT 0 0 0 "
    <<(mode==1?9999:4096)<<' '<<(mode==2?2:1)<<" 5 "<<(mode==3?0:1)<<" 0\n";plan.close();c.functional_sync_plan=dir+"corrupt.plan";
   if(mode==4){c.functional_sync_plan.clear();std::fstream f(dir+"corrupt.fst.fsem",std::ios::in|std::ios::out|std::ios::binary);f.seekp(40);std::uint64_t wrong=0;f.write(reinterpret_cast<const char*>(&wrong),8);f.close();inputs.clear();inputs.push_back(source(dir+"corrupt.fst",0,2));}
  }
  bool rejected=false;try{Simulator(c,std::move(inputs)).run();}catch(const std::exception&){rejected=true;}require(rejected,"corrupt ordinal/PC/ASID/phase/source hash rejects");
 }
}

void overlap_and_origin_members(){
 const auto events=audit_rows("tmp/f1-fs-bridge/seam1.csv");const auto warm=find(events,"retire",0,0);
 require(find(events,"retire",0,2).completion<warm.cycle,"measured independent ALU completes before warmup miss returns");
 require(find(events,"retire",0,1).issue>=warm.completion,"RAW dependency keeps warmup producer across seam");
 const std::string dir="tmp/f1-fs-bridge/";
 {BinaryTraceWriter out(dir+"origins.fst",0);out.set_address_space_id(1);out.enable_complete_dependencies();out.enable_memory_request_roles();out.enable_functional_retirement();
  for(unsigned i=0;i<3;++i){auto r=alu(4096+4*i);r.set_kernel_mode(true);r.flags=kRetires|kMicroOp|kLastMicroOp;
   out.append(r,nullptr,{},fst::MemoryRequestRole::Data,0,i<2,i==1);}
  out.close();}
 std::ofstream plan(dir+"origins.plan");plan<<"FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS 1 TASKS 1\nCORE 0 3 0 3 101\nTASK 101 101 1\n"
 "ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nTASK_CLOCK task-lifecycle-basecpu-active-v1\nCLOCK_STATE 0 1 1 101 1\nEVENT 0 0 0 4096 1 1 101 101 1 0\n";plan.close();
 auto c=config(128);c.native_kernel_trace=true;c.syscall_restart_latency=0;c.measurement_scope=MeasurementScope::kUserPlusKernel;c.thread_state_plan=dir+"origins.plan";
 std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"origins.fst",0,3));
 const auto result=Simulator(c,std::move(inputs)).run();
 require(result.cores[0].records==3 && result.task_clock_macro_instructions==1 && result.thread_state_task_macro_instructions==1,
 "hardware-entry/abandoned members execute without inventing workload task macros despite reader runahead");
}
// Production binary + FFE, not a second scheduling model.
bool instruction_matrix=false;
void enable_instruction(SimulatorConfig& c) {
 c.causal_instruction_fetch=true;c.causal_native_coherence=true;c.cha_xor_hash=false;
 c.l1i_enabled=true;c.fetch_supply_model=true;c.fetch_supply_physical_request_ledger=true;c.fetch_supply_lower_hierarchy=true;
 c.l1i.replacement=ReplacementPolicy::kLru;c.causal_l0_transitions_per_cycle=32;
 auto& n=c.native_coherence;n.source_bound=true;n.source_identity=spr::kNativeCoherenceSourceIdentity;
 n.transitions={32,32,4,32};n.llc_request_latency=2;n.llc_response_latency=2;n.directory_memory_latency=1;n.private_unblock_latency=1;
 n.ordered_private_links=n.unordered_network_queues=n.deterministic_unbounded_queues=true;
 n.directories.push_back({0,dram_physical_extent(c.dram),0,{}});
}
SimulatorConfig frontend_config(unsigned chunk){
 auto c=native_config(chunk,true);c.causal_static_integer=true;c.spr_profile.integer_nominal_shape_verified=true;
 c.causal_retired_frontend=true;
 c.spr_retired_frontend=true;c.fetch_buffer_bytes=64;c.fetch_queue_entries=144;
 c.rename_to_dispatch=2;c.spr_frontend.rename_width=c.rename_width;
 if(instruction_matrix){enable_instruction(c);c.causal_early_store_prepare=true;c.spr_profile.early_store_prepare=true;}
 return c;
}
void write_frontend(const std::string& path,unsigned core,const std::vector<TraceRecord>& rows,
                    std::vector<fst::RetiredFrontendMacro> macros={}){
 BinaryTraceWriter out(path,core);out.set_address_space_id(1);std::array<std::uint8_t,32> policy;policy.fill(0x44);
 out.enable_complete_dependencies();out.enable_memory_request_roles();out.enable_functional_retirement();out.enable_store_facts();
 out.enable_static_uop_facts(policy);out.enable_frontend_facts();
 if(macros.empty())for(unsigned i=0;i<rows.size();++i){fst::RetiredFrontendMacro m;m.begin_record=i;m.end_record=i+1;
  m.pc=rows[i].pc;m.address_space_id=1;m.code_identity=11;m.semantic_identity=19;m.bytes[0]=0x90;m.instruction_size=1;m.observed_uops=1;
  m.known_flags=fst::kFrontendRequiredKnown;m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro|fst::kFrontendDsbEligible;macros.push_back(m);}
 for(const auto& m:macros)out.register_frontend_macro(m);
 for(const auto& r:rows){
  fst::StaticUopFactsRow f;f.flags=fst::kStaticExecutionKnown|fst::kStaticOperandRolesKnown;f.nominal_latency=1;
  f.source_count=r.n_src;f.destination_count=r.n_dst;
  std::vector<fst::StaticUopOperand> operands;
  for(unsigned i=0;i<r.n_src;++i){fst::StaticUopOperand o;o.consumer_operand=static_cast<std::uint16_t>(i);o.reg_class=0;o.producer_distance=r.producer_dists[i];operands.push_back(o);}
  if(r.canonical_op_class()==1){f.zero_idiom=1;f.source_count=0;}
  if(r.canonical_op_class()==kSprStoreAddress || r.canonical_op_class()==kSprStoreData){
   f.flags|=fst::kStaticAddressDataKnown|fst::kStaticStorePairKnown;
   f.store_group=1;f.store_role=r.is_write()?fst::StaticStoreRole::Address:fst::StaticStoreRole::Data;
   if(r.is_write())f.address_operands=(1ull<<r.n_src)-1;else f.data_operands=(1ull<<r.n_src)-1;
  }
  fst::StoreRequestFacts memory;memory.address=r.address;memory.size=r.size;memory.byte_enable=255;
  memory.attributes=fst::kStoreFactsComplete|(r.is_write()?fst::kStoreOrdinary:(fst::kMemoryRead|fst::kOrdinaryLoad));
  out.append(r,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,r.is_memory()?&memory:nullptr,&f,&operands);
 }out.close();
}
void causal_instruction_supply() {
 const std::string path="tmp/f1-fs-bridge/ifetch.fst";
 auto a=alu(4096);a.op_class=4;auto b=a;b.pc=8192;
 write_frontend(path,0,{a,a,b,a});
 auto c=frontend_config(1);enable_instruction(c);c.l1i_enabled=true;c.fetch_supply_model=true;
 c.fetch_supply_physical_request_ledger=true;c.fetch_supply_lower_hierarchy=true;
 c.causal_runtime_audit_path=path+".csv";
 std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,0,4));
 const auto result=Simulator(c,std::move(inputs)).run();
 require(result.cores[0].retired_instructions==4,"causal instruction supply preserves macro population");
 require(result.cores[0].l1i.misses==2,"actual causal instruction misses reach L1I");
 require(!SimulatorConfig{}.causal_instruction_fetch,"instruction adapter defaults OFF");
 for(unsigned mode=0;mode<4;++mode){auto bad=c;if(mode==0)bad.instruction_address_mode="trace";if(mode==1)bad.dtlb.enabled=true;
  if(mode==2)bad.causal_native_coherence=false;if(mode==3)bad.l1i.replacement=ReplacementPolicy::kTreePlru;
  bool rejected=false;try{bad.validate();}catch(const std::exception&){rejected=true;}require(rejected,"I adapter retains exact-mode, DTLB, native-coherence and unsupported policy guards");}

}
void causal_instruction_spans() {
 const std::string path="tmp/f1-fs-bridge/ifetch-spans.fst";
 auto a=alu(4096);a.op_class=4;auto b=a;b.pc=8192;auto cross=a;cross.pc=12287;
 std::vector<TraceRecord> records{a,b,a,cross};std::vector<fst::RetiredFrontendMacro> macros;
 for(unsigned i=0;i<records.size();++i){fst::RetiredFrontendMacro m;m.begin_record=i;m.end_record=i+1;
  m.pc=records[i].pc;m.address_space_id=1;m.code_identity=11;m.semantic_identity=19;m.bytes[0]=0x90;if(i==3)m.bytes[1]=0x90;m.instruction_size=i==3?2:1;m.observed_uops=1;
  m.known_flags=fst::kFrontendRequiredKnown;m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;macros.push_back(m);}
 write_frontend(path,0,records,macros);std::string reference;
 for(unsigned chunk:{1,2,7,128,129}) {
  auto c=frontend_config(chunk==129?7:chunk);enable_instruction(c);
  c.dram.size_bytes=32ull<<30;c.dram.channels=8;c.dram.memory_layout="x86-low3g-high4g-v1";
  c.native_coherence.directories={{0,3ull<<30,0,{}},{4ull<<30,33ull<<30,0,{}}};
  c.causal_runtime_audit_path=path+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,1,3,UINT64_MAX,UINT64_MAX,chunk==129));
  const auto result=Simulator(c,std::move(inputs)).run();const auto& count=result.cores[0];
  require(count.retired_instructions==3 && count.l1i.misses==4 && count.l1i.hits==1,"cross-page requests use real I tags across warmup");
  require(count.physical_instruction_fetch_requests==5 && count.instruction_fetch_lower_hierarchy_requests==4 && count.memory_accesses==0,"I requests never inflate D population");
  const auto events=audit_rows(c.causal_runtime_audit_path);
  unsigned demands=0,responses=0;bool high=false;
  for(const auto& e:events){demands+=e.kind=="instruction_demand";responses+=e.kind=="instruction_response";
   if(e.kind=="instruction_memory"){require(dram_contains_line(c.dram,e.issue,64),"modeled instruction backing routes within split RAM");high|=e.issue*64>=4ull<<30;}
   require(e.kind!="load_send","instruction packets bypass LSQ banks/ports");
   if(e.kind=="mandatory_enqueued")require(e.issue==1,"I mandatory entry omits Dcache data latency");
   if(e.kind=="coherence_release" && e.issue==0)require(e.completion==0,"I miss has shared L0 TBE but no D fill");
  }
  require(demands==5 && responses==5 && high,"sequential fragment owners return exactly once through real split backing");
  const auto audit=normalized(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"I owners and clocks invariant across chunks and host delay");
 }
}
void causal_instruction_capacity() {
 const std::string path="tmp/f1-fs-bridge/ifetch-capacity.fst";
 std::vector<TraceRecord> rows;for(unsigned i=0;i<32;++i){auto r=load();r.address=0x10000+64*i;r.flags|=kMicroOp;if(i==31)r.flags|=kLastMicroOp;rows.push_back(r);}
 auto tail=alu(8192);tail.op_class=4;rows.push_back(tail);
 fst::RetiredFrontendMacro m;m.begin_record=0;m.end_record=32;m.pc=4096;m.address_space_id=1;m.code_identity=11;m.semantic_identity=19;
 m.bytes[0]=0x90;m.instruction_size=1;m.observed_uops=32;m.known_flags=fst::kFrontendRequiredKnown;m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;
 auto last=m;last.begin_record=32;last.end_record=33;last.pc=8192;last.observed_uops=1;
 write_frontend(path,0,rows,{m,last});auto c=frontend_config(7);enable_instruction(c);c.rename_width=1;c.spr_frontend.rename_width=1;
 c.causal_runtime_audit_path=path+".csv";std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,0,33,0,2));
 const auto result=Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
 const auto demand=find(events,"instruction_demand",0,32),admit=find(events,"sequencer_request",0,32);
 require(admit.cycle>demand.cycle,"full ordinary16 stalls actual frontend I admission until data returns");
 unsigned live=0,peak=0;for(const auto& e:events){if(e.kind=="sequencer_request" || e.kind=="sequencer_follower")peak=std::max(peak,++live);if(e.kind=="sequencer_response"){require(live>0,"ordinary callback conservation");--live;}}
 require(peak==16 && live==0 && result.cores[0].memory_accesses==32 && result.cores[0].physical_instruction_fetch_requests==2,"one real ordinary16 shared table, distinct I/D populations");
}
void frontend_actual_rename(){
 const std::string path="tmp/f1-fs-bridge/frontend.fst";
 std::vector<TraceRecord> records;for(unsigned i=0;i<4;++i){auto r=i==0?load():alu(4096);r.op_class=i==0?56:4;records.push_back(r);}write_frontend(path,0,records);
 std::string reference;
 for(unsigned chunk:{1,2,7,128,129}){
  auto c=frontend_config(chunk==129?7:chunk);c.rob_entries=1;c.causal_runtime_audit_path=path+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,1,3,UINT64_MAX,UINT64_MAX,chunk==129));
  Simulator(c,std::move(inputs)).run();const auto rows=audit_rows(c.causal_runtime_audit_path);
  for(unsigned i=0;i<4;++i){const auto rename=find(rows,"frontend_rename",0,i);const auto dispatch=find(rows,"frontend_dispatch",0,i);
   require(dispatch.cycle>=rename.cycle+2,"actual rename precedes dispatch by imported two-cycle edge");
   if(i)require(rename.cycle>=find(rows,"retire",0,i-1).cycle,"ROB pressure delays actual IDQ acknowledgement");
  }
  const auto audit=normalized(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"frontend backend lifecycle tuple chunk invariant");
 }
}

void frontend_width_modes_and_pressure(){
 const std::string dir="tmp/f1-fs-bridge/";
 for(unsigned pressure=0;pressure<3;++pressure){
  std::vector<TraceRecord> rows;
  for(unsigned i=0;i<9;++i){auto r=load();if(i>=3){r=alu(4096);r.op_class=4;}
   if(i>0 && i<3){r.n_src=1;r.producer_dists[0]=1;}
   rows.push_back(r);}
  fst::RetiredFrontendMacro m;m.begin_record=0;m.end_record=9;m.pc=4096;m.address_space_id=1;m.code_identity=11;m.semantic_identity=19;
  m.instruction_size=1;m.bytes[0]=0x90;m.observed_uops=9;m.known_flags=fst::kFrontendRequiredKnown;
  m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro|fst::kFrontendDsbEligible;
  for(unsigned i=0;i<rows.size();++i){rows[i].flags|=kMicroOp;if(i+1==rows.size())rows[i].flags|=kLastMicroOp;}
  const auto path=dir+"frontpressure"+std::to_string(pressure)+".fst";write_frontend(path,0,rows,{m});
  std::string reference;
  for(unsigned chunk:{1,2,7,128,129}){
   auto c=frontend_config(chunk==129?7:chunk);c.rename_width=2;c.spr_frontend.rename_width=2;c.spr_frontend.idq_capacity=4;c.fetch_queue_entries=4;
   if(pressure==1)c.lq_entries=1;if(pressure==2)c.spr_profile.iq_entries[1]=1;
   c.causal_runtime_audit_path=path+std::to_string(chunk)+".csv";
   std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,1,8,0,1,chunk==129));
   const auto result=Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
   require(result.cores[0].records==8 && result.cores[0].retired_instructions==1,"warmup inside pending macro preserves functional denominator");
   std::map<std::uint64_t,unsigned> widths;
   bool held=false;
   for(unsigned i=0;i<rows.size();++i){const auto rename=find(events,"frontend_rename",0,i),delivery=find(events,"frontend_delivery",0,i),dispatch=find(events,"frontend_dispatch",0,i);
    require(++widths[rename.cycle]<=2,"actual rename respects slot width while dispatch wider");
    require(dispatch.cycle>=rename.cycle+2 && rename.cycle>=delivery.cycle,"delivery/rename/dispatch causal order");
    held|=rename.cycle>delivery.cycle;
    require(delivery.issue<=4 && rename.issue<4,"IDQ credits bounded and returned only at rename");
   }
   require(held,"backend or rename width must retain delivered IDQ owner");
   if(pressure)require(find(events,"frontend_rename",0,1).cycle>=find(events,pressure==1?"retire":"iq_release",0,0).cycle,"LQ/IQ in-flight reservation prevents premature rename");
   const auto audit=normalized(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"whole frontend/backend pressure tuple stream survives chunks and host delay");
  }
 }
 // Repeated complete template learns DSB; explicit microcode and partial/pseudo
 // stay in their own source domains. A source seam is not macro completion.
 std::vector<TraceRecord> rows;std::vector<fst::RetiredFrontendMacro> macros;
 for(unsigned i=0;i<6;++i){auto r=alu(4096);r.op_class=4;rows.push_back(r);
  fst::RetiredFrontendMacro m;m.begin_record=i;m.end_record=i+1;m.pc=r.pc;m.address_space_id=1;m.code_identity=11;m.semantic_identity=19;
  m.instruction_size=1;m.bytes[0]=0x90;m.observed_uops=1;m.known_flags=fst::kFrontendRequiredKnown;
  m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro|fst::kFrontendDsbEligible;
  if(i==2)m.flags|=fst::kFrontendMicrocode;
  if(i==3){m.flags=fst::kFrontendStartsMacro;rows.back().flags|=kMicroOp;}
  if(i==4){m.flags=fst::kFrontendStartsMacro|fst::kFrontendPseudo|fst::kFrontendComplete;m.instruction_size=0;m.bytes={};}
  macros.push_back(m);
 }
 const auto path=dir+"frontmodes.fst";write_frontend(path,0,rows,macros);
 std::string reference;
 for(unsigned chunk:{1,2,7,128,129}){auto c=frontend_config(chunk==129?7:chunk);c.causal_runtime_audit_path=path+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(path,0,6,0,5,chunk==129));
  Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
  require(find(events,"frontend_delivery",0,0).line==0 && find(events,"frontend_delivery",0,1).line==1 &&
   find(events,"frontend_delivery",0,2).line==2 && find(events,"frontend_delivery",0,3).line==0 && find(events,"frontend_delivery",0,4).line==0,"actual MITE/DSB/MS and partial/pseudo source witnesses");
  const auto audit=normalized(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"source mode tuple stream invariant");
 }
}

void frontend_zero_store_and_graph(){
 const std::string dir="tmp/f1-fs-bridge/";
 auto zero=alu();zero.n_dst=1;
 auto sa=load();sa.op_class=kSprStoreAddress;sa.flags=kRetires|kMicroOp|kStore|kPhysicalAddress;
 auto sd=alu();sd.op_class=kSprStoreData;sd.flags=kRetires|kMicroOp|kLastMicroOp;
 auto plain=alu();plain.op_class=4;
 fst::RetiredFrontendMacro m;m.begin_record=1;m.end_record=3;m.pc=4096;m.address_space_id=1;m.code_identity=11;m.semantic_identity=19;
 m.instruction_size=1;m.bytes[0]=0x90;m.observed_uops=2;m.known_flags=fst::kFrontendRequiredKnown;
 m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro|fst::kFrontendDsbEligible;
 auto z=m;z.begin_record=0;z.end_record=1;z.observed_uops=1;auto tail=z;tail.begin_record=3;tail.end_record=4;
 write_frontend(dir+"frontpair.fst",0,{zero,sa,sd,plain},{z,m,tail});
 std::string reference;
 for(unsigned chunk:{1,2,7,128,129}){auto c=frontend_config(chunk==129?7:chunk);c.causal_runtime_audit_path=dir+"frontpair"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"frontpair.fst",2,2,1,2,chunk==129));
  const auto result=Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
  require(find(events,"retire",0,0).completion==find(events,"frontend_rename",0,0).cycle,"zero constant result is ready at actual rename");
  unsigned allocations=0;for(const auto& e:events)allocations+=e.kind=="store_allocate";
  require(allocations==1 && result.cores[0].retired_instructions==2,"certified SA/SD has one SQ token and measured macro despite frontend seam");
  require(find(events,"store_allocate",0,1).cycle==find(events,"frontend_dispatch",0,1).cycle,"store actual allocation stays at dispatch, separate from rename reservation");
  if(instruction_matrix) {
   const auto allocated=find(events,"store_allocate",0,1),prepared=find(events,"prepare_accepted",0,1),taken=find(events,"prepare_taken_over",0,1);
   const auto sent=find(events,"store_write_accepted",0,0),returned=find(events,"store_write_complete",0,0);
   require(allocated.completion==prepared.completion && prepared.completion==taken.completion && sent.issue==returned.issue,
    "combined frontend/I/coherent early prepare preserves SQ/prepare/takeover/write identities");
  }
  require(find(events,"store_write_complete",0,0).cycle>find(events,"core_accounting_finish",0,4).cycle,"late physical store drain cannot extend local frontend core lifetime");
  const auto audit=normalized(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"zero SA/SD detached-tail tuples invariant");
 }
 // Actual one-entry shared SQ pressure includes rename-in-flight reservations.
 auto store=sa;store.op_class=57;store.flags=kRetires|kStore|kPhysicalAddress;
 write_frontend(dir+"frontsq.fst",0,{store,store,plain});reference.clear();
 for(unsigned chunk:{1,2,7,128}){auto c=frontend_config(chunk);c.spr_profile.limits.shared_stores=1;
  c.causal_runtime_audit_path=dir+"frontsq"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"frontsq.fst",0,3));
  Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
  require(find(events,"frontend_rename",0,1).cycle>=find(events,"store_write_complete",0,0).cycle,"SQ reservation cannot acknowledge younger store before shared credit returns");
  const auto audit=normalized(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"SQ pressure tuples invariant");
 }
 // Reuse actual graph lifecycle with a delayed remote parent and unequal
 // warmup origins. Core 0 stops locally while core 1 drains its miss.
 auto wait=plain;wait.pc=8192;auto later=load();later.pc=8196;later.address=8192;
 write_frontend(dir+"frontgraph0.fst",0,{load(),plain});write_frontend(dir+"frontgraph1.fst",1,{wait,later,plain});
 std::ofstream plan(dir+"frontgraph.plan");plan<<"FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 2\nCORE 0 2 1 1\nCORE 1 3 0 3\n"
  "EVENT 0 0 1 4096 1 5 1 0\nEVENT 1 1 0 8192 1 6 1 1 0\n";plan.close();reference.clear();
 for(unsigned chunk:{1,2,7,128,129}){auto c=frontend_config(chunk==129?7:chunk);c.cores=2;
  c.functional_sync_plan=dir+"frontgraph.plan";c.causal_runtime_audit_path=dir+"frontgraph"+std::to_string(chunk)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(source(dir+"frontgraph0.fst",1,1));inputs.push_back(source(dir+"frontgraph1.fst",0,3,0,3,chunk==129));
  const auto result=Simulator(c,std::move(inputs)).run();const auto events=audit_rows(c.causal_runtime_audit_path);
  const auto release=find(events,"graph_release",1,0).cycle;
  require(release==find(events,"retire",0,1).cycle && find(events,"frontend_rename",1,1).cycle>=release,"parked source forbids postgate rename while older backend completes");
  require(find(events,"core_accounting_finish",0,2).cycle<find(events,"core_accounting_finish",1,3).cycle && result.functional_sync_dependency_waits==1,"unequal origins and local EOF survive frontend state");
  const auto audit=normalized(c.causal_runtime_audit_path);if(reference.empty())reference=audit;else require(audit==reference,"graph frontend tuple streams invariant");
 }
}

void frontend_gates_and_refill(){
 const std::string dir="tmp/f1-fs-bridge/";
 require(!SimulatorConfig{}.causal_retired_frontend,"causal retired frontend defaults OFF");
 for(unsigned mode=0;mode<12;++mode){auto c=frontend_config(1);
  if(mode==0)c.causal_runtime=false;if(mode==1)c.causal_static_integer=false;
  if(mode==2)c.spr_retired_frontend=false;if(mode==3)c.rename_to_dispatch=1;
  if(mode==4)c.l1i_enabled=true;if(mode==5)c.fetch_supply_physical_request_ledger=true;
  if(mode==6)c.fetch_supply_lower_hierarchy=true;if(mode==7)c.dtlb.enabled=true;
  if(mode==8)c.rename_free_list=true;if(mode==9)c.fetch_supply_speculative_shadow=true;
  if(mode==10)c.fetch_buffer_bytes=32;if(mode==11)c.l2.replacement=ReplacementPolicy::kTreePlru;
  bool rejected=false;try{c.validate();}catch(const std::exception&){rejected=true;}require(rejected,"frontend flag preserves unsupported hardware/configuration gates");
 }
 auto r=alu();r.op_class=4;r.flags|=kMicroOp;
 fst::RetiredFrontendMacro m;m.begin_record=0;m.end_record=1;m.pc=4096;m.address_space_id=1;m.code_identity=11;m.semantic_identity=19;
 m.instruction_size=1;m.bytes[0]=0x90;m.observed_uops=1;m.known_flags=fst::kFrontendRequiredKnown;m.flags=fst::kFrontendStartsMacro;
 auto next=m;next.begin_record=1;next.end_record=2;
 write_frontend(dir+"frontrefill.fst",0,{r,r},{m,next});
 std::uint64_t first=0,second=0;
 for(unsigned delay:{0,7}){auto c=frontend_config(1);c.fetch_buffer_refill_latency=delay;c.causal_runtime_audit_path=dir+"frontrefill"+std::to_string(delay)+".csv";
  std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(std::make_unique<BinaryTraceSource>(dir+"frontrefill.fst"));Simulator(c,std::move(inputs)).run();
  const auto events=audit_rows(c.causal_runtime_audit_path);const auto a=find(events,"frontend_rename",0,0).cycle,b=find(events,"frontend_rename",0,1).cycle;
  if(!delay){first=a;second=b;}else require(a==first+7 && b==second+7,"configured no-L1I refill charged on byte-buffer miss only");
 }
 for(unsigned mode=0;mode<3;++mode){auto bad=m;
  if(mode==0){bad.known_flags^=fst::kFrontendDsbEligible;}
  if(mode==1)bad.flags|=fst::kFrontendSharedAllocation;
  if(mode==2)bad.flags&=~fst::kFrontendStartsMacro;
  const auto path=dir+"frontbad"+std::to_string(mode)+".fst";write_frontend(path,0,{r},{bad});
  bool rejected=false;try{auto c=frontend_config(1);std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(std::make_unique<BinaryTraceSource>(path));Simulator(c,std::move(inputs)).run();}
  catch(const std::exception&){rejected=true;}require(rejected,"unknown facts/shared backend semantics/missing leader fail closed");
 }
}

void preserve_gates(){
 write("tmp/f1-fs-bridge/io-origin.fst",0,{load()});
 bool io_rejected=false;try{std::vector<std::unique_ptr<TraceSource>> inputs;inputs.push_back(std::make_unique<DeliveredBinary>("tmp/f1-fs-bridge/io-origin.fst",false,false,true));Simulator(config(1),std::move(inputs)).run();}catch(const std::invalid_argument&){io_rejected=true;}
 require(io_rejected,"translated IO origin must not become an ordinary RAM transaction");
 for(unsigned mode=0;mode<5;++mode){auto c=config(1);if(mode==0)c.causal_runtime=false;if(mode==1)c.interval_scheduler="round_robin";
  if(mode==2)c.dtlb.enabled=true;if(mode==3)c.fetch_to_decode=1;if(mode==4)c.response_queue_feedback=true;
  bool rejected=false;try{c.validate();}catch(const std::exception&){rejected=true;}require(rejected,"FS flag preserves unsupported configuration gates");}
 auto c=config(1);auto branch=alu();branch.flags|=kBranch;write("tmp/f1-fs-bridge/branch.fst",0,{branch});
 bool rejected=false;try{std::vector<std::unique_ptr<TraceSource>> input;input.push_back(source("tmp/f1-fs-bridge/branch.fst",0,1));Simulator(c,std::move(input)).run();}catch(const std::invalid_argument&){rejected=true;}
 require(rejected,"FS flag cannot turn unsupported branch into ordinary ALU");
}

}
int main(){std::filesystem::create_directories("tmp/f1-fs-bridge");causal_instruction_supply();causal_instruction_spans();causal_instruction_capacity();frontend_actual_rename();frontend_width_modes_and_pressure();frontend_zero_store_and_graph();frontend_gates_and_refill();continuous_warmup();cross_core_graph();local_clocks_and_kernel();partial_macro_and_corrupt_source();stores_across_seam();corrupted_origins_and_anchors();overlap_and_origin_members();preserve_gates();instruction_matrix=true;frontend_actual_rename();frontend_width_modes_and_pressure();frontend_zero_store_and_graph();instruction_matrix=false;std::cout<<"causal FS execution tests passed\n";}
