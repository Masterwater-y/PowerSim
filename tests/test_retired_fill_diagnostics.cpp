#include "fastsim/retired_cache_miss_estimator.hpp"
#include "fastsim/retired_fill_diagnostics.hpp"
#include <iostream>
#include <sstream>
#include <stdexcept>
static void check(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
int main(){
 try {
  using S=fastsim::RetiredCacheMissEstimator; S s(1),plain(1); S::Audit a;
  auto p=s.observe(10,0,20,1,false,false,&a);
  check(a.decision==S::Decision::Install && a.install_begin==0,"zero issue is a valid fill begin");
  check(p.misses==plain.observe(10,0,20,1).misses,"metadata is neutral");
  auto q=s.observe(10,10,12,0,false,false,&a);
  check(a.decision==S::Decision::Pending && a.begin==0 && a.end==20 && q.misses==1,"pending records original parent interval");
  check(q.misses==plain.observe(10,10,12,0).misses,"metadata is neutral for hits converted to pending");
  S owners;
  owners.observe(10,0,20,1,false,false,nullptr,UINT64_MAX,4);
  owners.observe(10,5,7,0,false,false,&a);
  check(a.owner_ordinal==4 && a.shadow_generation==1,"follower links to the creating source member");
  q=s.observe(10,20,22,0,false,false,&a);
  check(a.decision==S::Decision::Hit && q.misses==0,"half-open end is resident");
  q=s.observe(10,10,12,7,true,false,&a);
  check(a.decision==S::Decision::Forwarded && q.misses==0,"forwarding bypasses shadow");
  std::ostringstream out; fastsim::RetiredFillDiagnostics d(out,1,1);
  fastsim::RetiredFillSample sample; sample.core=0; sample.ordinal=4;sample.line=10;sample.own=0;
  d.member(sample,a,q);d.member(sample,a,q);d.finish();
  check(d.members()==2,"full counters survive sample cap");
  check(out.str().find("\"omitted\":1")!=std::string::npos,"truncation is explicit");
  std::ostringstream uncertain_output,summary;
  fastsim::RetiredFillDiagnostics uncertain(uncertain_output,1,1);
  sample.private_level=5;sample.input_fallback_reasons=2;
  uncertain.member(sample,a,q);uncertain.write_summary(summary);uncertain.finish();
  check(summary.str().find("\"unknown_private_members\":1")!=std::string::npos,
        "full uncertainty counts survive independently of samples");
  check(summary.str().find("\"input_fallback_members\":1")!=std::string::npos,
        "caller fallback is distinct from shadow fallback");
  std::cout<<"retired fill diagnostics passed\n";
 } catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
