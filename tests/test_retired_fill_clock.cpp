#include "fastsim/retired_fill_clock.hpp"
#include "fastsim/retired_cache_miss_estimator.hpp"
#include "fastsim/config.hpp"
#include <iostream>
#include <stdexcept>
static void check(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
int main(){try{
 using C=fastsim::RetiredFillClock;using S=fastsim::RetiredCacheMissEstimator;
 check(C::select(8,10,true,0).reference_issue==0,"zero final issue must remain valid");
 check(C::select(8,10,true).reference_issue==10,"missing final uses selected service");
 check(C::select(8,0,false).reference_issue==8,"missing clocks use canonical fallback");
 S s;S::Audit audit;
 auto p=s.observe(10,100,50,1,false,false,&audit,10);
 check(!p.fallback && audit.install_begin==10,"CPU admission after callback must not shift an existing service interval");
 auto h=s.observe(10,60,62,0,false,false,&audit);
 check(h.misses==0,"completed parent cannot create a new pending fill");
 S t;t.observe(10,100,50,1,false,false,nullptr,10);
 auto f=t.observe(10,30,35,0,false,false,&audit);
 check(f.misses==1 && f.merged,"actual child inside selected service remains a follower");
 fastsim::SimulatorConfig cfg;
 cfg.core_model="interval_weave";cfg.response_queue_feedback=true;
 cfg.retired_cache_final_admission_clock=true;cfg.response_event_only_approximation=true;
 bool rejected=false;
 try{cfg.validate();}catch(const std::invalid_argument& e){
   rejected=std::string(e.what()).find("retired_cache_final_admission_clock")!=std::string::npos;
 }
 check(rejected,"unsupported event-only clock combination must explicitly reject");
 std::cout<<"retired fill clock tests passed\n";
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
