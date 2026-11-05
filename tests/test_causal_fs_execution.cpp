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
 bool complete_dependencies()const override{return binary_.complete_dependencies();}
 bool functional_retirement_complete()const override{return binary_.functional_retirement_complete();}
 bool memory_request_roles_complete()const override{return binary_.memory_request_roles_complete();}
 std::optional<FunctionalSyncLayout> functional_sync_layout()const override{return binary_.functional_sync_layout();}
 std::optional<std::uint64_t> current_source_record_ordinal()const override{auto o=binary_.current_source_record_ordinal();if(o&&bad_ordinal_)++*o;return o;}
 std::optional<fst::MemoryRequestRole> current_memory_request_role()const override{return binary_.current_memory_request_role();}
 std::optional<bool> current_ordinary_load_eligible()const override{return binary_.current_ordinary_load_eligible();}
 const fst::StoreRequestFacts* current_memory_facts()const override{return binary_.current_memory_facts();}
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
int main(){std::filesystem::create_directories("tmp/f1-fs-bridge");continuous_warmup();cross_core_graph();local_clocks_and_kernel();partial_macro_and_corrupt_source();stores_across_seam();corrupted_origins_and_anchors();overlap_and_origin_members();preserve_gates();std::cout<<"causal FS execution tests passed\n";}
