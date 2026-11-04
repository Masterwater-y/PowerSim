#include "fastsim/mixed_dram.hpp"
#include <iostream>
#include <stdexcept>
using namespace fastsim;
namespace {
void check(bool b,const char* m){if(!b)throw std::runtime_error(m);}
MixedDramConfig cfg(MixedDramScheduler policy) {
    MixedDramConfig c;c.scheduler=policy;c.page_policy="open";
    auto& d=c.dram;d.channels=d.ranks_per_channel=1;d.banks_per_channel=d.bank_groups_per_rank=2;
    d.row_bytes=256;d.t_cl=d.t_rcd=d.t_rp=10;d.burst_cycles=4;
    d.t_rrd=d.t_rrd_l=4;d.t_ccd_l=100;d.t_cs=2;d.t_ras=d.t_rtp=0;
    d.frontend_latency=d.backend_latency=0;d.frfcfs_selection_window=0;
    c.t_cwl=c.t_rcd_wr=10;return c;
}
MixedDramRequest req(unsigned id,std::uint64_t line,std::uint64_t time=0) {
    return {{0,id,0,DramServiceKind::kRead},time,line};
}
std::vector<MixedDramCompletion> run(MixedDramConfig c,unsigned step) {
    MixedDramController m(c,64);m.submit(req(0,0));m.submit(req(1,1));m.submit(req(2,4));
    std::vector<MixedDramCompletion> out;
    for(unsigned end=step;end<1000;end+=step){auto r=m.advance(end);out.insert(out.end(),r.begin(),r.end());}
    auto r=m.advance(1000);out.insert(out.end(),r.begin(),r.end());return out;
}
void tests() {
    const auto native=run(cfg(MixedDramScheduler::kBankPrepFrfcfs),1000);
    const auto old=run(cfg(MixedDramScheduler::kRowHitFirstApproximation),1000);
    check(native.size()==3 && native[0].id.sequence==0 && native[1].id.sequence==2 && native[2].id.sequence==1,"hidden closed bank must beat nonseamless row hit");
    check(old[1].id.sequence==1,"legacy row-hit-first stays unchanged");
    check(native[1].command<native[2].command,"model bank preparation admits earlier column");
    for(unsigned step:{1,7,100})check(run(cfg(MixedDramScheduler::kBankPrepFrfcfs),step)==native,"strict frontier slicing cannot change candidate or command");
    auto c=cfg(MixedDramScheduler::kBankPrepFrfcfs);c.dram.t_ccd_l=4;
    const auto seamless=run(c,1000);check(seamless[1].id.sequence==1 && seamless[1].row_hit,"seamless hit keeps FCFS hit priority");
    c=cfg(MixedDramScheduler::kBankPrepFrfcfs);c.dram.t_rrd=c.dram.t_rrd_l=100;
    const auto visible=run(c,1000);check(visible[1].id.sequence==1 && visible[1].row_hit,"unhidden bank preparation cannot displace first prepped hit");
    bool bad=false;c.dram.frfcfs_selection_window=1;
    try{MixedDramController model(c,64);}catch(const std::invalid_argument&){bad=true;}
    check(bad,"source full-queue policy cannot silently honor legacy selection truncation");
    c.dram.frfcfs_selection_window=0;c.dram.refresh_enabled=true;bad=false;
    try{MixedDramController refresh(c,64);}catch(const std::invalid_argument&){bad=true;}
    check(bad,"active rank refresh cannot silently enter all-ranks-idle selector");
    c.dram.refresh_enabled=false;MixedDramController strict(c,64);bad=false;
    try{strict.reserve_projected_batch({req(0,0)});}catch(const std::invalid_argument&){bad=true;}
    check(bad && strict.stats().submitted==0,"bank-prep source policy requires strict certified arrivals before mutation");
    // All first requests miss: select oldest request within earliest bank set,
    // not rank/bank index scan order. Later known arrivals remain ordinary.
    c=cfg(MixedDramScheduler::kBankPrepFrfcfs);MixedDramController model(c,64);
    model.submit(req(9,4));model.submit(req(10,0));
    auto first=model.advance(1);check(first.size()>=1 && first[0].id.sequence==9,"equal earliest banks preserve request queue order");
    model.submit(req(11,1,1));model.advance(1000);
    check(model.stats().reads_serviced==3,"later source admission retains full population");
}
}
int main(){try{tests();std::cout<<"mixed DRAM bank-prep FRFCFS tests passed\n";return 0;}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
