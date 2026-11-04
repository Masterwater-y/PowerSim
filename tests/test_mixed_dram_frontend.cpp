#include "fastsim/mixed_dram.hpp"
#include <iostream>
#include <stdexcept>
#include <tuple>
using namespace fastsim;
void check(bool b,const char* message){if(!b)throw std::runtime_error(message);}
template<class F> void rejects(F f){bool bad=false;try{f();}catch(const std::invalid_argument&){bad=true;}check(bad,"incompatible controller API accepted");}
MixedDramConfig fixture(){MixedDramConfig c;c.dram.channels=1;c.dram.size_bytes=1ull<<30;c.dram.ranks_per_channel=1;c.dram.banks_per_channel=8;c.dram.bank_groups_per_rank=4;c.dram.row_bytes=8192;c.dram.write_buffer_size=4;c.dram.read_buffer_size=4;c.dram.write_low_threshold_percent=50;c.dram.frontend_latency=7;c.dram.backend_latency=3;return c;}
MixedDramRequest req(unsigned id,unsigned arrival,unsigned line,bool write){return {{0,id,0,write?DramServiceKind::kWriteback:DramServiceKind::kRead},arrival,line};}
int main(){
 auto c=fixture();MixedDramController ctrl(c,64);ctrl.enable_native_frontend();
 ctrl.submit(req(0,0,0,true));ctrl.submit(req(1,1,0,true));ctrl.submit(req(2,2,0,false));
 rejects([&]{ctrl.advance(100);});
 auto p=ctrl.advance_with_admissions(1000);check(p.admissions.size()==3,"all logical requests admitted");
 check(p.completions.empty(),"quiet merged write and forwarded read create no physical service");
 check(p.admissions[0].frontend_response==7 && !p.admissions[0].merged_write,"write ACK is admission plus frontend");
 check(p.admissions[1].merged_write && p.admissions[1].frontend_response==8,"merged write has own frontend ACK");
 check(p.admissions[2].forwarded_read && p.admissions[2].frontend_response==9,"forwarded read excludes backend/DDR latency");
 check(p.admissions[1].queued_write && p.admissions[2].queued_write && p.admissions[1].queued_write->sequence==0 && p.admissions[2].queued_write->sequence==0,"merge/forward bind original physical queue owner");
 auto s=ctrl.frontend_stats();check(s.write_responses_scheduled==2 && s.merged_write_requests==1 && s.forwarded_read_requests==1,"native frontend populations separate");
 auto old=ctrl.stats();check(old.admitted==3 && old.pending_writes==1 && old.writes_serviced==0 && old.reads_serviced==0,"logical admissions are not physical services");
 check(old.admitted==old.pending_writes+old.pending_reads+old.reads_serviced+old.writes_serviced+s.merged_write_requests+s.forwarded_read_requests,"admission populations conserved");
 check(ctrl.advance_with_admissions(1000).admissions.empty(),"frontier reentry cannot emit twice");
 rejects([&]{ctrl.enable_native_frontend();});
 rejects([&]{ctrl.reserve_projected_batch({});});
 // Native capacity check occurs BEFORE write-queue search for a forwarded read.
 c.dram.read_buffer_size=1;MixedDramController pressure(c,64);pressure.enable_native_frontend();
 pressure.submit(req(0,0,0,true));pressure.submit(req(1,0,128,false));pressure.submit(req(2,1,0,false));
 auto before=pressure.advance_with_admissions(2);check(before.admissions.size()==2 && before.completions.size()==1,"full read receiver blocks even WQ-hit load");
 auto after=pressure.advance_with_admissions(1000);check(after.admissions.size()==1 && after.admissions[0].forwarded_read,"WQ-hit load eventually accepted");
 check(after.admissions[0].admission==before.completions[0].dram_ready && after.admissions[0].frontend_response==after.admissions[0].admission+7,"read queue credit returns at physical readiness before response");
 // A full write queue refuses a same-line merge before searching the queue.
 c=fixture();c.dram.write_buffer_size=2;c.dram.write_low_threshold_percent=100;c.dram.write_high_threshold_percent=100;
 MixedDramController full(c,64);full.enable_native_frontend();full.submit(req(0,0,0,true));full.submit(req(1,0,128,true));full.submit(req(2,1,0,true));
 auto f=full.advance_with_admissions(2);check(f.admissions.size()==2 && full.stats().waiting_admission==1 && full.frontend_stats().merged_write_requests==0,"full queue cannot merge around admission bound");
 // A selected write has left WQ; an in-flight DDR write is not a WQ hit.
 c=fixture();c.dram.write_low_threshold_percent=0;
 MixedDramController selected(c,64);selected.enable_native_frontend();selected.submit(req(0,0,0,true));
 auto w=selected.advance_with_admissions(1);check(w.completions.size()==1,"write selected into physical service");
 selected.submit(req(1,1,0,false));auto read=selected.advance_with_admissions(1000);
 check(read.admissions.size()==1 && !read.admissions[0].forwarded_read && !read.admissions[0].frontend_response && read.completions.size()==1,"only queued writes may forward");
 c=fixture();c.dram.frontend_latency=0;MixedDramController zero(c,64);zero.enable_native_frontend();
 zero.submit(req(0,0,0,true));zero.submit(req(1,0,0,false));auto z=zero.advance_with_admissions(1);
 check(z.admissions.size()==2 && z.admissions[0].frontend_response==0 && z.admissions[1].frontend_response==0,"zero frontend delay stays same-cycle");
 // Live controller copies preserve frontend ownership and receipt state.
 c=fixture();MixedDramController original(c,64);original.enable_native_frontend();original.submit(req(0,0,0,true));original.advance_with_admissions(1);
 auto copied=original;original.submit(req(1,1,0,false));copied.submit(req(1,1,0,false));
 auto oa=original.advance_with_admissions(2),ca=copied.advance_with_admissions(2);
 check(oa.admissions.size()==1 && ca.admissions.size()==1 && oa.admissions[0].frontend_response==ca.admissions[0].frontend_response && copied.frontend_stats().forwarded_read_requests==1,"native frontend copy owns identical pending state");
 rejects([&]{copied.submit(req(1,2,0,false));});
 // Chunk the same closed arrival set; callback identity/times must be invariant.
 using Row=std::tuple<std::uint64_t,std::uint64_t,std::uint64_t,bool,bool,std::uint64_t>;
 std::vector<Row> reference;
 for(unsigned step:{1u,7u,1000u}){MixedDramController m(fixture(),64);m.enable_native_frontend();m.submit(req(0,0,0,true));m.submit(req(1,1,0,true));m.submit(req(2,2,0,false));std::vector<Row> rows;
  for(unsigned t=step;t<1000+step;t+=step){auto out=m.advance_with_admissions(t);for(auto e:out.admissions)rows.emplace_back(e.id.sequence,e.arrival,e.admission,e.merged_write,e.forwarded_read,e.frontend_response.value_or(UINT64_MAX));}
  if(reference.empty())reference=rows;else check(reference==rows,"native frontend changed with frontier granularity");
 }
 std::cout<<"native DRAM frontend admission/merge/forwarding tests passed\n";
}
