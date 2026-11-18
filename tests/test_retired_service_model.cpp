#include "fastsim/retired_service_model.hpp"
#include <iostream>
#include <stdexcept>
static void check(bool x,const char* why){if(!x)throw std::runtime_error(why);}
int main(){try{
 using M=fastsim::RetiredServiceModel;constexpr auto Q=fastsim::kServiceQ;
 M m(false,192,768,32768,2048,16,8);
 fastsim::ServiceObservation a;a.line=10;a.pc=100;a.own_mask=7;a.legacy_mask=7;
 a.selected_issue=0;a.selected_response=30;a.final_issue=0;
 auto cold=m.observe(a);check(cold.p[2]==Q,"cold service remains one memory miss");
 a.own_mask=0;a.legacy_mask=1;a.selected_issue=20;a.final_issue=40;a.selected_response=42;
 auto uncertain=m.observe(a);check(uncertain.p[0]==Q/2 && uncertain.p[1]==0,"uniform issue interval gives bounded pending probability");
 a.forwarded=true;check(m.observe(a).p[0]==0,"forwarding is a proven zero");a.forwarded=false;
 m.invalidate(10);check(m.observe(a).p[0]==0,"write terminates the old read generation");
 M future(false,192,768,32768,2048,16,8);
 fastsim::ServiceObservation newer;newer.line=20;newer.own_mask=newer.legacy_mask=7;
 newer.selected_issue=newer.final_issue=100;newer.selected_response=200;future.observe(newer);
 newer.own_mask=newer.legacy_mask=0;newer.selected_issue=50;newer.final_issue=150;newer.selected_response=152;
 check(future.observe(newer).fallback,"a future selected owner needs explicit fallback, not a fabricated parent");
 a.known_source=false;a.legacy_mask=3;auto fallback=m.observe(a);
 check(fallback.p[0]==Q && fallback.p[1]==Q && fallback.p[2]==0 && fallback.fallback,"unknown source retains complete legacy point fallback");
 fastsim::ServiceMacroUnion same;
 same.add(10,{Q/2,0,0});same.add(10,{Q/2,0,0});
 check(same.finish().point[0]==Q/2,"same-line fragments must not assume independence");
 same.add(11,{Q/2,0,0});auto joined=same.finish();
 check(joined.lower[0]==Q/2 && joined.upper[0]==Q && joined.point[0]==3*Q/4,"distinct-line Frechet bounds retain correlation uncertainty");
 fastsim::ServiceMacroUnion generations;
 generations.add(10,{Q/2,0,0},1);generations.add(10,{Q/2,0,0},2);
 check(generations.finish().upper[0]==Q,"same address in different observed generations is not one fill");
 M ledger(false,192,768,32768,2048,16,8);
 a.known_source=true;a.forwarded=false;a.own_mask=a.legacy_mask=7;
 a.selected_issue=a.final_issue=0;a.selected_response=30;
 ledger.observe(a);ledger.finish_uop(false,true,false,true,true,false);
 ledger.begin_measurement();a.forwarded=true;ledger.observe(a);
 ledger.finish_uop(true,true,false,true,true,false);
 check(ledger.snapshot().scopes[0].macro_loads==1 && ledger.snapshot().scopes[3].expected[2]==Q,
       "warmup-spanning macro retains prior fragments and counts once");
 ledger.observe(a);ledger.finish_uop(false,true,false,true,true,false);ledger.abandon();
 check(ledger.snapshot().scopes[0].macro_loads==1,"abandoned prefix creates no retired load");
 fastsim::ServiceMacroUnion overflow;
 for(unsigned i=0;i<20;++i)overflow.add(i,{Q/4,Q/8,0});
 check(overflow.finish().overflow && overflow.finish().upper[0]==Q,"bounded overflow preserves a full-population uncertainty bound");
 M p(true,192,768,32768,2048,16,8);
 fastsim::ServiceObservation r;r.pc=200;r.line=1;r.own_mask=r.legacy_mask=7;r.selected_issue=1;r.selected_response=100;r.final_issue=1;
 p.observe(r);p.finish_uop(true,true,false,true,true,false);
 p.note_branch_miss();r.selected_issue=200;r.selected_response=300;r.final_issue=200;
 auto warmed=p.observe(r);check(warmed.p[2]<Q && warmed.p[0]>=warmed.p[1] && warmed.p[1]>=warmed.p[2],"functional branch/reuse evidence produces bounded warming");
 for(unsigned i=0;i<10000;++i){r.line=(i*31)%100;r.selected_issue=i*1000;r.selected_response=r.selected_issue+100;r.final_issue=r.selected_issue+30;
  auto x=p.observe(r);check(x.p[0]<=Q && x.p[1]<=x.p[0] && x.p[2]<=x.p[1],"all service probabilities conserve nesting");
  p.finish_uop(true,true,false,true,true,false);}
 std::cout<<"retired service model tests passed\n";
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
